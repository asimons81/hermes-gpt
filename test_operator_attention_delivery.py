"""Tests for the default-off attention delivery relay (operator_attention_delivery).

No test sends a real message. Most tests inject a fake runner. The few that
spawn children use harmless local scripts that record their argv, env and stdin
and never contact Hermes or Messages. An autouse guard refuses any other spawn.
"""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
import json
import os
import signal
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import operator_attention_delivery as delivery
import operator_owner_inbox as owner_inbox

REAL_DRIVE = delivery._drive
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc).timestamp()
STATE_REL = Path("missions") / "attention_delivery_state.json"
ISOLATED = (
    delivery.ENABLE_ENV,
    delivery.TARGETS_ENV,
    delivery.HERMES_CLI_ENV,
    delivery.IMSG_CLI_ENV,
    delivery.LINK_BASE_ENV,
    delivery.MAX_PER_RUN_ENV,
    "TYPESAFE_API_KEY",
    "HERMES_GPT_BEARER_TOKEN",
    "HERMES_GPT_OAUTH_CLIENT_SECRET",
)
SENTINELS = {
    "TYPESAFE_API_KEY": "sentinel-typesafe-key-value",
    "HERMES_GPT_BEARER_TOKEN": "sentinel-bearer-value",
    "HERMES_GPT_OAUTH_CLIENT_SECRET": "sentinel-oauth-value",
}



# The real-process tests spawn shebang scripts and rely on POSIX execute bits
# and flock; the relay's child backends (imsg, hermes send) are POSIX in practice.
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process/permission/flock semantics")

def make_exe(path: Path, body: str = "import sys\nsys.exit(0)\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


class FakeRunner:
    """Records every child invocation; never spawns anything."""

    def __init__(self, outcome_for=None):
        self.calls: list[dict] = []
        self.outcome_for = outcome_for

    def __call__(self, argv, payload, *, env, timeout, stop_when=None):
        self.calls.append(
            {"argv": list(argv), "payload": payload, "env": dict(env), "timeout": timeout, "stop_when": stop_when}
        )
        if self.outcome_for is not None:
            return self.outcome_for(argv, payload)
        if "rpc" in argv:
            return delivery.ChildOutcome(0, b'{"jsonrpc":"2.0","id":2,"result":{}}\n')
        return delivery.ChildOutcome(0, b"")

    def texts(self):
        return [c["payload"].decode("utf-8") for c in self.calls]


def failing_outcome(returncode=1):
    return lambda argv, payload: delivery.ChildOutcome(returncode, b"")


def item(item_id, *, kind="mission_awaiting_approval", summary="Mission is awaiting Owner approval.",
         mission="msn-abc123", tool="hermes_mission_approve", since=None, **extra):
    row = {
        "item_id": item_id,
        "kind": kind,
        "mission_id": mission,
        "summary": summary,
        "since": since,
        "suggested_tool": tool,
        "requires": "owner",
    }
    row.update(extra)
    return row


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("test attempted a real child process spawn")

    monkeypatch.setattr(delivery, "_drive", refuse)


@pytest.fixture
def relay(monkeypatch, tmp_path):
    for name in ISOLATED:
        monkeypatch.delenv(name, raising=False)
    hermes_bin = make_exe(tmp_path / "bin" / "hermes")
    imsg_bin = make_exe(tmp_path / "bin" / "imsg")
    monkeypatch.setenv(delivery.ENABLE_ENV, "1")
    monkeypatch.setenv(delivery.HERMES_CLI_ENV, str(hermes_bin))
    monkeypatch.setenv(delivery.IMSG_CLI_ENV, str(imsg_bin))
    monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram,imessage:+15551234567")
    root = tmp_path / "hermes-root"
    root.mkdir()
    return types.SimpleNamespace(root=root, tmp=tmp_path, hermes=hermes_bin, imsg=imsg_bin, monkeypatch=monkeypatch)


def set_inbox(monkeypatch, items, complete=True):
    def fake_build(hermes_root=None, limit=50):
        return {"items": list(items), "count": len(items), "complete": complete, "sources": {}, "skipped_sources": []}

    monkeypatch.setattr(owner_inbox, "build_inbox", fake_build)


def read_state(root: Path) -> dict:
    return json.loads((root / STATE_REL).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Disabled and configuration
# ---------------------------------------------------------------------------


def test_disabled_is_a_no_op(relay, monkeypatch, capsys):
    monkeypatch.setenv(delivery.ENABLE_ENV, "true")  # only the exact value enables
    runner = FakeRunner()
    set_inbox(monkeypatch, [item("a1")])
    assert delivery.run_once(relay.root, now=NOW, runner=runner) == {"enabled": False}
    assert delivery.status(relay.root) == {"enabled": False, "read_only": True}
    assert runner.calls == []
    assert delivery.main(["--once"]) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": False}
    assert not (relay.root / "missions").exists()


@pytest.mark.parametrize(
    "targets",
    [
        "slack:C0123ABCD",  # unsupported backend
        "hermes:Telegram!",  # uppercase / bad characters
        "hermes:",  # empty value
        "telegram",  # missing backend separator
        "imessage:+0123456",  # phone must not start with 0
        "imessage:not-an-address",
        "hermes:telegram,,hermes:discord:#ops",  # empty entry
        "hermes:telegram,hermes:telegram",  # duplicate
        ",".join(f"hermes:platform{i}" for i in range(9)),  # more than 8
        "   ",
    ],
)
def test_invalid_targets_fail_closed(relay, targets):
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, targets)
    with pytest.raises(delivery.AttentionConfigError):
        delivery.run_once(relay.root, now=NOW, runner=FakeRunner())


def test_config_error_does_not_echo_target_value(relay):
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "imessage:+15551234567x")
    with pytest.raises(delivery.AttentionConfigError) as info:
        delivery.load_config()
    assert "5551234567" not in str(info.value)


@pytest.mark.parametrize("value", ["relative/hermes", "./hermes"])
def test_relative_executable_path_rejected(relay, value):
    relay.monkeypatch.setenv(delivery.HERMES_CLI_ENV, value)
    with pytest.raises(delivery.AttentionConfigError, match="absolute path"):
        delivery.load_config()


def test_missing_executable_rejected(relay):
    relay.monkeypatch.setenv(delivery.HERMES_CLI_ENV, str(relay.tmp / "nope" / "hermes"))
    with pytest.raises(delivery.AttentionConfigError, match="existing executable"):
        delivery.load_config()


@POSIX_ONLY
def test_non_executable_file_rejected(relay):
    plain = relay.tmp / "plain-hermes"
    plain.write_text("not executable", encoding="utf-8")
    plain.chmod(0o644)
    relay.monkeypatch.setenv(delivery.HERMES_CLI_ENV, str(plain))
    with pytest.raises(delivery.AttentionConfigError):
        delivery.load_config()


def test_directory_as_executable_rejected(relay):
    relay.monkeypatch.setenv(delivery.HERMES_CLI_ENV, str(relay.tmp))
    with pytest.raises(delivery.AttentionConfigError):
        delivery.load_config()


def test_executable_only_required_for_used_backend(relay):
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "imessage:+15551234567")
    relay.monkeypatch.delenv(delivery.HERMES_CLI_ENV)
    config = delivery.load_config()
    assert config.hermes_cli is None
    assert config.imsg_cli == str(relay.imsg)


def test_max_per_run_bounds(relay):
    relay.monkeypatch.setenv(delivery.MAX_PER_RUN_ENV, "21")
    with pytest.raises(delivery.AttentionConfigError):
        delivery.load_config()
    relay.monkeypatch.setenv(delivery.MAX_PER_RUN_ENV, "0")
    with pytest.raises(delivery.AttentionConfigError):
        delivery.load_config()
    relay.monkeypatch.delenv(delivery.MAX_PER_RUN_ENV)
    assert delivery.load_config().max_per_run == 5


def test_link_base_must_be_https(relay):
    relay.monkeypatch.setenv(delivery.LINK_BASE_ENV, "http://example.test")
    with pytest.raises(delivery.AttentionConfigError):
        delivery.load_config()
    relay.monkeypatch.setenv(delivery.LINK_BASE_ENV, "https://gpt.example.test/")
    assert delivery.load_config().link_base == "https://gpt.example.test"


def test_mask_target_redacts_phone_email_and_chat_ids():
    assert delivery.mask_target("imessage", "+15551234567") == "imessage:+1•••4567"
    assert delivery.mask_target("imessage", "owner@example.test") == "imessage:o•••@example.test"
    assert delivery.mask_target("hermes", "telegram") == "hermes:telegram"
    assert delivery.mask_target("hermes", "discord:#ops") == "hermes:discord:#ops"
    assert "100123456789" not in delivery.mask_target("hermes", "telegram:-100123456789")


# ---------------------------------------------------------------------------
# Backend request shapes (fake runner)
# ---------------------------------------------------------------------------


def test_hermes_argv_is_exact_and_message_on_stdin(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1", summary="Plan waits.")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner()
    delivery.run_once(relay.root, now=NOW, runner=runner)
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["argv"] == [str(relay.hermes), "send", "--to", "telegram", "--file", "-", "--quiet", "--json"]
    assert call["payload"].decode("utf-8").startswith("Hermes GPT needs you")
    assert call["stop_when"] is None
    assert call["timeout"] == 20.0


def test_hermes_target_with_thread_is_passed_verbatim(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:discord:#ops")
    runner = FakeRunner()
    delivery.run_once(relay.root, now=NOW, runner=runner)
    assert runner.calls[0]["argv"][3] == "discord:#ops"


def test_imessage_jsonrpc_request_shape(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1", summary="Plan waits.")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "imessage:+15551234567")
    runner = FakeRunner()
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    call = runner.calls[0]
    assert call["argv"] == [str(relay.imsg), "rpc", "--json"]
    lines = call["payload"].decode("utf-8").splitlines()
    assert len(lines) == 2
    init, send = json.loads(lines[0]), json.loads(lines[1])
    assert init["method"] == "initialize" and init["id"] == 1 and init["jsonrpc"] == "2.0"
    assert send["method"] == "send" and send["id"] == 2 and send["jsonrpc"] == "2.0"
    assert send["params"]["to"] == "+15551234567"
    assert send["params"]["service"] == "imessage"
    assert "Plan waits." in send["params"]["text"]
    stop = call["stop_when"]
    assert stop(b'{"jsonrpc":"2.0","id":2,"result":{}}\n') is True
    assert stop(b'{"jsonrpc":"2.0","id":2,"result":{}}') is False  # partial line is not a response
    assert stop(b'{"jsonrpc":"2.0","id":1,"result":{}}\n') is False
    assert summary["messages_sent"] == 1


def test_imessage_rpc_error_is_not_delivered(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "imessage:+15551234567")
    runner = FakeRunner(lambda argv, payload: delivery.ChildOutcome(
        0, b'{"jsonrpc":"2.0","id":2,"error":{"code":-32603,"message":"write failed"}}\n'))
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    assert summary["errors"] == 1
    assert summary["targets"][0]["error"] == "BAD_RESPONSE"
    assert read_state(relay.root)["targets"]["imessage:+15551234567"]["delivered"] == {}


def test_imessage_missing_response_is_bad_response(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "imessage:+15551234567")
    runner = FakeRunner(lambda argv, payload: delivery.ChildOutcome(0, b""))
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    assert summary["targets"][0]["error"] == "BAD_RESPONSE"


def test_hermes_nonzero_exit_maps_to_exit_code(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    summary = delivery.run_once(relay.root, now=NOW, runner=FakeRunner(failing_outcome(1)))
    assert summary["targets"][0]["error"] == "EXIT_1"


def test_hermes_json_reported_failure_is_not_delivered(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner(lambda argv, payload: delivery.ChildOutcome(0, b'{"success": false}'))
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    assert summary["targets"][0]["error"] == "BAD_RESPONSE"


def test_timeout_is_recorded_as_timeout(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner(lambda argv, payload: delivery.ChildOutcome(None, b"", timed_out=True))
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    assert summary["targets"][0]["error"] == "TIMEOUT"
    assert read_state(relay.root)["targets"]["hermes:telegram"]["last_error"]["code"] == "TIMEOUT"


def test_spawn_failure_codes(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")

    def raise_missing(argv, payload, **kwargs):
        raise delivery._SpawnFailed(delivery.ERR_NOT_CONFIGURED)

    summary = delivery.run_once(relay.root, now=NOW, runner=raise_missing)
    assert summary["targets"][0]["error"] == "NOT_CONFIGURED"


# ---------------------------------------------------------------------------
# Selection, dedupe, digest, delivery state
# ---------------------------------------------------------------------------


def test_second_run_sends_nothing(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1"), item("a2")])
    first = FakeRunner()
    delivery.run_once(relay.root, now=NOW, runner=first)
    assert len(first.calls) == 4  # two items x two targets
    second = FakeRunner()
    summary = delivery.run_once(relay.root, now=NOW + 1, runner=second)
    assert second.calls == []
    assert summary["messages_sent"] == 0
    assert summary["targets"][0]["pending"] == 0


def test_acknowledged_items_are_skipped(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1", acknowledged=True), item("a2", acknowledged=False)])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner()
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    assert summary["items"]["skipped"]["acknowledged"] == 1
    assert len(runner.calls) == 1
    assert "Mission:" in runner.texts()[0]
    delivered = read_state(relay.root)["targets"]["hermes:telegram"]["delivered"]
    assert set(delivered) == {"a2"}


def test_missing_item_id_is_skipped_and_reported(relay, monkeypatch):
    legacy = item("x")
    del legacy["item_id"]
    set_inbox(monkeypatch, [legacy, item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner()
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    assert summary["items"]["skipped"]["missing_item_id"] == 1
    assert len(runner.calls) == 1


def test_failure_is_not_marked_delivered_and_backs_off(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    delivery.run_once(relay.root, now=NOW, runner=FakeRunner(failing_outcome(1)))
    entry = read_state(relay.root)["targets"]["hermes:telegram"]
    assert entry["delivered"] == {}
    assert entry["failure_count"] == 1
    assert entry["last_error"]["code"] == "EXIT_1"

    # Inside the 30 second backoff window nothing is attempted.
    quiet = FakeRunner()
    summary = delivery.run_once(relay.root, now=NOW + 10, runner=quiet)
    assert quiet.calls == []
    assert summary["targets"][0]["status"] == "backoff"

    # After the window the item is retried and, on success, recorded as delivered.
    retry = FakeRunner()
    delivery.run_once(relay.root, now=NOW + 31, runner=retry)
    assert len(retry.calls) == 1
    entry = read_state(relay.root)["targets"]["hermes:telegram"]
    assert "a1" in entry["delivered"]
    assert entry["failure_count"] == 0
    assert entry["last_error"] is None


def test_backoff_grows_and_is_capped(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    expected = []
    for failures in range(1, 9):
        now = NOW + sum(expected)
        delivery.run_once(relay.root, now=now, runner=FakeRunner(failing_outcome(1)))
        entry = read_state(relay.root)["targets"]["hermes:telegram"]
        delay = datetime.fromisoformat(entry["next_attempt_at"]).timestamp() - now
        expected.append(delay)
        assert entry["failure_count"] == failures
    assert expected[:3] == [30, 60, 120]
    assert max(expected) == delivery.BACKOFF_CAP_SECONDS


def test_one_failing_target_does_not_block_another(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])

    def only_imessage_ok(argv, payload):
        if "rpc" in argv:
            return delivery.ChildOutcome(0, b'{"jsonrpc":"2.0","id":2,"result":{}}\n')
        return delivery.ChildOutcome(1, b"")

    summary = delivery.run_once(relay.root, now=NOW, runner=FakeRunner(only_imessage_ok))
    statuses = {t["backend"]: t["status"] for t in summary["targets"]}
    assert statuses == {"hermes": "error", "imessage": "delivered"}


def test_digest_when_over_max_per_run(relay, monkeypatch):
    items = [
        item("a1"),
        item("a2"),
        item("a3", kind="budget_breaker", tool="hermes_budget_check"),
    ]
    set_inbox(monkeypatch, items)
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    relay.monkeypatch.setenv(delivery.MAX_PER_RUN_ENV, "2")
    runner = FakeRunner()
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    assert len(runner.calls) == 1
    text = runner.texts()[0]
    assert "3 items need you: Mission awaiting approval x2, Budget breaker x1" in text
    assert summary["targets"][0]["digest"] is True
    assert set(read_state(relay.root)["targets"]["hermes:telegram"]["delivered"]) == {"a1", "a2", "a3"}


def test_individual_messages_at_or_under_max(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1"), item("a2")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    relay.monkeypatch.setenv(delivery.MAX_PER_RUN_ENV, "2")
    runner = FakeRunner()
    delivery.run_once(relay.root, now=NOW, runner=runner)
    assert len(runner.calls) == 2
    assert all("need you:" not in t for t in runner.texts())


def test_link_appended_when_configured(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    relay.monkeypatch.setenv(delivery.LINK_BASE_ENV, "https://gpt.example.test")
    runner = FakeRunner()
    delivery.run_once(relay.root, now=NOW, runner=runner)
    assert runner.texts()[0].endswith("https://gpt.example.test/ui/ops/owner-inbox")


def test_incomplete_inbox_adds_single_note_but_still_delivers(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")], complete=False)
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner()
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    assert summary["complete"] is False
    assert len(runner.calls) == 1
    assert runner.texts()[0].count("Some sources unavailable") == 1


def test_complete_inbox_has_no_unavailable_note(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")], complete=True)
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner()
    delivery.run_once(relay.root, now=NOW, runner=runner)
    assert "Some sources unavailable" not in runner.texts()[0]


def test_stale_items_are_not_renotified(relay, monkeypatch):
    old = datetime.fromtimestamp(NOW - 40 * 86400, timezone.utc).isoformat()
    set_inbox(monkeypatch, [item("old", since=old)])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner()
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    assert runner.calls == []
    assert summary["items"]["skipped"]["stale"] == 1


# ---------------------------------------------------------------------------
# Message text safety
# ---------------------------------------------------------------------------


def test_media_lines_and_control_characters_are_stripped(relay, monkeypatch):
    summary_text = "Plan waits\nMEDIA:/etc/passwd\n  media: ./secret.png\nok\x07 bell\x1b[31m tail"
    set_inbox(monkeypatch, [item("a1", summary=summary_text)])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner()
    delivery.run_once(relay.root, now=NOW, runner=runner)
    text = runner.texts()[0]
    assert "MEDIA:" not in text.upper()
    assert "passwd" not in text and "secret.png" not in text
    assert "\x07" not in text and "\x1b" not in text and " " not in text
    assert "ok bell[31m" in text
    assert "tail" in text


def test_media_token_mid_line_is_neutralized(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1", summary="Summary: MEDIA:/etc/hosts and Media : x")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner()
    delivery.run_once(relay.root, now=NOW, runner=runner)
    text = runner.texts()[0]
    assert "media:" not in text.lower().replace("media-", "")
    assert "/etc/hosts" in text  # content kept, directive neutralized


def test_message_is_capped_at_one_thousand_characters(relay):
    huge = "x" * 5000
    body = delivery._item_message(item("a1", summary=huge, mission="m" * 300), incomplete=True,
                                  link="https://gpt.example.test/ui/ops/owner-inbox")
    assert len(body) <= delivery.MESSAGE_MAX_CHARS
    assert body.startswith(delivery.HEADER)


def test_digest_message_is_capped(relay):
    items = [item(f"a{i}", kind=f"unknown-{i}") for i in range(500)]
    text = delivery._digest_message(items, incomplete=False, link=None)
    assert len(text) <= delivery.MESSAGE_MAX_CHARS


def test_secret_looking_summary_text_is_redacted(relay, monkeypatch):
    summary_text = (
        "Bearer abcdef.ghijkl.mnopqr sk-ABCDEFGH12345678 "
        "password=hunter2 api_key: AKIA1234567890ABCD"
    )
    set_inbox(monkeypatch, [item("a1", summary=summary_text)])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner()
    delivery.run_once(relay.root, now=NOW, runner=runner)
    text = runner.texts()[0]
    for secret in ("abcdef.ghijkl", "sk-ABCDEFGH12345678", "hunter2", "AKIA1234567890ABCD"):
        assert secret not in text
    assert "[REDACTED]" in text


def test_message_excludes_raw_prompt_fields(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1", objective="SECRET OBJECTIVE TEXT", prompt="SECRET PROMPT", transcript="SECRET TRANSCRIPT")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    runner = FakeRunner()
    delivery.run_once(relay.root, now=NOW, runner=runner)
    text = runner.texts()[0]
    assert "SECRET" not in text


# ---------------------------------------------------------------------------
# Real child driver (local harmless scripts only)
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_real_driver_uses_argv_stdin_and_minimal_env(relay, monkeypatch):
    monkeypatch.setattr(delivery, "_drive", REAL_DRIVE)
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    capture = relay.tmp / "capture.json"
    exe = make_exe(
        relay.tmp / "capture-bin" / "hermes",
        "import json, os, sys\n"
        "data = sys.stdin.read()\n"
        f"json.dump({{'argv': sys.argv, 'env': dict(os.environ), 'stdin': data}}, open({str(capture)!r}, 'w'))\n",
    )
    monkeypatch.setenv(delivery.HERMES_CLI_ENV, str(exe))
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    set_inbox(monkeypatch, [item("a1", summary="Plan waits.")])
    summary = delivery.run_once(relay.root, now=NOW)
    assert summary["targets"][0]["status"] == "delivered"
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert captured["argv"][1:] == ["send", "--to", "telegram", "--file", "-", "--quiet", "--json"]
    # Only the three explicit keys, plus two names the interpreter/OS itself adds.
    assert set(captured["env"]) <= {"PATH", "HOME", "LANG", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert captured["env"]["PATH"] == os.pathsep.join([str(exe.parent), "/usr/bin", "/bin"])
    assert "Hermes GPT needs you" in captured["stdin"]
    blob = json.dumps(captured)
    for value in SENTINELS.values():
        assert value not in blob


@POSIX_ONLY
def test_real_jsonrpc_exchange_over_pipes(relay, monkeypatch):
    monkeypatch.setattr(delivery, "_drive", REAL_DRIVE)
    capture = relay.tmp / "rpc-lines.txt"
    exe = make_exe(
        relay.tmp / "rpc-bin" / "imsg",
        "import json, sys\n"
        "for line in sys.stdin:\n"
        f"    open({str(capture)!r}, 'a').write(line)\n"
        "    req = json.loads(line)\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': {'sent': True}}), flush=True)\n",
    )
    monkeypatch.setenv(delivery.IMSG_CLI_ENV, str(exe))
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "imessage:+15551234567")
    set_inbox(monkeypatch, [item("a1", summary="Plan waits.")])
    summary = delivery.run_once(relay.root, now=NOW)
    assert summary["targets"][0]["status"] == "delivered"
    lines = [json.loads(x) for x in capture.read_text(encoding="utf-8").splitlines()]
    assert [line["method"] for line in lines] == ["initialize", "send"]
    assert lines[1]["params"]["to"] == "+15551234567"
    assert read_state(relay.root)["targets"]["imessage:+15551234567"]["delivered"]


@POSIX_ONLY
def test_real_driver_kills_and_reaps_on_timeout(relay):
    marker = relay.tmp / "pid.txt"
    exe = make_exe(
        relay.tmp / "sleep-bin" / "sleeper",
        "import os, time\n"
        f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
    )
    outcome = REAL_DRIVE([str(exe)], b"", env={"PATH": "/usr/bin:/bin"}, timeout=5.0)
    assert outcome.timed_out is True
    pid = int(marker.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # killed and reaped: no process and no zombie


@POSIX_ONLY
def test_real_driver_missing_executable_is_not_configured(relay):
    with pytest.raises(delivery._SpawnFailed) as info:
        REAL_DRIVE([str(relay.tmp / "absent-binary")], b"", env={"PATH": "/usr/bin:/bin"}, timeout=2.0)
    assert info.value.code == "NOT_CONFIGURED"


@POSIX_ONLY
def test_real_driver_bounds_stdout(relay):
    exe = make_exe(relay.tmp / "flood-bin" / "flood", "import sys\nsys.stdout.write('x' * (300 * 1024))\n")
    outcome = REAL_DRIVE([str(exe)], b"", env={"PATH": "/usr/bin:/bin"}, timeout=10.0)
    assert outcome.overflow is True
    assert len(outcome.stdout) <= delivery.OUTPUT_CAP_BYTES


# ---------------------------------------------------------------------------
# State file atomicity, pruning, corruption, locking
# ---------------------------------------------------------------------------


def test_state_write_is_atomic_and_leaves_no_temp_files(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    delivery.run_once(relay.root, now=NOW, runner=FakeRunner())
    leftovers = [p.name for p in (relay.root / "missions").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert json.loads((relay.root / STATE_REL).read_text(encoding="utf-8"))["schema_version"] == 1


def test_failed_replace_keeps_previous_state(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    delivery.run_once(relay.root, now=NOW, runner=FakeRunner())
    before = (relay.root / STATE_REL).read_bytes()

    set_inbox(monkeypatch, [item("a1"), item("a2")])

    def broken_replace(src, dst):
        raise OSError("disk trouble")

    monkeypatch.setattr(delivery.os, "replace", broken_replace)
    summary = delivery.run_once(relay.root, now=NOW + 1, runner=FakeRunner())
    # Sends happened but the state could not be saved: reported, never silent.
    assert summary["status"] == "error" and summary["error"] == "STATE_WRITE_FAILED"
    assert (relay.root / STATE_REL).read_bytes() == before
    leftovers = [p.name for p in (relay.root / "missions").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_pruning_drops_old_and_caps_count(relay, monkeypatch):
    set_inbox(monkeypatch, [])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    recent = datetime.fromtimestamp(NOW - 3600, timezone.utc).isoformat()
    old = datetime.fromtimestamp(NOW - 20 * 86400, timezone.utc).isoformat()
    delivered = {f"recent-{i}": recent for i in range(2100)}
    delivered.update({f"old-{i}": old for i in range(5)})
    state = {"schema_version": 1, "targets": {"hermes:telegram": {"delivered": delivered, "failure_count": 0}}}
    (relay.root / "missions").mkdir(parents=True, exist_ok=True)
    (relay.root / STATE_REL).write_text(json.dumps(state), encoding="utf-8")
    delivery.run_once(relay.root, now=NOW, runner=FakeRunner())
    kept = read_state(relay.root)["targets"]["hermes:telegram"]["delivered"]
    assert len(kept) == delivery.RETENTION_MAX_PER_TARGET
    assert not any(key.startswith("old-") for key in kept)


def test_corrupt_state_sends_nothing_and_is_not_overwritten(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    (relay.root / "missions").mkdir(parents=True, exist_ok=True)
    (relay.root / STATE_REL).write_text("{not json", encoding="utf-8")
    runner = FakeRunner()
    summary = delivery.run_once(relay.root, now=NOW, runner=runner)
    assert runner.calls == []
    assert summary["error"] == "STATE_UNREADABLE"
    assert (relay.root / STATE_REL).read_text(encoding="utf-8") == "{not json"


@POSIX_ONLY
def test_second_writer_is_skipped_while_lock_held(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1")])
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "hermes:telegram")
    lock_path = relay.root / "missions" / delivery.LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "a+b")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = FakeRunner()
        summary = delivery.run_once(relay.root, now=NOW, runner=runner)
        assert summary["status"] == "skipped" and summary["reason"] == "locked"
        assert runner.calls == []
    finally:
        holder.close()


# ---------------------------------------------------------------------------
# Status (read-only)
# ---------------------------------------------------------------------------


def test_status_writes_nothing_and_redacts_targets(relay, monkeypatch):
    set_inbox(monkeypatch, [item("a1"), item("a2")])
    (relay.root / "missions").mkdir(parents=True, exist_ok=True)
    (relay.root / STATE_REL).write_text(
        json.dumps({"schema_version": 1, "targets": {"hermes:telegram": {
            "delivered": {"a1": "2026-10-21T11:00:00+00:00"}, "last_success_at": "2026-10-21T11:00:00+00:00",
            "last_error": None, "failure_count": 0, "next_attempt_at": None}}}),
        encoding="utf-8",
    )

    def snapshot():
        return sorted((str(p.relative_to(relay.root)), p.read_bytes() if p.is_file() else None)
                      for p in relay.root.rglob("*"))

    before = snapshot()
    view = delivery.status(relay.root)
    assert snapshot() == before

    assert view["enabled"] is True and view["configured"] is True
    by_target = {t["target"]: t for t in view["targets"]}
    assert set(by_target) == {"hermes:telegram", "imessage:+1•••4567"}
    assert by_target["hermes:telegram"]["pending"] == 1
    assert by_target["hermes:telegram"]["last_success_at"] == "2026-10-21T11:00:00+00:00"
    assert by_target["imessage:+1•••4567"]["pending"] == 2
    assert "15551234567" not in json.dumps(view)


def test_status_reports_configuration_error_without_raising(relay):
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "slack:C1")
    view = delivery.status(relay.root)
    assert view["configured"] is False and view["error"] == "CONFIGURATION"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_once_config_error_exits_2_with_message(relay, capsys):
    relay.monkeypatch.setenv(delivery.TARGETS_ENV, "slack:C1")
    assert delivery.main(["--once"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "CONFIGURATION"
    assert "C1" not in json.dumps(out)


def test_main_interval_bounds(relay):
    assert delivery.main(["--watch", "--interval", "14"]) == 2
    assert delivery.main(["--watch", "--interval", "3601"]) == 2


@POSIX_ONLY  # os.kill(SIGTERM) on Windows terminates the test process
def test_main_watch_stops_cleanly_on_sigterm(relay, monkeypatch, capsys):
    calls = []

    def fake_run_once(*args, **kwargs):
        calls.append(1)
        os.kill(os.getpid(), signal.SIGTERM)  # the signal arrives during a pass
        return {"enabled": True, "status": "ok", "messages_sent": 0}

    monkeypatch.setattr(delivery, "run_once", fake_run_once)
    before = signal.getsignal(signal.SIGTERM)
    assert delivery.main(["--watch", "--interval", "15"]) == 0
    assert len(calls) == 1
    assert signal.getsignal(signal.SIGTERM) is before  # previous disposition restored


def test_main_watch_disabled_returns_immediately(monkeypatch, capsys):
    for name in ISOLATED:
        monkeypatch.delenv(name, raising=False)
    assert delivery.main(["--watch", "--interval", "60"]) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": False}
