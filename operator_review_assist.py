"""Advisory review triage through the Jev bridge (v0.15, default off).

``hermes_review_assess`` asks the pinned Jev adapter (via ``jev_bridge``) to
score how well a Work Contract's observable state fits its completion criteria.
The result is written to a separate advisory store and is **never evidence**:

- it is not Work Contract acceptance and never changes a validation verdict;
- ``operator_contract``, ``operator_review``, ``operator_delegations`` and
  Autopilot do not read ``missions/review_advisories.jsonl`` (tested);
- no objective, instructions, state or artifact text is persisted.

Gates: ``jev_bridge.enabled()`` (``HERMES_GPT_ENABLE_JEV=1``) AND
``HERMES_GPT_REVIEW_ASSIST=1``. Otherwise nothing is contacted or written.

Calls send the contract's completion criteria and, when requested, one bounded
artifact's text to TypeSafe. That may incur API charges.
"""

from __future__ import annotations

import codecs
import json
import math
import os
import re
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jev_bridge as bridge
import operator_contract as contract_mod
import operator_delegations as delegations
import operator_policy as op

REVIEW_ENV = "HERMES_GPT_REVIEW_ASSIST"
ADVISORY_RELPATH = Path("missions") / "review_advisories.jsonl"
ARTIFACT_MAX_BYTES = 12_000
STATE_MAX_CHARS = 16_000
INSTRUCTIONS_MAX_CHARS = 4_000
READ_MAX_BYTES = 1_048_576
RECORD_MAX_BYTES = 8_192
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_WRITE_LOCK = threading.Lock()

# Fixed ordered levels sent as jev_score criteria (weakest first).
SCORE_LEVELS: tuple[str, ...] = (
    "Does not meet the acceptance criteria",
    "Meets only a small part of the acceptance criteria",
    "Meets about half of the acceptance criteria",
    "Meets most of the acceptance criteria",
    "Fully meets the acceptance criteria",
)

_NOTICE = "Advisory only. Not review acceptance and not Work Contract evidence."
_MESSAGES = {
    "DISABLED": "Review assist is disabled. Requires HERMES_GPT_ENABLE_JEV=1 and HERMES_GPT_REVIEW_ASSIST=1.",
    "INVALID_INPUT": "contract_sha256 must be 64 lowercase hex characters.",
    "NOT_FOUND": "No dispatched Work Contract with that digest, or its artifact, is present.",
    "DENIED": "The requested artifact is not admissible for review.",
    "UNSUPPORTED_ARTIFACT": "The artifact is not a regular UTF-8 text file.",
}


class _Refusal(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def enabled() -> bool:
    """Both the Jev bridge gate and the review-assist gate must be on."""
    return bridge.enabled() and os.environ.get(REVIEW_ENV) == "1"


def advisory_path(hermes_root: Path | None = None) -> Path:
    return contract_mod._resolve_root(hermes_root) / ADVISORY_RELPATH


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...[truncated]"
    return text[: max(0, limit - len(marker))] + marker


def _load_contract(sha: str, root: Path) -> dict[str, Any]:
    """Read the contract by digest from its durable validation manifest.

    The delegation store is the only by-digest record. Its manifest is the
    prompt-free contract subset (no objective, inputs, constraints or review
    evidence text), so instructions are built from the durable criteria.
    """
    try:
        with delegations._connect(delegations._db_path(root), write=False) as db:
            row = db.execute(
                "SELECT delegation_id FROM delegations WHERE contract_sha256=? ORDER BY created_at LIMIT 1",
                (sha,),
            ).fetchone()
            if row is None:
                raise _Refusal("NOT_FOUND")
            manifest = delegations._manifest_row(db, row["delegation_id"])
        contract, found_sha = contract_mod._contract_from_validation_manifest(manifest)
    except _Refusal:
        raise
    except (OSError, sqlite3.Error, LookupError, ValueError, TypeError, KeyError, PermissionError):
        raise _Refusal("NOT_FOUND") from None
    if found_sha != sha:
        raise _Refusal("NOT_FOUND")
    return contract


def _lexical_relative(ws: Path, rel: str) -> Path:
    raw = Path(rel)
    candidate = raw if raw.is_absolute() else ws / raw
    try:
        relative = candidate.relative_to(ws)
    except ValueError:
        raise _Refusal("DENIED") from None
    if ".." in relative.parts or not relative.parts:
        raise _Refusal("DENIED")
    return relative


def _read_bounded_text(path: Path) -> tuple[str, bool]:
    """Read at most ARTIFACT_MAX_BYTES + 1 bytes from a regular, non-symlink file."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = os.lstat(path)
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise _Refusal("NOT_FOUND") from None
    except OSError:
        raise _Refusal("UNSUPPORTED_ARTIFACT") from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise _Refusal("UNSUPPORTED_ARTIFACT")
        data = bytearray()
        while len(data) <= ARTIFACT_MAX_BYTES:
            chunk = os.read(fd, min(8192, ARTIFACT_MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    except OSError:
        raise _Refusal("UNSUPPORTED_ARTIFACT") from None
    finally:
        os.close(fd)
    truncated = len(data) > ARTIFACT_MAX_BYTES
    try:
        text = codecs.getincrementaldecoder("utf-8")("strict").decode(bytes(data[:ARTIFACT_MAX_BYTES]), final=not truncated)
    except UnicodeDecodeError:
        raise _Refusal("UNSUPPORTED_ARTIFACT") from None
    if "\x00" in text:
        raise _Refusal("UNSUPPORTED_ARTIFACT")
    # Same secret-looking-content shapes the Operator redacts from its output.
    if op.redact_output(text) != text:
        raise _Refusal("DENIED")
    return text, truncated


def _read_artifact(contract: dict[str, Any], name: str) -> tuple[str, bool]:
    """Resolve an expected artifact by basename with the validator's location logic."""
    # Anchor the name-only check so the process cwd cannot affect it.
    if op.is_denied_path(Path(os.sep + name)):
        raise _Refusal("DENIED")
    matches = [item for item in contract["expected_artifacts"] if Path(item["path"]).name == name]
    if len(matches) != 1:
        raise _Refusal("DENIED")
    expected = matches[0]["path"]
    for ws_text in contract["allowed_scope"]["workspaces"]:
        ws = Path(ws_text)
        relative = _lexical_relative(ws, expected)
        current = ws
        missing = False
        for part in relative.parts:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                missing = True
                break
            if stat.S_ISLNK(info.st_mode):
                raise _Refusal("DENIED")
        if missing:
            continue
        try:
            resolved = contract_mod._resolve_artifact_paths(expected, [ws])[0]
        except (ValueError, PermissionError):
            raise _Refusal("DENIED") from None
        if resolved != current.resolve() or op.is_denied_path(resolved):
            raise _Refusal("DENIED")
        return _read_bounded_text(current)
    raise _Refusal("NOT_FOUND")


def _instructions(contract: dict[str, Any]) -> str:
    criteria = contract["completion_criteria"]
    review = contract["review_requirements"]
    lines = []
    objective = str(contract.get("objective") or "").strip()
    if objective:
        lines.append("Objective: " + objective)
    lines.append("Run outcome must be one of: " + ", ".join(criteria["run_state"]["outcome_ok"]) + ".")
    lines.append("Required artifacts present: " + ("yes" if criteria["artifacts_present"] else "no") + ".")
    lines.append("Tests required to pass: " + ("yes" if criteria["tests_pass"] else "no") + ".")
    lines.append("Review required: " + ("yes" if review.get("required") else "no") + ".")
    lines.append("Review satisfied: " + ("yes" if criteria["review_satisfied"] else "no") + ".")
    lines.append("No forbidden actions: " + ("yes" if criteria["no_forbidden_actions"] else "no") + ".")
    lines.append("Judge only the supplied state. This is advisory and does not approve the work.")
    return _bounded("\n".join(lines), INSTRUCTIONS_MAX_CHARS)


def _state(contract: dict[str, Any], artifact: str | None, artifact_text: str | None, truncated: bool) -> str:
    names = ", ".join(item["path"] for item in contract["expected_artifacts"]) or "(none)"
    header = "Expected artifacts: " + _bounded(names, 2_000)
    if artifact is None or artifact_text is None:
        return _bounded(header, STATE_MAX_CHARS)
    suffix = " (truncated to the first 12000 bytes)" if truncated else ""
    prefix = f"{header}\n\nArtifact {artifact}{suffix}:\n"
    return prefix + artifact_text[: max(0, STATE_MAX_CHARS - len(prefix))]


def _score_from(answer: Any) -> dict[str, Any] | None:
    if not isinstance(answer, dict):
        return None
    score = answer.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        return None
    out: dict[str, Any] = {"score": float(score)}
    confidence = answer.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and math.isfinite(confidence):
        out["confidence"] = float(confidence)
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict) and len(probabilities) <= 32:
        clean = {
            str(key)[:32]: float(value)
            for key, value in probabilities.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        }
        if len(clean) == len(probabilities):
            out["probabilities"] = clean
    return out


def _append_record(root: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n"
    if len(line.encode("utf-8")) > RECORD_MAX_BYTES:
        raise _Refusal("UNSUPPORTED_ARTIFACT")
    path = advisory_path(root)
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def _payload(code: str) -> str:
    return json.dumps({"success": False, "code": code, "message": _MESSAGES.get(code, "")}, ensure_ascii=False, indent=2)


def _audit(contract_sha256: Any, artifact: Any, result: str) -> None:
    """Record the call without a ``task_id`` so the contract forbidden-action
    scan (which only reads records carrying the contract's task_id) ignores it."""
    try:
        outcome = json.loads(result)
        sha = contract_sha256 if isinstance(contract_sha256, str) and _SHA_RE.fullmatch(contract_sha256) else "invalid"
        op.audit_record(
            tool="hermes_review_assess",
            level=op.OperatorPolicy().level or "read_only",
            apply_mode="read_only",
            dry_run=True,
            success=bool(outcome.get("success")),
            changed=False,
            summary=f"advisory contract={sha[:16]} artifact={'yes' if artifact else 'no'} code={outcome.get('code', 'OK')}",
        )
    except Exception:  # noqa: BLE001 - audit must never break the advisory call
        pass


async def hermes_review_assess(
    contract_sha256: str,
    artifact: str | None = None,
    hermes_root: Path | None = None,
) -> str:
    """Advisory Jev score of a Work Contract's state; never evidence or acceptance.

    Sends the contract's completion criteria, and the bounded text of one
    expected artifact when ``artifact`` (a basename) is given, to TypeSafe
    (may incur API charges). Requires the read_only Operator level.
    """
    if not enabled():
        return _payload("DISABLED")
    try:
        op.OperatorPolicy().require_level("read_only")
    except PermissionError:
        result = _payload("DENIED")
    else:
        result = await _assess(contract_sha256, artifact, hermes_root)
    _audit(contract_sha256, artifact, result)
    return result


async def _assess(contract_sha256: Any, artifact: Any, hermes_root: Path | None) -> str:
    try:
        if not isinstance(contract_sha256, str) or not _SHA_RE.fullmatch(contract_sha256):
            raise _Refusal("INVALID_INPUT")
        root = contract_mod._resolve_root(hermes_root)
        contract = _load_contract(contract_sha256, root)
        artifact_text, truncated = (None, False)
        if artifact is not None:
            if not isinstance(artifact, str) or not artifact or "/" in artifact or "\\" in artifact:
                raise _Refusal("DENIED")
            artifact_text, truncated = _read_artifact(contract, artifact)
        state = _state(contract, artifact, artifact_text, truncated)
        instructions = _instructions(contract)
    except _Refusal as refusal:
        return _payload(refusal.code)

    criteria = list(SCORE_LEVELS)
    result = await bridge.call("jev_score", {"state": state, "instructions": instructions, "criteria": criteria})
    raw = result.model_dump(by_alias=True, exclude_none=True)
    if raw.get("isError"):
        code = "UNAVAILABLE"
        try:
            candidate = json.loads(raw["content"][0]["text"]).get("code")
            if candidate in bridge._MESSAGES:
                code = candidate
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            pass
        return json.dumps({"success": False, "code": code}, ensure_ascii=False, indent=2)

    data = raw.get("structuredContent")
    score = _score_from(data.get("answer")) if isinstance(data, dict) else None
    if score is None:
        return json.dumps({"success": False, "code": "UNAVAILABLE"}, ensure_ascii=False, indent=2)
    model = str(data.get("model") or "")[:100]
    record = {
        "contract_sha256": contract_sha256,
        "artifact": artifact,
        "score": score,
        "model": model,
        "assessed_at": datetime.now(timezone.utc).isoformat(),
        "advisory": True,
        "evidence": False,
    }
    try:
        _append_record(root, record)
    except (OSError, _Refusal):
        return json.dumps({"success": False, "code": "UNAVAILABLE"}, ensure_ascii=False, indent=2)
    return json.dumps(
        {"success": True, "advisory": True, "evidence": False, "score": score, "notice": _NOTICE},
        ensure_ascii=False,
        indent=2,
    )


def read_advisories(
    hermes_root: Path | None,
    contract_sha256: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Newest-first advisory records from the bounded tail of the store."""
    maximum = max(1, min(int(limit), 200))
    path = advisory_path(hermes_root)
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - READ_MAX_BYTES)
            fh.seek(start)
            data = fh.read(size - start)
    except OSError:
        return []
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]  # first line may be a partial record
    out: list[dict[str, Any]] = []
    for raw_line in reversed(lines):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("advisory") is not True or record.get("evidence") is not False:
            continue
        if contract_sha256 is not None and record.get("contract_sha256") != contract_sha256:
            continue
        out.append(record)
        if len(out) >= maximum:
            break
    return out
