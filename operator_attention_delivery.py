"""Default-off attention delivery relay: send NEW Owner-inbox items to a configured target.

This is a notification relay, not a control channel. Messages carry a fixed
header, the item kind, the mission id, the already-redacted inbox summary, the
name of the existing tool that could act on it, and an optional link to the
Flight Deck Owner inbox. Messages never contain approvals, commands, or replies
that change state; replying to a message does nothing.

It holds no platform credentials. Delivery reuses existing tools as child
processes with a fixed argv, ``shell=False`` and a minimal environment:

- ``hermes``: ``hermes send`` (Hermes Agent's own platform credentials).
- ``imessage``: ``imsg rpc`` JSON-RPC ``send`` (Messages must be signed in).

Configuration is administrator environment only (never tool arguments):
``HERMES_GPT_ATTENTION_DELIVERY=1`` enables the relay; otherwise every entry point
is a no-op. See ``docs/attention-delivery.md``.

Delivery state lives in ``missions/attention_delivery_state.json`` (atomic
replace writes, single-writer lock). An item is marked delivered only after its
backend reports success. Failures back off per target and are recorded as bounded
error codes only.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import operator_mission_runtime as missions
import operator_owner_inbox as owner_inbox
import operator_policy as op
import ui_security

try:  # POSIX advisory lock
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows byte-range lock
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

ENABLE_ENV = "HERMES_GPT_ATTENTION_DELIVERY"
TARGETS_ENV = "HERMES_GPT_ATTENTION_TARGETS"
HERMES_CLI_ENV = "HERMES_GPT_HERMES_CLI"
IMSG_CLI_ENV = "HERMES_GPT_IMSG_CLI"
LINK_BASE_ENV = "HERMES_GPT_ATTENTION_LINK_BASE"
MAX_PER_RUN_ENV = "HERMES_GPT_ATTENTION_MAX_PER_RUN"

DEFAULT_MAX_PER_RUN = 5
MAX_PER_RUN_LIMIT = 20
MAX_TARGETS = 8
BACKENDS = ("hermes", "imessage")
HERMES_TARGET_RE = re.compile(r"[a-z_]+(:[A-Za-z0-9#@+._:-]{1,200})?")
PHONE_RE = re.compile(r"\+[1-9][0-9]{6,14}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
LINK_BASE_RE = re.compile(r"https://[A-Za-z0-9._~:/%-]{1,280}")
ITEM_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")

CHILD_TIMEOUT_SECONDS = 20.0
OUTPUT_CAP_BYTES = 64 * 1024
MESSAGE_MAX_CHARS = 1000
HEADER = "Hermes GPT needs you"
UNAVAILABLE_LINE = "Some sources unavailable"
INBOX_LIMIT = 200
STATE_FILE = "attention_delivery_state.json"
LOCK_FILE = "attention_delivery.lock"
STATE_SCHEMA = 1
RETENTION_SECONDS = 14 * 24 * 3600
RETENTION_MAX_PER_TARGET = 2000
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
INTERVAL_MIN = 15
INTERVAL_MAX = 3600
INTERVAL_DEFAULT = 60
RPC_INIT_ID = 1
RPC_SEND_ID = 2

ERR_CONFIG = "CONFIGURATION"
ERR_TIMEOUT = "TIMEOUT"
ERR_BAD_RESPONSE = "BAD_RESPONSE"
ERR_NOT_CONFIGURED = "NOT_CONFIGURED"
ERR_SPAWN_FAILED = "SPAWN_FAILED"
ERR_STATE = "STATE_UNREADABLE"
ERR_STATE_WRITE = "STATE_WRITE_FAILED"
# Sent, but the backend could not confirm delivery. Recorded as delivered (at most
# once): retrying could duplicate a message that already arrived.
WARN_UNVERIFIED = "SENT_UNVERIFIED"

KIND_LABELS = {
    "mission_awaiting_approval": "Mission awaiting approval",
    "autopilot_waiting_owner": "Autopilot waiting for Owner",
    "budget_breaker": "Budget breaker",
    "red_health": "Health attention (RED)",
    "no_capable_target": "No capable target",
}

# Extra secret shapes the shared redactors do not cover.
_EXTRA_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghs|xox[abprs])[-_][A-Za-z0-9_-]{8,}"), "[REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[:=]\s*)\S+"), r"\1\2[REDACTED]"),
)


class AttentionConfigError(ValueError):
    """Invalid administrator configuration. Messages never echo configured values."""

    code = ERR_CONFIG


class _SpawnFailed(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _StateError(Exception):
    """Delivery state exists but cannot be trusted; nothing is sent over it."""


@dataclass(frozen=True)
class Target:
    backend: str
    target: str

    @property
    def key(self) -> str:
        return f"{self.backend}:{self.target}"


@dataclass(frozen=True)
class AttentionConfig:
    targets: tuple[Target, ...]
    hermes_cli: str | None
    imsg_cli: str | None
    link_base: str | None
    max_per_run: int


@dataclass(frozen=True)
class ChildOutcome:
    returncode: int | None
    stdout: bytes
    timed_out: bool = False
    overflow: bool = False


Runner = Callable[..., ChildOutcome]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def enabled(environ: Any = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENABLE_ENV) == "1"


def _valid_email(value: str) -> bool:
    return len(value) <= 254 and EMAIL_RE.fullmatch(value) is not None


def _parse_targets(raw: str) -> tuple[Target, ...]:
    if not raw.strip():
        raise AttentionConfigError(f"{TARGETS_ENV} must list 1 to {MAX_TARGETS} backend:target entries")
    parts = raw.split(",")
    if len(parts) > MAX_TARGETS:
        raise AttentionConfigError(f"{TARGETS_ENV} allows at most {MAX_TARGETS} entries")
    targets: list[Target] = []
    seen: set[str] = set()
    for index, part in enumerate(parts, start=1):
        entry = part.strip()
        backend, sep, value = entry.partition(":")
        if not entry or not sep or not value:
            raise AttentionConfigError(f"{TARGETS_ENV} entry {index} must be backend:target")
        if backend not in BACKENDS:
            raise AttentionConfigError(f"{TARGETS_ENV} entry {index} uses an unsupported backend")
        if backend == "hermes":
            valid = HERMES_TARGET_RE.fullmatch(value) is not None
        else:
            valid = PHONE_RE.fullmatch(value) is not None or _valid_email(value)
        if not valid:
            raise AttentionConfigError(f"{TARGETS_ENV} entry {index} has an invalid {backend} target")
        target = Target(backend, value)
        if target.key in seen:
            raise AttentionConfigError(f"{TARGETS_ENV} entry {index} is a duplicate")
        seen.add(target.key)
        targets.append(target)
    return tuple(targets)


def _require_exe(environ: Any, name: str) -> str:
    value = (environ.get(name) or "").strip()
    if not value or not os.path.isabs(value):
        raise AttentionConfigError(f"{name} must be an absolute path to an executable file")
    if not Path(value).is_file() or not os.access(value, os.X_OK):
        raise AttentionConfigError(f"{name} must point to an existing executable regular file")
    return value


def _parse_link_base(raw: str) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    value = value.rstrip("/")
    if LINK_BASE_RE.fullmatch(value) is None:
        raise AttentionConfigError(f"{LINK_BASE_ENV} must be an https:// base URL without query or spaces")
    return value


def _parse_max_per_run(raw: str) -> int:
    value = (raw or "").strip()
    if not value:
        return DEFAULT_MAX_PER_RUN
    if not re.fullmatch(r"[0-9]{1,2}", value) or not 1 <= int(value) <= MAX_PER_RUN_LIMIT:
        raise AttentionConfigError(f"{MAX_PER_RUN_ENV} must be an integer from 1 to {MAX_PER_RUN_LIMIT}")
    return int(value)


def load_config(environ: Any = None) -> AttentionConfig:
    """Validate administrator configuration. Raises AttentionConfigError (fail closed)."""
    env = os.environ if environ is None else environ
    targets = _parse_targets(env.get(TARGETS_ENV, "") or "")
    backends = {target.backend for target in targets}
    return AttentionConfig(
        targets=targets,
        hermes_cli=_require_exe(env, HERMES_CLI_ENV) if "hermes" in backends else None,
        imsg_cli=_require_exe(env, IMSG_CLI_ENV) if "imessage" in backends else None,
        link_base=_parse_link_base(env.get(LINK_BASE_ENV, "") or ""),
        max_per_run=_parse_max_per_run(env.get(MAX_PER_RUN_ENV, "") or ""),
    )


def mask_target(backend: str, target: str) -> str:
    """Display form of a target: phone ``+1•••4567``, email ``a•••@domain``."""
    if backend == "imessage":
        return f"imessage:{_mask_value(target)}"
    platform, _, rest = target.partition(":")
    if not rest:
        return f"hermes:{platform}"
    return f"hermes:{platform}:{_mask_value(rest)}"


def _mask_value(value: str) -> str:
    if PHONE_RE.fullmatch(value):
        return f"{value[:2]}•••{value[-4:]}"
    if _valid_email(value):
        local, _, domain = value.partition("@")
        return f"{local[:1]}•••@{domain}"
    if len(value) <= 6:
        return value
    return f"{value[:2]}•••{value[-2:]}"


# ---------------------------------------------------------------------------
# Time, state and locking
# ---------------------------------------------------------------------------


def _to_epoch(now: Any) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).timestamp()
    return float(now)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _sort_epoch(value: Any) -> float:
    parsed = _parse_iso(value)
    return parsed if parsed is not None else float("-inf")


def _data_dir(root: Path) -> Path:
    return missions._root(root) / "missions"  # noqa: SLF001 - same resolution as the Mission runtime


def _state_path(root: Path) -> Path:
    return _data_dir(root) / STATE_FILE


def _empty_state() -> dict[str, Any]:
    return {"schema_version": STATE_SCHEMA, "targets": {}}


def _load_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state()
    except (OSError, UnicodeDecodeError):
        raise _StateError("state unreadable") from None
    if len(raw) > 8 * 1024 * 1024:
        raise _StateError("state too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise _StateError("state is not JSON") from None
    if not isinstance(data, dict) or data.get("schema_version") != STATE_SCHEMA or not isinstance(data.get("targets"), dict):
        raise _StateError("state schema mismatch")
    for entry in data["targets"].values():
        if not isinstance(entry, dict) or not isinstance(entry.get("delivered", {}), dict):
            raise _StateError("state target entry is malformed")
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic replace: write a temp file in the same directory, fsync, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".attention_delivery.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _prune(state: dict[str, Any], epoch: float) -> None:
    cutoff = epoch - RETENTION_SECONDS
    for entry in state["targets"].values():
        kept = {item_id: ts for item_id, ts in entry["delivered"].items() if _sort_epoch(ts) >= cutoff}
        if len(kept) > RETENTION_MAX_PER_TARGET:
            newest = sorted(kept.items(), key=lambda pair: _sort_epoch(pair[1]), reverse=True)
            kept = dict(newest[:RETENTION_MAX_PER_TARGET])
        entry["delivered"] = kept


def _target_entry(state: dict[str, Any], key: str) -> dict[str, Any]:
    entry = state["targets"].get(key)
    if entry is None:
        entry = {
            "delivered": {},
            "last_success_at": None,
            "last_error": None,
            "failure_count": 0,
            "next_attempt_at": None,
        }
        state["targets"][key] = entry
    return entry


def _try_lock(handle: Any) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:  # pragma: no cover - Windows
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return True


def _unlock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _single_writer(root: Path) -> Iterator[bool]:
    """Yield True when this process holds the relay lock (one writer per state file)."""
    path = _data_dir(root) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        acquired = _try_lock(handle)
        try:
            yield acquired
        finally:
            if acquired:
                _unlock(handle)
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# Inbox selection and message text
# ---------------------------------------------------------------------------


def _identify(items: list[Any], epoch: float) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep items that can be deduplicated; count the ones skipped and why."""
    skipped = {"missing_item_id": 0, "acknowledged": 0, "stale": 0}
    cutoff = epoch - RETENTION_SECONDS
    identified: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            skipped["missing_item_id"] += 1
            continue
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or ITEM_ID_RE.fullmatch(item_id) is None:
            skipped["missing_item_id"] += 1
            continue
        if item.get("acknowledged") is True:
            skipped["acknowledged"] += 1
            continue
        since = _parse_iso(item.get("since"))
        if since is not None and since < cutoff:
            # Older than the delivery-state retention window: pruned records could
            # otherwise make a stale item look new again.
            skipped["stale"] += 1
            continue
        identified.append(item)
    return identified, skipped


def _redact_line(value: Any) -> str:
    text = str(ui_security.redact_browser(str(value or "")))
    for pattern, replacement in _EXTRA_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return op.redact_output(text)


def _clean(value: Any) -> str:
    """Strip MEDIA: lines and control characters; return non-empty lines only."""
    kept: list[str] = []
    for raw_line in str(value or "").replace("\r", "\n").split("\n"):
        if re.match(r"\s*MEDIA:", raw_line, re.IGNORECASE):
            continue
        cleaned = "".join(ch for ch in raw_line if unicodedata.category(ch) not in ("Cc", "Zl", "Zp"))
        # Hermes reads MEDIA:<path> as an attachment directive anywhere in text, so
        # neutralize the token mid-line too (not only at the start of a line).
        cleaned = re.sub(r"(?i)media\s*:", "media-", cleaned).strip()
        if cleaned:
            kept.append(cleaned)
    return "\n".join(kept)


def _finalize(parts: list[str], *, incomplete: bool, link: str | None) -> str:
    lines = [HEADER]
    lines.extend(part for part in (_clean(p) for p in parts) if part)
    if incomplete:
        lines.append(UNAVAILABLE_LINE)
    if link:
        lines.append(link)
    text = "\n".join(lines)
    if len(text) > MESSAGE_MAX_CHARS:
        text = text[: MESSAGE_MAX_CHARS - 3] + "..."
    return text


def _item_message(item: dict[str, Any], *, incomplete: bool, link: str | None) -> str:
    kind = str(item.get("kind") or "")
    label = KIND_LABELS.get(kind, "Attention item")
    mission = _redact_line(item.get("mission_id") or "") or "none"
    tool = _redact_line(item.get("suggested_tool") or "") or "none"
    parts = [
        f"Kind: {label}",
        f"Mission: {mission}",
        f"Summary: {_redact_line(item.get('summary') or '')}",
        f"Suggested tool: {tool}",
    ]
    return _finalize(parts, incomplete=incomplete, link=link)


def _digest_message(items: list[dict[str, Any]], *, incomplete: bool, link: str | None) -> str:
    counts: OrderedDict[str, int] = OrderedDict()
    for item in items:
        label = KIND_LABELS.get(str(item.get("kind") or ""), "Attention item")
        counts[label] = counts.get(label, 0) + 1
    kinds = ", ".join(f"{label} x{count}" for label, count in counts.items())
    return _finalize([f"{len(items)} items need you: {kinds}"], incomplete=incomplete, link=link)


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------


def _child_env(executable: str) -> dict[str, str]:
    """Minimal environment: the executable's directory plus system dirs, HOME and LANG only."""
    env = {"PATH": os.pathsep.join([os.path.dirname(executable), "/usr/bin", "/bin"])}
    for name in ("HOME", "LANG"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _close(stream: Any) -> None:
    try:
        if stream is not None:
            stream.close()
    except OSError:
        pass


def _drive(
    argv: list[str],
    payload: bytes,
    *,
    env: dict[str, str],
    timeout: float,
    stop_when: Callable[[bytes], bool] | None = None,
) -> ChildOutcome:
    """Run one bounded child: write payload to stdin, read stdout, kill and reap on any exit path.

    With ``stop_when=None`` stdin is closed after the payload and stdout is read to EOF.
    With a predicate, stdin stays open until the predicate is satisfied (interactive
    JSON-RPC), then stdin is closed and the child gets a short grace period.
    """
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            env=dict(env),
            close_fds=True,
        )
    except FileNotFoundError:
        raise _SpawnFailed(ERR_NOT_CONFIGURED) from None
    except OSError:
        raise _SpawnFailed(ERR_SPAWN_FAILED) from None

    chunks: queue.Queue[bytes | None] = queue.Queue()

    def pump() -> None:
        try:
            while True:
                data = os.read(proc.stdout.fileno(), 4096)
                if not data:
                    break
                chunks.put(data)
        except OSError:
            pass
        finally:
            chunks.put(None)

    reader = threading.Thread(target=pump, name="attention-delivery-stdout", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    timed_out = False
    overflow = False
    eof = False
    try:
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except OSError:
            pass  # child exited early; stdout/exit status below decide the outcome
        if stop_when is None:
            _close(proc.stdin)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                chunk = chunks.get(timeout=remaining)
            except queue.Empty:
                timed_out = True
                break
            if chunk is None:
                eof = True
                break
            buffer.extend(chunk)
            if len(buffer) > OUTPUT_CAP_BYTES:
                overflow = True
                break
            if stop_when is not None and stop_when(bytes(buffer)):
                break
        _close(proc.stdin)
        if not (timed_out or overflow):
            limit = max(0.05, deadline - time.monotonic()) if eof else 2.0
            try:
                proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                if eof:
                    timed_out = True
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        reader.join(timeout=1.0)
        if not reader.is_alive():
            _close(proc.stdout)
    return ChildOutcome(
        returncode=proc.returncode,
        stdout=bytes(buffer[:OUTPUT_CAP_BYTES]),
        timed_out=timed_out,
        overflow=overflow,
    )


def _exit_code(returncode: int | None) -> str:
    if returncode is None:
        return ERR_BAD_RESPONSE
    if returncode < 0:
        return f"EXIT_{128 - returncode}"  # killed by signal -N, shell convention 128+N
    return f"EXIT_{returncode}"


def _hermes_reported_failure(stdout: bytes) -> bool:
    if not stdout.strip():
        return False
    try:
        obj = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False  # --quiet may suppress output; exit status is the contract
    if not isinstance(obj, dict):
        return False
    return bool(obj.get("error")) or obj.get("success") is False or obj.get("ok") is False


def _send_hermes(target: Target, text: str, config: AttentionConfig, run: Runner) -> tuple[bool, str | None]:
    executable = config.hermes_cli or ""
    argv = [executable, "send", "--to", target.target, "--file", "-", "--quiet", "--json"]
    outcome = run(argv, text.encode("utf-8"), env=_child_env(executable), timeout=CHILD_TIMEOUT_SECONDS, stop_when=None)
    if outcome.timed_out:
        return False, ERR_TIMEOUT
    if outcome.overflow:
        return False, ERR_BAD_RESPONSE
    if outcome.returncode != 0:
        return False, _exit_code(outcome.returncode)
    if _hermes_reported_failure(outcome.stdout):
        return False, ERR_BAD_RESPONSE
    return True, None


def _rpc_response(data: bytes, request_id: int, *, final: bool) -> dict[str, Any] | None:
    lines = data.split(b"\n")
    if not final:
        lines = lines[:-1]  # the trailing segment may be a partial line
    for raw in lines:
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("id") == request_id and not isinstance(obj.get("id"), bool):
            return obj
    return None


def _send_imessage(target: Target, text: str, config: AttentionConfig, run: Runner) -> tuple[bool, str | None]:
    executable = config.imsg_cli or ""
    argv = [executable, "rpc", "--json"]
    initialize = {"jsonrpc": "2.0", "id": RPC_INIT_ID, "method": "initialize", "params": {}}
    send = {
        "jsonrpc": "2.0",
        "id": RPC_SEND_ID,
        "method": "send",
        "params": {"to": target.target, "text": text, "service": "imessage"},
    }
    payload = (json.dumps(initialize) + "\n" + json.dumps(send) + "\n").encode("utf-8")
    outcome = run(
        argv,
        payload,
        env=_child_env(executable),
        timeout=CHILD_TIMEOUT_SECONDS,
        stop_when=lambda data: _rpc_response(data, RPC_SEND_ID, final=False) is not None,
    )
    if outcome.timed_out:
        # The send request was already written; Messages may have delivered it.
        return True, WARN_UNVERIFIED
    response = _rpc_response(outcome.stdout, RPC_SEND_ID, final=True)
    if response is None:
        if outcome.overflow:
            return False, ERR_BAD_RESPONSE
        if outcome.returncode not in (None, 0):
            return False, _exit_code(outcome.returncode)
        return False, ERR_BAD_RESPONSE
    if response.get("jsonrpc") != "2.0":
        return False, ERR_BAD_RESPONSE
    if "error" in response:
        if _imsg_may_have_completed(response.get("error")):
            return True, WARN_UNVERIFIED
        return False, ERR_BAD_RESPONSE
    if "result" not in response:
        return False, ERR_BAD_RESPONSE
    return True, None


def _imsg_may_have_completed(error: Any) -> bool:
    """imsg reports sends it could not confirm as not retry-safe (e.g. "Delivery outcome unknown")."""
    data = error.get("data") if isinstance(error, dict) else None
    if not isinstance(data, dict):
        return False
    return data.get("retry_safe") is False or data.get("disposition") == "may_have_completed"


def _send(target: Target, text: str, config: AttentionConfig, run: Runner) -> tuple[bool, str | None]:
    try:
        if target.backend == "hermes":
            return _send_hermes(target, text, config, run)
        return _send_imessage(target, text, config, run)
    except _SpawnFailed as exc:
        return False, exc.code


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _record_success(entry: dict[str, Any], epoch: float, warning: str | None = None) -> None:
    entry["last_success_at"] = _iso(epoch)
    entry["last_warning"] = {"code": warning, "at": _iso(epoch)} if warning else None
    entry["last_error"] = None
    entry["failure_count"] = 0
    entry["next_attempt_at"] = None


def _record_failure(entry: dict[str, Any], code: str, epoch: float) -> None:
    entry["failure_count"] = int(entry.get("failure_count") or 0) + 1
    delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (entry["failure_count"] - 1)))
    entry["next_attempt_at"] = _iso(epoch + delay)
    entry["last_error"] = {"code": code, "at": _iso(epoch)}


def _unavailable(message: str, epoch: float, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "enabled": True,
        "status": "error",
        "error": message,
        "messages_sent": 0,
        "items_delivered": 0,
        "errors": 0,
        "generated_at": _iso(epoch),
    }
    payload.update(extra)
    return payload


def run_once(hermes_root: Path | None = None, *, now: Any = None, runner: Runner | None = None) -> dict[str, Any]:
    """Deliver new eligible inbox items to every configured target once.

    Returns ``{"enabled": False}`` when the relay is off. Raises
    ``AttentionConfigError`` on invalid configuration (fail closed, nothing is sent).
    ``runner`` replaces the child-process driver; it receives
    ``(argv, payload, *, env, timeout, stop_when)`` and returns a ``ChildOutcome``.
    """
    if not enabled():
        return {"enabled": False}
    config = load_config()
    run: Runner = runner or _drive
    root = missions._root(hermes_root)  # noqa: SLF001
    epoch = _to_epoch(now)
    with _single_writer(root) as acquired:
        if not acquired:
            return {
                "enabled": True,
                "status": "skipped",
                "reason": "locked",
                "messages_sent": 0,
                "items_delivered": 0,
                "errors": 0,
                "generated_at": _iso(epoch),
            }
        path = _state_path(root)
        try:
            state = _load_state(path)
        except _StateError:
            return _unavailable(ERR_STATE, epoch)
        try:
            inbox = owner_inbox.build_inbox(root, limit=INBOX_LIMIT)
        except Exception:  # noqa: BLE001 - an unreadable inbox must not produce sends
            return _unavailable("INBOX_UNAVAILABLE", epoch)
        incomplete = not bool(inbox.get("complete"))
        identified, skipped = _identify(list(inbox.get("items") or []), epoch)
        _prune(state, epoch)

        reports: list[dict[str, Any]] = []
        messages_sent = 0
        items_delivered = 0
        errors = 0
        for target in config.targets:
            entry = _target_entry(state, target.key)
            report: dict[str, Any] = {
                "target": mask_target(target.backend, target.target),
                "backend": target.backend,
                "status": "idle",
                "messages": 0,
                "items_delivered": 0,
                "digest": False,
                "error": None,
                "pending": 0,
            }
            reports.append(report)
            new_items = [item for item in identified if item["item_id"] not in entry["delivered"]]
            report["pending"] = len(new_items)
            if not new_items:
                continue
            next_attempt = _parse_iso(entry.get("next_attempt_at"))
            if next_attempt is not None and next_attempt > epoch:
                report["status"] = "backoff"
                continue
            if len(new_items) > config.max_per_run:
                text = _digest_message(new_items, incomplete=incomplete, link=_link(config))
                ok, code = _send(target, text, config, run)
                if ok:
                    stamp = _iso(epoch)
                    for item in new_items:
                        entry["delivered"][item["item_id"]] = stamp
                    _record_success(entry, epoch, code)
                    report.update(status="digest", messages=1, items_delivered=len(new_items), digest=True)
                    if code:
                        report["unverified"] = 1
                    messages_sent += 1
                    items_delivered += len(new_items)
                else:
                    _record_failure(entry, code or ERR_BAD_RESPONSE, epoch)
                    report.update(status="error", error=code)
                    errors += 1
                continue
            sent_here = 0
            unverified = 0
            failure: str | None = None
            for item in new_items:
                text = _item_message(item, incomplete=incomplete, link=_link(config))
                ok, code = _send(target, text, config, run)
                if not ok:
                    failure = code or ERR_BAD_RESPONSE
                    break
                entry["delivered"][item["item_id"]] = _iso(epoch)
                sent_here += 1
                unverified += 1 if code else 0
            if unverified:
                report["unverified"] = unverified
            if sent_here:
                _record_success(entry, epoch, WARN_UNVERIFIED if unverified else None)
                messages_sent += sent_here
                items_delivered += sent_here
            if failure is not None:
                _record_failure(entry, failure, epoch)
                errors += 1
                report.update(status="error", error=failure)
            else:
                report["status"] = "delivered"
            report.update(messages=sent_here, items_delivered=sent_here)
            report["pending"] = len([i for i in identified if i["item_id"] not in entry["delivered"]])

        payload: dict[str, Any] = {
            "enabled": True,
            "status": "ok",
            "complete": not incomplete,
            "items": {"eligible": len(identified), "skipped": skipped},
            "targets": reports,
            "messages_sent": messages_sent,
            "items_delivered": items_delivered,
            "errors": errors,
            "generated_at": _iso(epoch),
        }
        try:
            _save_state(path, state)
        except OSError:
            payload["status"] = "error"
            payload["error"] = ERR_STATE_WRITE
        return payload


def _link(config: AttentionConfig) -> str | None:
    return f"{config.link_base}/ui/ops/owner-inbox" if config.link_base else None


def status(hermes_root: Path | None = None) -> dict[str, Any]:
    """Read-only view of configuration, per-target delivery health and pending counts. Writes nothing."""
    root = missions._root(hermes_root)  # noqa: SLF001
    if not enabled():
        return {"enabled": False, "read_only": True}
    try:
        config = load_config()
    except AttentionConfigError:
        return {"enabled": True, "configured": False, "error": ERR_CONFIG, "targets": [], "read_only": True}
    state_error = False
    try:
        state = _load_state(_state_path(root))
    except _StateError:
        state_error = True
        state = _empty_state()
    try:
        inbox: dict[str, Any] | None = owner_inbox.build_inbox(root, limit=INBOX_LIMIT)
    except Exception:  # noqa: BLE001 - status reports, never raises
        inbox = None
    identified: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    if inbox is not None:
        identified, skipped = _identify(list(inbox.get("items") or []), time.time())
    targets: list[dict[str, Any]] = []
    for target in config.targets:
        entry = state["targets"].get(target.key) or {}
        delivered = entry.get("delivered") if isinstance(entry.get("delivered"), dict) else {}
        pending = None
        if inbox is not None and not state_error:
            pending = len([item for item in identified if item["item_id"] not in delivered])
        targets.append(
            {
                "backend": target.backend,
                "target": mask_target(target.backend, target.target),
                "last_success_at": entry.get("last_success_at"),
                "last_error": entry.get("last_error"),
                "last_warning": entry.get("last_warning"),
                "failure_count": int(entry.get("failure_count") or 0),
                "next_attempt_at": entry.get("next_attempt_at"),
                "pending": pending,
            }
        )
    return {
        "enabled": True,
        "configured": True,
        "error": ERR_STATE if state_error else None,
        "targets": targets,
        "max_per_run": config.max_per_run,
        "link_base_configured": config.link_base is not None,
        "inbox_complete": None if inbox is None else bool(inbox.get("complete")),
        "unidentified_items": skipped.get("missing_item_id"),
        "read_only": True,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False), flush=True)


def _run_guarded() -> tuple[dict[str, Any], int]:
    epoch = time.time()
    try:
        payload = run_once()
    except AttentionConfigError as exc:
        return {"enabled": True, "status": "error", "error": ERR_CONFIG, "message": str(exc), "generated_at": _iso(epoch)}, 2
    except Exception:  # noqa: BLE001 - keep the watcher alive; report a fixed code only
        return {"enabled": True, "status": "error", "error": "RUN_FAILED", "generated_at": _iso(epoch)}, 1
    return payload, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="operator_attention_delivery",
        description="Deliver new Owner-inbox items to configured attention targets (default off).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one delivery pass (default)")
    mode.add_argument("--watch", action="store_true", help="repeat delivery passes until SIGTERM/SIGINT")
    parser.add_argument(
        "--interval",
        type=int,
        default=INTERVAL_DEFAULT,
        help=f"seconds between passes in --watch mode ({INTERVAL_MIN}-{INTERVAL_MAX}, default {INTERVAL_DEFAULT})",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse usage errors exit 2
        return int(exc.code or 0)
    if not INTERVAL_MIN <= args.interval <= INTERVAL_MAX:
        _emit({"enabled": enabled(), "status": "error", "error": ERR_CONFIG, "message": "interval out of range"})
        return 2
    if not args.watch:
        payload, code = _run_guarded()
        _emit(payload)
        return code
    if not enabled():
        _emit({"enabled": False})
        return 0
    stop = threading.Event()
    previous: dict[int, Any] = {}

    def _request_stop(signum: int, frame: Any) -> None:  # noqa: ARG001
        stop.set()

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, _request_stop)
    exit_code = 0
    try:
        while not stop.is_set():
            payload, code = _run_guarded()
            _emit(payload)
            if code == 2:
                exit_code = 2
                break
            if stop.wait(args.interval):
                break
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return exit_code


__all__ = [
    "AttentionConfig",
    "AttentionConfigError",
    "Target",
    "load_config",
    "main",
    "main_entry",
    "mask_target",
    "run_once",
    "status",
]

def main_entry() -> None:
    """Console-script entry point (``hermes-gpt-attention-relay``): forwards argv to ``main``."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
