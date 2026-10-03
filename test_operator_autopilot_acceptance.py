"""PR10 — real acceptance test for Autopilot (v0.13).

The scenario from the original brief, run end to end with a *real detached
worker process* started through the production ``hermes_autopilot_start`` path:

- >= 4 nodes, >= 3 independent, and >= 2 placements (two fleet peers with
  different capability profiles, so placement genuinely splits the work);
- a mid-run peer kill: the peer holding one node disappears from the manifest and
  its run fails, and the node is rerouted to the surviving peer with no
  duplicate mutation-capable attempt;
- a real approval frontier: Autopilot stops at the approval node without
  dispatching past it, the *owner* resolves it through the existing tools, and
  Autopilot resumes;
- the Mission's own final approval is performed by the owner in a separate
  Owner-mode process; the worker is shown to hold no owner authority;
- the whole run is repeated with the MCP server restarted mid-Mission: the
  server that started Autopilot has exited and every later read or owner action
  comes from a fresh interpreter, while the same worker keeps ownership.

Only the remote fleet is stood in for: a file-backed "world" (peers manifest,
submission ledger, runner job records) that the test itself drives. Placement,
contract validation, the delegation store, Mission attachments, reconciliation,
supersession, the classifier and the scheduler are all the real code.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="Linux /proc authority proof; portable Autopilot unit tests run on every OS")

import operator_autopilot as autopilot
import operator_job_supervisor as jobs
import operator_mission_runtime as mission
import operator_policy as op
from test_operator_autopilot_advance import _observe

REPO = str(Path(__file__).resolve().parent)
MID = "msn-acceptance"

# ---------------------------------------------------------------------------
# The stand-in for the remote fleet (runs inside the detached worker process)
# ---------------------------------------------------------------------------

LAUNCHER = '''
import json, sys, time
from pathlib import Path

repo, mission, job_id, root = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])
sys.path.insert(0, repo)
import operator_autopilot as ap
import operator_contract as cm
import operator_placement as pl

world = root / "world"


def targets(hermes_root=None, **kw):
    peers = json.loads((world / "peers.json").read_text())
    return [{
        "entity_id": "fleet:" + name, "kind": "fleet_peer", "name": name, "enabled": True, "reachable": True,
        "identity_configured": True, "authorization_ceiling": spec["ceiling"], "allowed_profiles": spec["profiles"],
        "features": [], "workspaces": [], "backends": [], "skills": [], "model": "", "provider": "",
        "host_role": "worker", "allow_public_actions": False,
    } for name, spec in sorted(peers.items())]


def dispatch(contract_json, confirm=False, dry_run=True, timeout=30, hermes_root=None):
    contract = json.loads(contract_json)
    if dry_run:
        return json.dumps({"success": True, "dry_run": True, "changed": False})
    with (world / "ledger.jsonl").open("a") as fh:
        fh.write(json.dumps({"task_id": contract["task_id"], "agent": contract["assigned_agent"],
                             "auth": contract["authorization"]["class"], "t": time.time()}) + "\\n")
    return json.dumps({"success": True, "changed": True, "state": "running"})


pl.load_manifest_targets = targets
cm.hermes_contract_dispatch = dispatch
ap.RETRY_BACKOFF_BASE_SECONDS = 0.0  # the real backoff is covered by the recovery tests
ap.RETRY_BACKOFF_CAP_SECONDS = 0.0
sys.exit(ap._worker(mission, job_id, root))
'''

# ---------------------------------------------------------------------------
# What an MCP server does. Run in-process (resident) or in a fresh interpreter (restarted).
# ---------------------------------------------------------------------------

ACTIONS = '''
import json, os, subprocess, sys
from pathlib import Path


def run(action, repo, root, mid):
    if repo not in sys.path:
        sys.path.insert(0, repo)
    import operator_autopilot as autopilot
    import operator_mission_plan as plan
    import operator_mission_runtime as mission
    root = Path(root)
    if action == "start":
        real = subprocess.Popen

        def popen(argv, **kw):
            if "--worker" in argv:
                i = argv.index("--worker")
                argv = [argv[0], str(root / "world" / "launcher.py"), repo, argv[i + 1], argv[i + 3], argv[i + 5]]
            return real(argv, **kw)

        subprocess.Popen = popen
        try:
            out = json.loads(autopilot.hermes_autopilot_start(
                mid, max_concurrency=3, max_replans=0, confirm=True, dry_run=False, hermes_root=root))
        finally:
            subprocess.Popen = real
    elif action == "status":
        out = json.loads(autopilot.hermes_autopilot_status(mid, hermes_root=root))
    elif action == "mission":
        out = json.loads(mission.hermes_mission_get(mid, hermes_root=root))
    elif action == "resolve_gate":
        out = {}
        for target in ("dispatched", "running", "awaiting_review", "validated", "awaiting_approval", "completed"):
            out = json.loads(plan.hermes_plan_node_transition(
                mid, "gate", target, confirm=True, dry_run=False, hermes_root=root))
            assert out["success"] is True, out
    elif action == "approve":
        out = json.loads(mission.hermes_mission_approve(
            mid, "acceptance-final-approval", confirm=True, dry_run=False, hermes_root=root))
    elif action == "stop":
        out = json.loads(autopilot.hermes_autopilot_stop(mid, confirm=True, dry_run=False, hermes_root=root))
    else:
        raise ValueError(action)
    out["_server_pid"] = os.getpid()
    return out


if __name__ == "__main__":
    _repo, _root, _mid, _action = sys.argv[1:5]
    print(json.dumps(run(_action, _repo, _root, _mid)))
'''

OWNER_ENV = {
    op.OPERATOR_LEVEL_ENV: "owner",
    op.OWNER_ACTIVE_ENV: "1",
    op.OWNER_ACK_ENV: op.OWNER_ACK_REQUIRED_VALUE,
}


class Server:
    """An MCP server. ``restarting=True`` makes every call a brand-new process."""

    def __init__(self, root: Path, *, restarting: bool):
        self.root, self.restarting = root, restarting
        self.script = root / "world" / "actions.py"
        self.pids: list[int] = []

    def call(self, action: str, extra_env: dict[str, str] | None = None) -> dict:
        if self.restarting:
            proc = subprocess.run(
                [sys.executable, str(self.script), REPO, str(self.root), MID, action],
                capture_output=True, text=True, timeout=90, check=False, env={**os.environ, **(extra_env or {})},
            )
            assert proc.returncode == 0, proc.stderr[-2000:]
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        else:
            spec = importlib.util.spec_from_file_location("acceptance_actions", self.script)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            patch = pytest.MonkeyPatch()
            try:
                for key, value in (extra_env or {}).items():
                    patch.setenv(key, value)
                out = module.run(action, REPO, str(self.root), MID)
            finally:
                patch.undo()
        self.pids.append(out["_server_pid"])
        return out


# ---------------------------------------------------------------------------
# The world, as seen and driven by the test
# ---------------------------------------------------------------------------


def _ledger(root: Path) -> list[dict]:
    path = root / "world" / "ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _node_of(task_id: str) -> str:
    return task_id[len(f"ap-{MID}-"):-17]  # ap-<mission>-<node>-<key16>


def _by_node(root: Path) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for entry in _ledger(root):
        grouped.setdefault(_node_of(entry["task_id"]), []).append(entry)
    return grouped


def _wait(predicate, what: str, timeout: float | None = None):
    timeout = timeout or float(os.environ.get("ACCEPTANCE_WAIT_SECONDS", "40"))
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}; last={last!r}")


def _node(node_id: str, parents=(), profile: str = "hermes-researcher", kind: str = "single") -> dict:
    return {
        "node_id": node_id, "kind": kind, "owner": "owner" if kind == "approval" else profile, "parents": list(parents),
        "objective": f"Raw objective {node_id}.",
        "capability_req": {"profile": profile, "skills": [], "authorization_class": "reversible_write"},
        "budget": {"est_minutes": 5, "est_tokens": 1000},
        "expected_artifacts": [] if kind == "approval" else ["work-contract.json"],
    }


PLAN = [
    _node("n1"),
    _node("n2", profile="hermes-dev"),
    _node("n3"),
    _node("n4", ["n1", "n2", "n3"]),
    _node("gate", ["n4"], kind="approval"),
    _node("n5", ["gate"]),
]

PEERS = {
    # rza can run anything but has the higher authority ceiling; rzb is dev-only with an exact-fit ceiling,
    # so placement prefers it for the dev node and rza takes the rest.
    "rza": {"profiles": ["hermes-researcher", "hermes-dev"], "ceiling": "high_impact"},
    "rzb": {"profiles": ["hermes-dev"], "ceiling": "reversible_write"},
}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "hermes"
    (root / "world").mkdir(parents=True)
    (root / "world" / "launcher.py").write_text(LAUNCHER)
    (root / "world" / "actions.py").write_text(ACTIONS)
    (root / "world" / "peers.json").write_text(json.dumps(PEERS))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.delenv(op.OWNER_ACTIVE_ENV, raising=False)
    monkeypatch.delenv(op.OWNER_ACK_ENV, raising=False)
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, "1")
    monkeypatch.setenv(autopilot.IDLE_POLL_ENV, "0.5")
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    return root


def _create_mission_and_plan(root: Path) -> None:
    import operator_mission_plan as plan

    spec = json.dumps({
        "schema": mission.MISSION_SPEC_SCHEMA, "mission_id": MID, "title": "Autopilot acceptance",
        "objective": "Run a real multi-node Mission under Autopilot.", "owner_profile": "default",
        "acceptance_criteria": ["all nodes complete on observed evidence", "owner approves"],
        "context_refs": [], "skills": [], "final_approval_required": True,
    })
    assert json.loads(mission.hermes_mission_create(spec, confirm=True, dry_run=False, hermes_root=root))["success"]
    doc = json.dumps({"schema": plan.PLAN_SCHEMA, "mission_id": MID, "version": 1, "decomposition": "operator-provided",
                      "objective": "Raw mission objective.", "nodes": PLAN})
    created = json.loads(plan.hermes_plan_create(MID, doc, confirm=True, dry_run=False, hermes_root=root))
    assert created["success"] is True, created
    moved = json.loads(mission.hermes_mission_transition(MID, "running", confirm=True, dry_run=False, hermes_root=root))
    assert moved["changed"] is True, moved


def _alive(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False
    state = next((line.split()[1] for line in status.splitlines() if line.startswith("State:")), "")
    return state not in ("Z", "X")


# ---------------------------------------------------------------------------
# The acceptance scenario
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("restarting", [False, True], ids=["resident-server", "server-restarted-mid-mission"])
def test_autopilot_acceptance_scenario(world: Path, restarting: bool):
    root = world
    server = Server(root, restarting=restarting)
    _create_mission_and_plan(root)
    worker_pid = None
    try:
        # ---- start: the production path; in the restart run the starting server exits right after ----
        started = server.call("start")
        assert started["success"] is True, started
        job_id = started["job_id"]
        assert started["run"]["attempt"] == 1

        # ---- >= 3 independent nodes run in parallel, on >= 2 placements ----
        # The remote ledger is written inside dispatch, before _dispatch_one commits
        # the plan node's durable "dispatched" transition. Wait for both facts:
        # all remote submissions exist and the authoritative plan summary records
        # all three as in flight. Never sample the summary in the post-dispatch gap.
        def all_three_in_flight():
            if len(_ledger(root)) < 3:
                return None
            current = server.call("status")
            return current if current["summary"]["progress"]["in_flight"] == 3 else None

        status = _wait(all_three_in_flight, "three durable in-flight plan nodes")
        first = _by_node(root)
        assert sorted(first) == ["n1", "n2", "n3"] and all(len(v) == 1 for v in first.values())
        assert first["n1"][0]["agent"] == "rza" and first["n3"][0]["agent"] == "rza"
        assert first["n2"][0]["agent"] == "rzb"  # >= 2 placements: the dev node went to the other peer
        worker_pid = status["run"]["pid"]
        assert worker_pid and _alive(worker_pid)

        # The worker holds no owner authority (and Autopilot has no approval path).
        environ = {kv.split(b"=", 1)[0].decode() for kv in Path(f"/proc/{worker_pid}/environ").read_bytes().split(b"\0") if b"=" in kv}
        assert op.OWNER_ACTIVE_ENV not in environ and op.OWNER_ACK_ENV not in environ

        if restarting:
            # The MCP server that started Autopilot is long gone, and this status came from a new interpreter.
            assert started["_server_pid"] != status["_server_pid"] != os.getpid() and not _alive(started["_server_pid"])

        # ---- mid-run peer kill: rzb disappears and its run fails; the node is rerouted ----
        n1_done, n3_done = time.time(), time.time()
        _observe(root, first["n1"][0]["task_id"], state="completed")
        _observe(root, first["n3"][0]["task_id"], state="completed")
        peers = json.loads((root / "world" / "peers.json").read_text())
        del peers["rzb"]
        (root / "world" / "peers.json").write_text(json.dumps(peers))
        killed_at = time.time()
        _observe(root, first["n2"][0]["task_id"], state="failed", error="connection_reset by peer rzb")
        grouped = _wait(lambda: len(_by_node(root).get("n2", [])) >= 2 and _by_node(root), "n2 rerouted")
        retry = grouped["n2"][1]
        assert retry["agent"] == "rza" and retry["task_id"] != grouped["n2"][0]["task_id"]
        # No duplicate mutation-capable attempt: the second attempt started only after the first had failed.
        assert retry["t"] > killed_at
        assert len(grouped["n2"]) == 2
        assert "n4" not in grouped  # the join must not start while n2 has no verified result

        # ---- the join waits for every parent's verified completion ----
        n2_done = time.time()
        _observe(root, retry["task_id"], state="completed")
        grouped = _wait(lambda: "n4" in _by_node(root) and _by_node(root), "n4 dispatched")
        assert grouped["n4"][0]["t"] > max(n1_done, n3_done, n2_done)
        _observe(root, grouped["n4"][0]["task_id"], state="completed")

        # ---- approval frontier: Autopilot stops and reports; nothing is dispatched past the gate ----
        def waiting():
            s = server.call("status")
            return s if s["run"]["state"] == "waiting_for_owner" and s["summary"]["frontier"]["nodes"] == ["gate"] else None

        held = _wait(waiting, "waiting_for_owner at the approval node")
        assert held["summary"]["needs_owner"] is True and held["run"]["attempt"] == 1
        count_at_gate = len(_ledger(root))
        time.sleep(3.0)  # several worker ticks
        assert len(_ledger(root)) == count_at_gate and "n5" not in _by_node(root) and "gate" not in _by_node(root)
        assert server.call("status")["run"]["state"] == "waiting_for_owner"

        # ---- the owner resolves the gate through the existing tools; Autopilot resumes ----
        resolved_at = time.time()
        server.call("resolve_gate")
        grouped = _wait(lambda: "n5" in _by_node(root) and _by_node(root), "n5 dispatched after the owner resolved the gate")
        assert grouped["n5"][0]["t"] > resolved_at
        _observe(root, grouped["n5"][0]["task_id"], state="completed")

        # ---- every node is done on observed evidence; the Mission stops at its own approval gate ----
        def at_final_gate():
            m = server.call("mission")
            s = server.call("status")
            return (m, s) if m["status"] == "awaiting_approval" and s["run"]["state"] == "waiting_for_owner" else None

        m, s = _wait(at_final_gate, "the Mission awaiting the owner's final approval")
        assert not m.get("approval", {}).get("approved")  # Autopilot never approves
        assert s["summary"]["progress"]["percent"] == 100 and s["run"]["attempt"] == 1

        # ---- final approval by the owner, in a separate Owner-mode process ----
        approved = server.call("approve", extra_env=OWNER_ENV)
        assert approved["success"] is True and approved["status"] == "completed", approved

        def finished():
            s = server.call("status")
            return s if s["run"]["state"] == "completed" else None

        final = _wait(finished, "the worker finishing after the Mission completed")
        assert final["run"]["attempt"] == 1 and final["run"]["job_id"] == job_id  # ownership never changed hands
        assert final["run"]["pid"] == worker_pid
        assert _wait(lambda: not _alive(worker_pid), "the worker process exiting")
        assert jobs.get_job(job_id, hermes_root=root, reconcile=False)["status"] == "completed"

        # ---- global invariants over the whole run ----
        grouped = _by_node(root)
        assert {k: len(v) for k, v in grouped.items()} == {"n1": 1, "n2": 2, "n3": 1, "n4": 1, "n5": 1}
        entries = _ledger(root)
        assert len({e["task_id"] for e in entries}) == len(entries) == 6  # no duplicate submission
        assert all(e["auth"] == "reversible_write" for e in entries)  # nothing above the plan's authority
        assert "gate" not in grouped  # the approval node was never dispatched

        m = server.call("mission")
        assert m["status"] == "completed" and m["approval"]["approved_by"] == "owner"
        delegation_rows = {a["ref"]: a for a in m["attachments"] if a["kind"] == "delegation"}
        assert len(delegation_rows) == 6
        assert sum(1 for a in delegation_rows.values() if a["relationship"].startswith(mission.SUPERSEDED_PREFIX)) == 1
        if restarting:
            assert len(set(server.pids)) >= 5 and os.getpid() not in server.pids  # many servers, one worker
    finally:
        if worker_pid and _alive(worker_pid):
            with contextlib.suppress(Exception):  # best-effort cleanup only
                Server(root, restarting=False).call("stop")
