"""Hermes Agent runner backend — hermes-gpt delegation through native Pi/OpenCode.

Why this exists (P0 architectural correction, 2026-10-20)
---------------------------------------------------------

The native-rails migration produced two parallel ways of executing coding
agents.  The ``pi_rpc`` / ``opencode`` / ``omx`` built-in backends launch the
coding agents directly (``pi --mode rpc``, ``opencode run``) and reimplement
session supervision Hermes Agent already provides natively: persistent
sessions, resume/steer, question auto-answer, dead-client reopen, staged
handshakes, retry-session derivation.  That is the duplication this module
retires.

The corrected architecture:

    Autonomy / Conductor
      -> Hermes-GPT native Missions / Work Contracts / Delegations
        -> Hermes Agent ``delegate_session``  (persistent Pi RPC / OpenCode)
          -> Pi / OpenCode do the actual coding

This backend is the thin adapter at the middle seam: a Hermes-GPT native
Delegation is executed by *Hermes Agent*, not by a hermes-gpt-owned process
tree.  Session supervision, recovery, question handling, and lifecycle stay
Hermes Agent's property; Hermes-GPT keeps only contract identity, dispatch,
retry authorization, and independent completion verification.

Transport: Conductor's durable delegate-spool layout (crash-safe by
construction — atomic ``tmp + os.replace`` publish, attempt-id-keyed result
envelopes, ``done/`` retirement, JSONL progress stream).  The Hermes-side
spool server (``conductor-delegate-spool-server.service``) owns the
``delegate_session`` capability and drives the delegated turn.  The layout
writer here is deliberately self-contained (same grammar, same durability
discipline) so hermes-gpt does not import hermes-conductor: the spool layout
is the shared contract.

Completion doctrine: a Hermes Agent session-completion report is *never*
automatically Hermes-GPT's authoritative Work Contract completion.  The
backend only observes transport truth (terminal envelope, phase evidence);
contract validation stays with hermes-gpt's independent artifact checks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Default spool root — must match the deployed supervisor unit
#: (conductor-delegate-spool.service ``--spool``) and
#: conductor ``delegate_spool.DEFAULT_SPOOL``. Derived from the user's home
#: at call time (never a hardcoded absolute home path — package-hygiene
#: guard + portability); deployments that pin a different root set
#: ``HERMES_GPT_DELEGATE_SPOOL_ROOT``.
def _default_spool_root() -> Path:
    return Path.home() / ".hermes" / "conductor-delegate-spool"


DEFAULT_SPOOL_ROOT = _default_spool_root()

#: Optional override mirroring conductor's file:// endpoint spelling.
SPOOL_ROOT_ENV = "HERMES_GPT_DELEGATE_SPOOL_ROOT"

#: Attempt ids are opaque hex-ish tokens that join into filesystem paths
#: (path-traversal / collision guard; mirrors the spool layout's own rule).
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Bounded work-order size (same bound the spool layout enforces at send).
_MAX_PAYLOAD_BYTES = 256 * 1024

#: Coding backends the Hermes Agent runtime understands for one delegation.
#: The session binding is keyed by backend; a binding served on a different
#: backend would collapse two identities, so the mapping is fixed here.
_DELEGATE_BACKENDS = frozenset({"pi", "opencode"})

#: hermes-gpt contract backend names -> native delegate_session backend.
_BACKEND_BY_NAME = {
    "hermes_agent": "pi",
    "pi_rpc": "pi",
    "opencode": "opencode",
}

#: Optional routing tag so operators can see the corrected architecture in
#: availability/dispatch output.
_ARCH_TAG = "hermes-agent-delegation"


def _spool_root(hermes_root: Path | None = None) -> Path:
    raw = os.environ.get(SPOOL_ROOT_ENV, "")
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_SPOOL_ROOT


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """Durably create-or-replace ``path`` (same discipline as the spool layout:
    exclusive temp, payload fsync BEFORE rename, directory fsync AFTER)."""
    import secrets

    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: OSError | None = None
    for _ in range(3):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as exc:  # planted debris, not an accident
            last_error = exc
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return
    raise RuntimeError(f"could not atomically write {path} ({last_error})")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class HermesAgentBackend:
    """Execute a native Work Contract through Hermes Agent ``delegate_session``.

    Non-blocking by construction: ``dispatch`` durably publishes one
    work-order envelope and returns ``state=queued`` immediately; the
    Hermes-side supervisor (which owns ``delegate_session``) drives the
    delegated turn and writes the terminal result envelope.  Observations
    fold from the spool on later passes — identical doctrine to the
    fleet backend and to conductor's own non-blocking dispatch (b26ce6b).
    """

    name = "hermes_agent"

    # -- RunnerBackend protocol -------------------------------------------

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        root = _spool_root(hermes_root)
        pending = root / "pending"
        results = root / "results"
        if not (pending.is_dir() and results.is_dir()):
            return {
                "available": False,
                "reason": f"delegate spool not initialized at {root}",
            }
        return {
            "available": True,
            "reason": None,
            "transport": _ARCH_TAG,
            "spool_root": str(root),
            "backends": sorted(_DELEGATE_BACKENDS),
        }

    def dispatch(
        self,
        contract: dict[str, Any],
        *,
        confirm: bool,
        dry_run: bool,
        timeout: int,
        hermes_root: Path | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        execution = contract.get("execution") or {}
        options = execution.get("options") or {}
        backend_name = str(execution.get("backend") or self.name)
        native_backend = str(options.get("delegate") or _BACKEND_BY_NAME.get(backend_name, "pi")).strip().lower()
        if native_backend not in _DELEGATE_BACKENDS:
            raise ValueError(
                f"{self.name} execution.options.delegate must be one of "
                f"{sorted(_DELEGATE_BACKENDS)!r} (got {native_backend!r})"
            )

        workspaces = contract.get("allowed_scope", {}).get("workspaces") or []
        workspace = str(workspaces[0]) if workspaces else ""
        if not workspace:
            raise ValueError(f"{self.name} contracts require allowed_scope.workspaces[0]")
        workspace_path = Path(workspace).expanduser()
        if not workspace_path.is_dir():
            raise ValueError(f"authorized workspace does not exist: {workspace}")

        task_id = str(contract.get("task_id") or "")
        if not _ATTEMPT_ID_RE.fullmatch(task_id):
            raise ValueError(f"task_id is not a conforming attempt id: {task_id!r}")

        if dry_run:
            return {
                "success": True,
                "changed": False,
                "dry_run": True,
                "backend": self.name,
                "task_id": task_id,
                "state": "planned",
                "delegate_backend": native_backend,
                "workspace": str(workspace_path),
            }

        root = _spool_root(hermes_root)
        pending = root / "pending"
        results = root / "results"
        if not (pending.is_dir() and results.is_dir()):
            raise RuntimeError(
                f"delegate spool not initialized at {root}; start "
                "conductor-delegate-spool.service first"
            )
        result_path = results / f"{task_id}.json"
        envelope_path = pending / f"{task_id}.json"
        if result_path.exists():
            # Duplicate dispatch after a crash: the terminal envelope is
            # already on disk; fold it instead of re-queueing the work.
            observed = self._envelope_to_observation(task_id, result_path)
            if observed is None:
                # Corrupt or foreign terminal file (malformed JSON,
                # non-terminal status, or a different attempt_id inside):
                # fail closed — the transport is untrustworthy for this
                # attempt, and silently re-queueing over it could
                # double-execute the work order.
                raise RuntimeError(
                    f"terminal result envelope for {task_id} is non-conforming; "
                    "refusing to fold or re-queue (transport corruption)"
                )
            return {
                "success": True,
                "changed": False,
                "backend": self.name,
                "task_id": task_id,
                "state": observed.get("outcome") or "unknown",
                "detail": "terminal envelope already present; folded, not re-queued",
            }

        payload_timeout = float(options.get("timeout") or timeout or 900)
        payload = {
            "schema": 1,
            "interface": "delegate_session",
            "attempt_id": task_id,
            "run_id": str(options.get("run_id") or kwargs.get("run_id") or task_id),
            "action_id": str(options.get("action_id") or f"mission:{task_id}"),
            "intent": str(options.get("intent") or "mission_node"),
            "backend_hint": native_backend,
            "workspace_path": str(workspace_path),
            "phase_prompt": str(contract.get("objective") or ""),
            "timeout_seconds": payload_timeout,
            "attempt_seq": int(options.get("attempt_seq") or 1),
            # Session binding: the stable logical identity Hermes Agent keys
            # its persistent sessions on (run + role + backend).  hermes-gpt
            # supplies the mission-scoped identity; Hermes Agent owns the
            # native session that identity resolves to.
            "session_binding": {
                "run_id": str(options.get("run_id") or task_id),
                "role": str(options.get("role") or "primary"),
                "backend": native_backend,
                "affinity_key": str(options.get("affinity_key") or ""),
                "resume": bool(options.get("resume") or False),
            },
            "spool_path": str(root),
        }
        context_block = str(options.get("context") or "")
        if context_block:
            payload["action_payload"] = {"instruction": context_block}

        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(body) > _MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"work order for {task_id} exceeds {_MAX_PAYLOAD_BYTES} bytes"
            )
        if envelope_path.exists():
            # First-publish authority (mirrors conductor DelegateSessionSpool.send):
            # a pending envelope for this attempt is the in-flight work order.
            # Never rewrite it — its mtime is the first-publish timestamp the
            # consumer's ordering and recovery depend on. Identical payload →
            # idempotent re-dispatch (queued, unchanged); divergent payload →
            # the attempt id is being reused for different work: refuse.
            try:
                existing = envelope_path.read_bytes()
            except OSError as exc:
                raise RuntimeError(
                    f"could not read existing pending envelope for {task_id}: {exc}"
                ) from exc
            if existing != body:
                raise RuntimeError(
                    f"pending envelope for {task_id} exists with different content; "
                    "attempt ids are single-use (first publish is authority)"
                )
            return {
                "success": True,
                "changed": False,
                "dry_run": False,
                "backend": self.name,
                "task_id": task_id,
                "state": "queued",
                "delegate_backend": native_backend,
                "workspace": str(workspace_path),
                "transport": _ARCH_TAG,
                "detail": "pending envelope already present; first publish is authority",
            }
        _atomic_write_json(envelope_path, payload)
        return {
            "success": True,
            "changed": True,
            "dry_run": False,
            "backend": self.name,
            "task_id": task_id,
            "state": "queued",
            "delegate_backend": native_backend,
            "workspace": str(workspace_path),
            "transport": _ARCH_TAG,
        }

    def observed_runs(self, task_id: str, *, hermes_root: Path | None = None) -> list[dict[str, Any]]:
        if not _ATTEMPT_ID_RE.fullmatch(task_id or ""):
            return []
        root = _spool_root(hermes_root)
        record = self._envelope_to_observation(
            task_id, root / "results" / f"{task_id}.json"
        )
        if record is not None:
            return [record]
        # Still in flight? A durable pending envelope proves dispatch crossed
        # the boundary (outcome unknown, never dispatch unknown).
        envelope = root / "pending" / f"{task_id}.json"
        if envelope.is_file():
            meta = _read_json(envelope) or {}
            return [
                {
                    "task_id": task_id,
                    "status": "running",
                    "outcome": "",
                    "error": None,
                    "started_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(envelope.stat().st_mtime)
                    ),
                    "ended_at": None,
                    "scope": f"runner:{self.name}",
                    "delegate_backend": meta.get("backend_hint") or "",
                }
            ]
        return []

    def cancel(self, task_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]:
        if not _ATTEMPT_ID_RE.fullmatch(task_id or ""):
            return {"success": False, "code": "RUNNER_JOB_NOT_FOUND", "backend": self.name}
        root = _spool_root(hermes_root)
        result_path = root / "results" / f"{task_id}.json"
        if result_path.exists():
            record = self._envelope_to_observation(task_id, result_path)
            state = str((record or {}).get("outcome") or "")
            if state in {"succeeded", "failed"}:
                return {
                    "success": False,
                    "changed": False,
                    "proven_terminal": True,
                    "state": state,
                    "backend": self.name,
                }
        # Cancellation authorization is hermes-gpt's; the native session
        # lifecycle (stop/resume of the Pi/OpenCode conversation) is Hermes
        # Agent's.  The transport seam records the intent durably and the
        # terminal envelope remains the only completion authority — the
        # session itself is deliberately NOT reaped here.
        cancel_path = root / "pending" / f"{task_id}.cancel.json"
        _atomic_write_json(
            cancel_path,
            {"attempt_id": task_id, "requested_at": time.time(), "reason": "delegation_cancel"},
        )
        return {
            "success": True,
            "changed": True,
            "backend": self.name,
            "task_id": task_id,
            "state": "cancelling",
        }

    # -- envelope mapping ---------------------------------------------------

    def _envelope_to_observation(
        self, task_id: str, result_path: Path
    ) -> dict[str, Any] | None:
        """Map one terminal result envelope onto the shared observation shape.

        Deliberately transport-shaped: ``succeeded``/``failed`` describe the
        delegated turn, NOT contract completion.  Work Contract validation
        (artifacts, tests, review) stays hermes-gpt's independent check, so a
        session report can never self-satisfy a contract.
        """
        data = _read_json(result_path)
        if not data or data.get("attempt_id") != task_id:
            return None
        status = str(data.get("status") or "")
        if status not in {"succeeded", "failed"}:
            return None
        detail = str(data.get("detail") or "")
        return {
            "task_id": task_id,
            "status": "completed" if status == "succeeded" else "failed",
            "outcome": "completed" if status == "succeeded" else "failed",
            "error": None if status == "succeeded" else (detail or "delegated turn failed"),
            "started_at": None,
            "ended_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(result_path.stat().st_mtime)
            ),
            "scope": f"runner:{self.name}",
            "session_id": str(data.get("session_id") or ""),
            "session_binding_id": str(data.get("session_binding_id") or ""),
            "delegate_backend": str(data.get("backend") or ""),
        }


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r (using default %s)", name, raw, default)
        return default


#: Concurrent in-flight delegations this transport may have published but not
#: yet observed.  A bounded outstanding set keeps a wedged supervisor from
#: accumulating unbounded queued work; the Hermes-side server remains the
#: serve-order authority (its own pool bounds concurrency of execution).
MAX_OUTSTANDING = _env_int("HERMES_GPT_HERMES_AGENT_MAX_OUTSTANDING", 8)


def register(backend: "HermesAgentBackend | None" = None, *, replace: bool = True) -> None:
    """Register the Hermes Agent backend in the runner registry."""
    import operator_runners as op_runners

    op_runners.register_backend(backend or HermesAgentBackend(), replace=replace)
