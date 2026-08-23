"""Bounded ChatGPT -> Hermes Finance broker.

The broker validates a minimal ``finance.evidence/v1`` packet, refuses obvious
credential-bearing payloads, and runs the local ``finance`` profile in an
isolated child process with persistence disabled by ``finance_worker``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import operator_policy as op


ENABLE_FINANCE_ENV = "HERMES_GPT_ENABLE_FINANCE"
FINANCE_PROFILE = "finance"
EVIDENCE_SCHEMA = "finance.evidence/v1"
DECISION_SCHEMA = "finance.decision/v1"
MAX_EVIDENCE_CHARS = 48_000
MAX_RESULT_CHARS = 32_000
MIN_TIMEOUT = 10
MAX_TIMEOUT = 300

_SECRET_KEY_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key|credential|client[_-]?secret|access[_-]?key|refresh[_-]?token)"
)
_PRIVATE_MATERIAL_RE = re.compile(
    r"(?i)(?:^\s*bearer\s+|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)"
)


def _error(code: str, message: str, action: str) -> str:
    payload = op.make_error_envelope(
        layer="finance",
        code=code,
        safe_message=message,
        suggested_action=action,
    )
    return json.dumps(payload, ensure_ascii=False)


def _walk_sensitive(value: Any, path: tuple[str, ...] = ()) -> str | None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            if _SECRET_KEY_RE.search(key):
                return ".".join((*path, key)) or key
            hit = _walk_sensitive(item, (*path, key))
            if hit:
                return hit
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            hit = _walk_sensitive(item, (*path, str(index)))
            if hit:
                return hit
        return None
    if isinstance(value, str) and _PRIVATE_MATERIAL_RE.search(value):
        return ".".join(path) or "value"
    return None


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_evidence(evidence_json: str) -> tuple[dict[str, Any], str] | str:
    if not isinstance(evidence_json, str) or not evidence_json.strip():
        return _error(
            "INVALID_EVIDENCE",
            "Finance evidence must be a non-empty JSON string.",
            "Provide one bounded finance.evidence/v1 packet.",
        )
    if len(evidence_json) > MAX_EVIDENCE_CHARS:
        return _error(
            "EVIDENCE_TOO_LARGE",
            f"Finance evidence exceeds the {MAX_EVIDENCE_CHARS}-character limit.",
            "Send only the minimum normalized facts required for the decision.",
        )
    try:
        payload = json.loads(evidence_json)
    except ValueError:
        return _error(
            "INVALID_EVIDENCE_JSON",
            "Finance evidence is not valid JSON.",
            "Send a valid finance.evidence/v1 JSON object.",
        )
    if not isinstance(payload, dict):
        return _error(
            "INVALID_EVIDENCE",
            "Finance evidence must be a JSON object.",
            "Send a finance.evidence/v1 object.",
        )

    required = {
        "schema",
        "request_id",
        "intent",
        "as_of",
        "coverage",
        "facts",
        "ambiguities",
        "assumptions",
        "quality_flags",
        "sensitivity",
    }
    if not required.issubset(payload):
        return _error(
            "INCOMPLETE_EVIDENCE",
            "Finance evidence is missing required fields.",
            "Include schema, request_id, intent, as_of, coverage, facts, ambiguities, assumptions, quality_flags, and sensitivity.",
        )
    if payload.get("schema") != EVIDENCE_SCHEMA:
        return _error(
            "INVALID_EVIDENCE_SCHEMA",
            "Unsupported finance evidence schema.",
            f"Use {EVIDENCE_SCHEMA}.",
        )
    request_id = payload.get("request_id")
    intent = payload.get("intent")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        return _error(
            "INVALID_REQUEST_ID",
            "Finance request_id must contain 1 to 128 characters.",
            "Provide a bounded opaque request identifier.",
        )
    if not isinstance(intent, str) or not intent.strip() or len(intent) > 160:
        return _error(
            "INVALID_INTENT",
            "Finance intent must contain 1 to 160 characters.",
            "Provide a concise decision or analysis intent.",
        )
    if not isinstance(payload.get("as_of"), str) or not payload["as_of"].strip():
        return _error(
            "INVALID_AS_OF",
            "Finance as_of must be a non-empty timestamp string.",
            "Include the Finances data as-of time.",
        )
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        return _error(
            "INVALID_COVERAGE",
            "Finance coverage must be an object.",
            "Include coverage.status and any relevant period limitations.",
        )
    if coverage.get("status") not in {"complete", "partial", "recent_only", "stale", "unknown"}:
        return _error(
            "INVALID_COVERAGE_STATUS",
            "Finance coverage.status is invalid.",
            "Use complete, partial, recent_only, stale, or unknown.",
        )
    if not isinstance(payload.get("facts"), dict):
        return _error(
            "INVALID_FACTS",
            "Finance facts must be an object.",
            "Pass only normalized decision-relevant facts.",
        )
    for key in ("ambiguities", "assumptions", "quality_flags"):
        if not _is_str_list(payload.get(key)):
            return _error(
                "INVALID_EVIDENCE_LIST",
                f"Finance {key} must be a list of strings.",
                "Normalize the evidence packet before dispatch.",
            )
    if payload.get("sensitivity") != "financial-confidential":
        return _error(
            "INVALID_SENSITIVITY",
            "Finance evidence must be explicitly marked financial-confidential.",
            "Set sensitivity to financial-confidential.",
        )

    sensitive_path = _walk_sensitive(payload)
    if sensitive_path:
        return _error(
            "SENSITIVE_MATERIAL_REJECTED",
            "Finance evidence contains credential-like or authorization material.",
            "Remove credentials and send only normalized financial facts.",
        )

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(canonical) > MAX_EVIDENCE_CHARS:
        return _error(
            "EVIDENCE_TOO_LARGE",
            f"Finance evidence exceeds the {MAX_EVIDENCE_CHARS}-character limit after normalization.",
            "Send only the minimum normalized facts required for the decision.",
        )
    return payload, canonical


def _validate_decision(raw: str, request_id: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return _error(
            "EMPTY_FINANCE_RESULT",
            "Finance returned no decision packet.",
            "Check the Finance profile model/provider and retry.",
        )
    if len(raw) > MAX_RESULT_CHARS:
        return _error(
            "FINANCE_RESULT_TOO_LARGE",
            "Finance returned an oversized decision packet.",
            "Reduce the evidence scope or tighten the requested analysis.",
        )
    try:
        payload = json.loads(raw)
    except ValueError:
        return _error(
            "INVALID_FINANCE_RESULT",
            "Finance returned malformed output instead of a decision packet.",
            "Retry after checking the Finance profile and model behavior.",
        )
    if not isinstance(payload, dict) or payload.get("schema") != DECISION_SCHEMA:
        return _error(
            "INVALID_FINANCE_RESULT_SCHEMA",
            "Finance returned an unsupported decision schema.",
            f"Finance must return {DECISION_SCHEMA}.",
        )
    if payload.get("request_id") != request_id:
        return _error(
            "FINANCE_REQUEST_MISMATCH",
            "Finance returned a decision for a different request identifier.",
            "Discard the result and retry the bounded request.",
        )
    verdict = payload.get("verdict")
    if not isinstance(verdict, dict):
        return _error("INVALID_FINANCE_RESULT", "Finance decision is missing verdict.", "Retry the bounded request.")
    if not isinstance(verdict.get("summary"), str) or verdict.get("confidence") not in {"high", "medium", "low"}:
        return _error("INVALID_FINANCE_RESULT", "Finance verdict is malformed.", "Retry the bounded request.")
    if not isinstance(payload.get("current_state"), str):
        return _error("INVALID_FINANCE_RESULT", "Finance current_state is malformed.", "Retry the bounded request.")
    if not isinstance(payload.get("options"), list):
        return _error("INVALID_FINANCE_RESULT", "Finance options are malformed.", "Retry the bounded request.")
    if not isinstance(payload.get("recommendation"), dict):
        return _error("INVALID_FINANCE_RESULT", "Finance recommendation is malformed.", "Retry the bounded request.")
    for key in ("uncertainties", "next_actions", "approval_required"):
        if not _is_str_list(payload.get(key)):
            return _error("INVALID_FINANCE_RESULT", f"Finance {key} is malformed.", "Retry the bounded request.")
    if not isinstance(payload.get("specialist_review"), dict):
        return _error("INVALID_FINANCE_RESULT", "Finance specialist_review is malformed.", "Retry the bounded request.")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def hermes_finance_analyze(
    evidence_json: str,
    timeout: int = 120,
    *,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
) -> str:
    """Analyze one bounded Finances evidence packet with the local Finance profile."""
    if not op.env_truthy(ENABLE_FINANCE_ENV):
        return _error(
            "FINANCE_DISABLED",
            "Hermes Finance analysis is disabled.",
            f"Set {ENABLE_FINANCE_ENV}=1 on the trusted local MCP server after Finance QA passes.",
        )

    checked = _validate_evidence(evidence_json)
    if isinstance(checked, str):
        return checked
    payload, canonical = checked

    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return _error(
            "INVALID_TIMEOUT",
            "Finance timeout must be an integer number of seconds.",
            f"Choose {MIN_TIMEOUT} to {MAX_TIMEOUT} seconds.",
        )
    safe_timeout = max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT))

    root = op.normalize_hermes_data_root(hermes_root or Path.home() / ".hermes")
    data_root = Path(root or Path.home() / ".hermes")
    profile_home = data_root / "profiles" / FINANCE_PROFILE
    worker = Path(__file__).resolve().with_name("finance_worker.py")
    resolved_agent_root = Path(agent_root).resolve() if agent_root is not None else None

    if not profile_home.is_dir() or not (profile_home / "SOUL.md").is_file():
        return _error(
            "FINANCE_PROFILE_UNAVAILABLE",
            "The local Finance profile is unavailable.",
            "Create and verify the finance profile before enabling the broker.",
        )
    if resolved_agent_root is None or not resolved_agent_root.is_dir() or not worker.is_file():
        return _error(
            "FINANCE_RUNTIME_UNAVAILABLE",
            "The Finance runtime is unavailable.",
            "Check the Hermes Agent source root and hermes-gpt installation.",
        )

    venv_python = resolved_agent_root / "venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    worker_python = str(venv_python) if venv_python.is_file() else sys.executable
    argv = [worker_python, str(worker), str(resolved_agent_root), str(profile_home)]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(profile_home)
    env["HERMES_PROFILE"] = FINANCE_PROFILE

    try:
        completed = subprocess.run(
            argv,
            input=canonical,
            text=True,
            capture_output=True,
            shell=False,
            timeout=safe_timeout,
            cwd=str(profile_home),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return _error(
            "FINANCE_TIMEOUT",
            "Finance analysis exceeded the bounded timeout.",
            "Retry with a smaller evidence packet or check provider health.",
        )
    except (OSError, ValueError):
        return _error(
            "FINANCE_START_FAILED",
            "Finance analysis could not start.",
            "Check the local Finance profile and Hermes runtime.",
        )

    if completed.returncode != 0:
        return _error(
            "FINANCE_WORKER_FAILED",
            "Finance analysis failed inside the isolated worker.",
            "Check the Finance profile model/provider and retry.",
        )

    result = _validate_decision(completed.stdout, str(payload["request_id"]))

    # Metadata-only audit. Never persist the evidence, facts, model output, or
    # caller text. The input hash supports correlation without reconstructing it.
    try:
        policy = op.OperatorPolicy()
        op.audit_record(
            tool="hermes_finance_analyze",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=not json.loads(result).get("error", False) if result.startswith("{") else True,
            changed=False,
            summary="bounded finance analysis",
            extra={
                "request_id_sha256": hashlib.sha256(str(payload["request_id"]).encode("utf-8")).hexdigest(),
                "evidence_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "evidence_chars": len(canonical),
            },
        )
    except Exception:
        pass

    return result
