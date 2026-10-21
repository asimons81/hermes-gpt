"""Advisory review triage (v0.15): gates, admission, bounded store, and the
"advisories are never evidence" invariant.

Contracts are real: they are dispatched through ``operator_delegations`` with a
mocked backend, which writes the durable validation manifest that the by-digest
lookup reads. Nothing touches the real Hermes root or Jev.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from mcp import types

import jev_bridge as bridge
import operator_contract as contract_mod
import operator_delegations as delegations
import operator_policy as op
import operator_review_assist as assist
from test_operator_contract import _contract_for_ws

REPO = Path(__file__).resolve().parent
MARKER = "UNIQUE_ARTIFACT_MARKER_7f3a"
OBJECTIVE = "Refactor the mission overview cache"
RECORD_KEYS = {"contract_sha256", "artifact", "score", "model", "assessed_at", "advisory", "evidence"}


class Env:
    def __init__(self, root: Path, ws: Path):
        self.root = root
        self.ws = ws


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    op.set_audit_log_override(tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(ws))
    monkeypatch.setenv(bridge.ENABLE_ENV, "1")
    monkeypatch.setenv(assist.REVIEW_ENV, "1")
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *a, **k: json.dumps({"success": True, "changed": True, "backend": "pi_rpc", "state": "queued"}),
    )
    yield Env(root, ws)
    op.set_audit_log_override(None)


def _seed(env: Env, *, task_id: str = "review-task-001", artifacts: tuple[str, ...] = ("report.md",)) -> str:
    """Dispatch a real contract and return its canonical digest."""
    contract = _contract_for_ws(
        env.ws,
        task_id=task_id,
        objective=OBJECTIVE,
        expected_artifacts=[{"path": name, "must_exist": True, "min_bytes": 1} for name in artifacts],
    )
    created = json.loads(
        delegations.hermes_delegation_dispatch(
            json.dumps(contract),
            delegation_id=f"dlg-{task_id}",
            confirm=True,
            dry_run=False,
            hermes_root=env.root,
        )
    )
    assert created["success"] is True, created
    return contract_mod._parse_contract(json.dumps(contract))[2]


def _contract_json(env: Env, task_id: str = "review-task-001", artifacts: tuple[str, ...] = ("report.md",)) -> str:
    return json.dumps(
        _contract_for_ws(
            env.ws,
            task_id=task_id,
            objective=OBJECTIVE,
            expected_artifacts=[{"path": name, "must_exist": True, "min_bytes": 1} for name in artifacts],
        )
    )


def _score_result(score: float = 0.75) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="{}")],
        structuredContent={
            "model": "jev-test",
            "answer": {
                "type": "score",
                "score": score,
                "confidence": 0.8,
                "probabilities": {"0": 0.1, "4": 0.9},
                "legend": {"0": "Does not meet the acceptance criteria"},
            },
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    )


def _error_result(code: str) -> types.CallToolResult:
    return types.CallToolResult(
        isError=True,
        content=[types.TextContent(type="text", text=json.dumps({"code": code, "message": "x"}))],
    )


class FakeJev:
    def __init__(self, result: types.CallToolResult):
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name: str, args: dict) -> types.CallToolResult:
        self.calls.append((name, args))
        return self.result


def _use_jev(monkeypatch, result: types.CallToolResult | None = None) -> FakeJev:
    fake = FakeJev(result or _score_result())
    monkeypatch.setattr(bridge, "call", fake)
    return fake


def _forbid_contact(monkeypatch) -> None:
    async def boom(name, args):
        raise AssertionError("Jev bridge must not be contacted")

    def no_process(*args, **kwargs):
        raise AssertionError("no subprocess may be spawned")

    monkeypatch.setattr(bridge, "call", boom)
    monkeypatch.setattr(subprocess, "Popen", no_process)


def _assess(sha: str, artifact: str | None, root: Path) -> dict:
    return json.loads(asyncio.run(assist.hermes_review_assess(sha, artifact, hermes_root=root)))


def _store_lines(root: Path) -> list[str]:
    path = assist.advisory_path(root)
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bridge_gate", "review_gate"),
    [
        (None, "1"),
        ("0", "1"),
        ("1", None),
        ("1", "true"),
        ("1", "01"),
        ("1", ""),
    ],
)
def test_disabled_gates_return_disabled_without_contacting_anything(env, monkeypatch, bridge_gate, review_gate):
    sha = _seed(env)
    if bridge_gate is None:
        monkeypatch.delenv(bridge.ENABLE_ENV, raising=False)
    else:
        monkeypatch.setenv(bridge.ENABLE_ENV, bridge_gate)
    if review_gate is None:
        monkeypatch.delenv(assist.REVIEW_ENV, raising=False)
    else:
        monkeypatch.setenv(assist.REVIEW_ENV, review_gate)
    _forbid_contact(monkeypatch)

    out = _assess(sha, "report.md", env.root)

    assert out["success"] is False
    assert out["code"] == "DISABLED"
    assert _store_lines(env.root) == []
    assert not assist.advisory_path(env.root).exists()


def test_enabled_requires_both_gates_exactly(monkeypatch):
    monkeypatch.setenv(bridge.ENABLE_ENV, "1")
    monkeypatch.setenv(assist.REVIEW_ENV, "1")
    assert assist.enabled() is True
    monkeypatch.setenv(assist.REVIEW_ENV, "yes")
    assert assist.enabled() is False


# ---------------------------------------------------------------------------
# Lookup and admission
# ---------------------------------------------------------------------------


def test_unknown_contract_is_not_found_without_a_store(env, monkeypatch):
    jev = _use_jev(monkeypatch)
    out = _assess("0" * 64, None, env.root)
    assert out == {"success": False, "code": "NOT_FOUND", "message": assist._MESSAGES["NOT_FOUND"]}
    assert jev.calls == []


def test_unknown_digest_with_dispatched_contracts_is_not_found(env, monkeypatch):
    _seed(env)
    jev = _use_jev(monkeypatch)
    out = _assess("f" * 64, None, env.root)
    assert out["code"] == "NOT_FOUND"
    assert jev.calls == []


@pytest.mark.parametrize("bad", ["", "ABC", "A" * 64, "g" * 64, "0" * 63, "0" * 65])
def test_malformed_digest_is_rejected_before_lookup(env, monkeypatch, bad):
    jev = _use_jev(monkeypatch)
    out = _assess(bad, None, env.root)
    assert out["code"] == "INVALID_INPUT"
    assert jev.calls == []


def test_artifact_not_in_expected_list_is_refused(env, monkeypatch):
    sha = _seed(env)
    (env.ws / "report.md").write_text("ok", encoding="utf-8")
    jev = _use_jev(monkeypatch)
    out = _assess(sha, "other.md", env.root)
    assert out["code"] == "DENIED"
    assert jev.calls == []
    assert _store_lines(env.root) == []


@pytest.mark.parametrize("artifact", ["sub/report.md", "../report.md", "..\\report.md"])
def test_artifact_must_be_a_basename(env, monkeypatch, artifact):
    sha = _seed(env)
    jev = _use_jev(monkeypatch)
    assert _assess(sha, artifact, env.root)["code"] == "DENIED"
    assert jev.calls == []


def test_expected_but_missing_artifact_is_not_found(env, monkeypatch):
    sha = _seed(env)
    jev = _use_jev(monkeypatch)
    assert _assess(sha, "report.md", env.root)["code"] == "NOT_FOUND"
    assert jev.calls == []


def test_symlinked_artifact_is_refused(env, monkeypatch, tmp_path):
    sha = _seed(env)
    outside = tmp_path / "outside-secret.md"
    outside.write_text("outside content", encoding="utf-8")
    try:
        os.symlink(outside, env.ws / "report.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    jev = _use_jev(monkeypatch)
    assert _assess(sha, "report.md", env.root)["code"] == "DENIED"
    assert jev.calls == []


def test_symlinked_directory_component_is_refused(env, monkeypatch, tmp_path):
    sha = _seed(env, task_id="review-task-dirlink", artifacts=("nested/report.md",))
    real = tmp_path / "real-dir"
    real.mkdir()
    (real / "report.md").write_text("outside content", encoding="utf-8")
    # Replace the directory after dispatch with a symlink that leaves the workspace.
    (env.ws / "nested").mkdir()
    (env.ws / "nested" / "report.md").write_text("inside", encoding="utf-8")
    (env.ws / "nested" / "report.md").unlink()
    (env.ws / "nested").rmdir()
    try:
        os.symlink(real, env.ws / "nested", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    jev = _use_jev(monkeypatch)
    assert _assess(sha, "report.md", env.root)["code"] == "DENIED"
    assert jev.calls == []


def test_binary_artifact_is_unsupported(env, monkeypatch):
    sha = _seed(env)
    (env.ws / "report.md").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe\x80")
    jev = _use_jev(monkeypatch)
    assert _assess(sha, "report.md", env.root)["code"] == "UNSUPPORTED_ARTIFACT"
    assert jev.calls == []


def test_non_utf8_text_is_unsupported(env, monkeypatch):
    sha = _seed(env)
    (env.ws / "report.md").write_bytes(b"plain start \xc3\x28 broken utf8")
    jev = _use_jev(monkeypatch)
    assert _assess(sha, "report.md", env.root)["code"] == "UNSUPPORTED_ARTIFACT"
    assert jev.calls == []


def test_secret_looking_content_is_denied(env, monkeypatch):
    sha = _seed(env)
    (env.ws / "report.md").write_text("config\napi_key: abcdefgh12345678ijkl\n", encoding="utf-8")
    jev = _use_jev(monkeypatch)
    assert _assess(sha, "report.md", env.root)["code"] == "DENIED"
    assert jev.calls == []
    assert _store_lines(env.root) == []


def test_oversize_artifact_is_truncated_to_the_byte_bound(env, monkeypatch):
    sha = _seed(env)
    (env.ws / "report.md").write_text("a" * 20_000 + "TAIL_SHOULD_NOT_APPEAR", encoding="utf-8")
    jev = _use_jev(monkeypatch)
    out = _assess(sha, "report.md", env.root)
    assert out["success"] is True
    state = jev.calls[0][1]["state"]
    assert "a" * assist.ARTIFACT_MAX_BYTES in state
    assert "a" * (assist.ARTIFACT_MAX_BYTES + 1) not in state
    assert "TAIL_SHOULD_NOT_APPEAR" not in state
    assert len(state) <= assist.STATE_MAX_CHARS


def test_multibyte_truncation_boundary_does_not_fail(env, monkeypatch):
    sha = _seed(env)
    # 1 + 2*7000 bytes: the 12000-byte cut lands inside a two-byte character.
    (env.ws / "report.md").write_text("a" + "é" * 7000, encoding="utf-8")
    jev = _use_jev(monkeypatch)
    out = _assess(sha, "report.md", env.root)
    assert out["success"] is True
    assert jev.calls[0][1]["state"].count("é") == 5999


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def test_jev_request_uses_fixed_levels_and_durable_criteria_only(env, monkeypatch):
    sha = _seed(env)
    (env.ws / "report.md").write_text(MARKER, encoding="utf-8")
    jev = _use_jev(monkeypatch)
    assert _assess(sha, "report.md", env.root)["success"] is True

    name, args = jev.calls[0]
    assert name == "jev_score"
    assert set(args) == {"state", "instructions", "criteria"}
    assert args["criteria"] == list(assist.SCORE_LEVELS)
    assert len(args["criteria"]) == 5
    assert args["criteria"][0] == "Does not meet the acceptance criteria"
    assert args["criteria"][-1] == "Fully meets the acceptance criteria"
    assert MARKER in args["state"]
    assert "Expected artifacts: report.md" in args["state"]
    assert "Run outcome must be one of: completed, done." in args["instructions"]
    # The by-digest manifest deliberately drops the objective; it must never reach Jev.
    assert OBJECTIVE not in json.dumps(args)
    assert len(args["instructions"]) <= assist.INSTRUCTIONS_MAX_CHARS


def test_state_without_artifact_names_only_expected_artifacts(env, monkeypatch):
    sha = _seed(env, artifacts=("report.md", "notes.txt"))
    (env.ws / "report.md").write_text(MARKER, encoding="utf-8")
    jev = _use_jev(monkeypatch)
    assert _assess(sha, None, env.root)["success"] is True
    state = jev.calls[0][1]["state"]
    assert "report.md, notes.txt" in state
    assert MARKER not in state


# ---------------------------------------------------------------------------
# Outcomes and store
# ---------------------------------------------------------------------------


def test_success_writes_one_advisory_with_exact_keys_and_no_content(env, monkeypatch):
    sha = _seed(env)
    (env.ws / "report.md").write_text(MARKER, encoding="utf-8")
    _use_jev(monkeypatch)

    out = _assess(sha, "report.md", env.root)

    assert out["success"] is True
    assert out["advisory"] is True
    assert out["evidence"] is False
    assert out["score"]["score"] == 0.75
    assert "Not review acceptance" in out["notice"]

    lines = _store_lines(env.root)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == RECORD_KEYS
    assert record["contract_sha256"] == sha
    assert record["artifact"] == "report.md"
    assert record["advisory"] is True and record["evidence"] is False
    assert record["model"] == "jev-test"
    assert set(record["score"]) <= {"score", "confidence", "probabilities"}
    assert "legend" not in record["score"]
    blob = lines[0]
    assert MARKER not in blob
    assert OBJECTIVE not in blob
    assert "Run outcome must be" not in blob
    assert "Expected artifacts" not in blob


def test_state_only_success_records_null_artifact(env, monkeypatch):
    sha = _seed(env)
    _use_jev(monkeypatch)
    assert _assess(sha, None, env.root)["success"] is True
    record = json.loads(_store_lines(env.root)[0])
    assert record["artifact"] is None


@pytest.mark.parametrize("code", ["TIMEOUT", "AUTHENTICATION", "RATE_LIMITED", "NOT_CONFIGURED", "UNAVAILABLE"])
def test_jev_error_returns_bridge_code_and_writes_nothing(env, monkeypatch, code):
    sha = _seed(env)
    (env.ws / "report.md").write_text(MARKER, encoding="utf-8")
    _use_jev(monkeypatch, _error_result(code))
    out = _assess(sha, "report.md", env.root)
    assert out == {"success": False, "code": code}
    assert _store_lines(env.root) == []


def test_unexpected_jev_payload_is_unavailable_and_writes_nothing(env, monkeypatch):
    sha = _seed(env)
    bad = types.CallToolResult(
        content=[types.TextContent(type="text", text="{}")],
        structuredContent={"model": "jev-test", "answer": {"type": "score"}, "usage": {}},
    )
    _use_jev(monkeypatch, bad)
    assert _assess(sha, None, env.root) == {"success": False, "code": "UNAVAILABLE"}
    assert _store_lines(env.root) == []


def test_read_advisories_is_newest_first_filtered_and_bounded(env, monkeypatch):
    sha_a = _seed(env, task_id="review-task-a")
    sha_b = _seed(env, task_id="review-task-b")
    _use_jev(monkeypatch)
    for _ in range(3):
        assert _assess(sha_a, None, env.root)["success"] is True
    assert _assess(sha_b, None, env.root)["success"] is True

    everything = assist.read_advisories(env.root, limit=50)
    assert len(everything) == 4
    assert everything[0]["contract_sha256"] == sha_b  # newest first
    only_a = assist.read_advisories(env.root, contract_sha256=sha_a, limit=2)
    assert len(only_a) == 2
    assert all(item["contract_sha256"] == sha_a for item in only_a)
    assert assist.read_advisories(env.root, limit=0)  # clamped to >= 1


def test_read_advisories_skips_corrupt_and_non_advisory_lines(env, monkeypatch):
    path = assist.advisory_path(env.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "not json\n"
        + json.dumps({"contract_sha256": "a" * 64, "advisory": True, "evidence": True}) + "\n"
        + json.dumps({"contract_sha256": "b" * 64, "advisory": True, "evidence": False}) + "\n",
        encoding="utf-8",
    )
    records = assist.read_advisories(env.root)
    assert [item["contract_sha256"] for item in records] == ["b" * 64]


def test_read_advisories_missing_store_is_empty(tmp_path):
    assert assist.read_advisories(tmp_path / "fresh") == []


# ---------------------------------------------------------------------------
# Invariant: advisories are never evidence
# ---------------------------------------------------------------------------

_EVIDENCE_READERS = (
    "operator_contract.py",
    "operator_review.py",
    "operator_delegations.py",
    "operator_autopilot.py",
)


def test_evidence_modules_never_reference_advisory_store():
    for name in _EVIDENCE_READERS:
        text = (REPO / name).read_text(encoding="utf-8")
        assert "review_advisories" not in text, name
        assert "operator_review_assist" not in text, name
        assert "hermes_review_assess" not in text, name


def test_validation_is_identical_before_and_after_advisory_write(env, monkeypatch):
    sha = _seed(env)
    (env.ws / "report.md").write_text(MARKER, encoding="utf-8")
    contract_json = _contract_json(env)

    def verdict() -> dict:
        out = json.loads(contract_mod.hermes_contract_validate(contract_json, hermes_root=env.root))
        # Volatile envelope fields only; every verdict, check and evidence field must match.
        for key in ("generated_at", "trace_id", "audit_count"):
            out.pop(key, None)
        return out

    before = verdict()
    _use_jev(monkeypatch)
    assert _assess(sha, "report.md", env.root)["success"] is True
    assert _store_lines(env.root)
    after = verdict()

    assert before == after
    assert before["verdict"] != "SATISFIED"  # no observed run, so an advisory cannot satisfy it
    assert "advisory" not in json.dumps(after).lower()


def test_validation_manifest_is_identical_before_and_after_advisory_write(env, monkeypatch):
    sha = _seed(env)
    (env.ws / "report.md").write_text(MARKER, encoding="utf-8")
    # Use the delegation store's durable manifest, the same input reconcile uses.
    with delegations._connect(delegations._db_path(env.root), write=False) as db:
        row = db.execute("SELECT delegation_id FROM delegations WHERE contract_sha256=?", (sha,)).fetchone()
        stored = delegations._manifest_row(db, row["delegation_id"])

    def run() -> dict:
        out = contract_mod._validate_manifest_impl(stored, None, env.root)
        return {k: v for k, v in out.items() if k not in {"generated_at", "trace_id"}}

    before = run()
    _use_jev(monkeypatch)
    assert _assess(sha, "report.md", env.root)["success"] is True
    assert run() == before


def test_every_call_is_audited_without_a_contract_task_id(env, monkeypatch):
    sha = _seed(env)
    (env.ws / "report.md").write_text(MARKER, encoding="utf-8")
    _use_jev(monkeypatch)
    assert _assess(sha, "report.md", env.root)["success"] is True
    assert _assess(sha, "missing.md", env.root)["success"] is False

    records = [r for r in op.audit_tail(50) if r.get("tool") == "hermes_review_assess"]
    assert [r["success"] for r in records] == [True, False]
    for record in records:
        # The contract forbidden-action scan only reads records with the contract's task_id.
        assert not record.get("task_id")
        assert MARKER not in json.dumps(record)
