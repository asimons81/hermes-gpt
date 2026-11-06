"""Bounded local metadata/help observations; no live Agent or model calls."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import operator_agent_readiness as readiness
import operator_session as session


@pytest.fixture(autouse=True)
def isolated_preflight_observations(monkeypatch):
    monkeypatch.setattr(readiness, "_preflights", {})


def test_help_probe_fixed_argv_minimal_environment_and_closed_result(monkeypatch):
    calls = []
    reader, writer = os.pipe()
    os.write(writer, b"private /home/person token=secret --format stream-json")
    os.close(writer)

    class Proc:
        pid = 12345
        returncode = 0
        stdout = os.fdopen(reader, "rb")

        def poll(self):
            return 0

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(readiness.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)) or Proc())
    observed = readiness.probe_stream_json("/private/agent/hermes", {
        "PATH": "/bin", "HOME": "/home/person", "HERMES_HOME": "/home/person/.hermes",
        "OPENAI_API_KEY": "private-key", "HERMES_YOLO_MODE": "1", "PYTHONPATH": "/unrelated",
    })
    assert observed["state"] == "supported"
    argv, kwargs = calls[0]
    assert argv == ["/private/agent/hermes", "chat", "--help"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["env"] == {"PATH": "/bin", "HOME": "/home/person", "HERMES_HOME": "/home/person/.hermes"}
    assert set(observed) == {"state", "code", "checked_at"}
    assert "private" not in json.dumps(observed)


@pytest.mark.parametrize("code,output,state,result_code", [
    ("OK", b"--format stream-json", "supported", "SUPPORTED"),
    ("OK", b"--format text json", "unsupported", "FORMAT_NOT_ADVERTISED"),
    ("OK", b"stream-json only", "unsupported", "FORMAT_NOT_ADVERTISED"),
    ("PROBE_FAILED", b"--format stream-json", "unavailable", "PROBE_FAILED"),
    ("CLI_MISSING", b"", "unavailable", "CLI_MISSING"),
    ("PROBE_TIMEOUT", b"--format stream-json", "unavailable", "PROBE_TIMEOUT"),
    ("OUTPUT_LIMIT", b"--format stream-json", "unavailable", "OUTPUT_LIMIT"),
])
def test_probe_fails_closed(monkeypatch, code, output, state, result_code):
    monkeypatch.setattr(readiness, "_bounded_command", lambda *args: (code, output))
    result = readiness.probe_stream_json("hermes")
    assert (result["state"], result["code"]) == (state, result_code)


def test_process_output_and_runtime_are_bounded(monkeypatch):
    monkeypatch.setattr(readiness, "PROBE_TIMEOUT_SECONDS", 0.2)
    child_env = {"SystemRoot": os.environ["SystemRoot"]} if os.name == "nt" else {}
    started = time.monotonic()
    code, output = readiness._bounded_command([sys.executable, "-c", "import time; time.sleep(10)"], child_env)
    assert code == "PROBE_TIMEOUT" and not output
    assert time.monotonic() - started < 2
    monkeypatch.setattr(readiness, "PROBE_TIMEOUT_SECONDS", 2)
    code, output = readiness._bounded_command([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 200000); sys.stdout.flush()"], child_env)
    assert code == "OUTPUT_LIMIT" and not output
    code, output = readiness._bounded_command([sys.executable, "-c", "import sys; sys.stderr.write('private-error'); sys.exit(2)"], child_env)
    assert code == "PROBE_FAILED" and not output


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group pipe inheritance")
def test_exited_parent_cannot_leave_help_probe_waiting_on_descendant(monkeypatch):
    monkeypatch.setattr(readiness, "PROBE_TIMEOUT_SECONDS", 1)
    started = time.monotonic()
    code, _ = readiness._bounded_command([
        sys.executable, "-c",
        "import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])",
    ], {})
    assert code == "PROBE_TIMEOUT"
    assert time.monotonic() - started < 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX detached descendant fixture")
def test_detached_pipe_holder_cannot_accumulate_bridge_threads_or_descriptors(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness, "PROBE_TIMEOUT_SECONDS", 1)
    before_threads = {thread.ident for thread in threading.enumerate()}
    before_fds = len(os.listdir("/dev/fd"))
    for index in range(2):
        marker = tmp_path / f"child-{index}.pid"
        script = (
            "import subprocess,sys,pathlib; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True); "
            f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))"
        )
        try:
            code, _ = readiness._bounded_command([sys.executable, "-c", script], {})
            assert code == "PROBE_TIMEOUT"
            assert marker.exists(), "fixture parent must have started the detached child"
            assert {thread.ident for thread in threading.enumerate()} == before_threads
            assert len(os.listdir("/dev/fd")) == before_fds
        finally:
            if marker.exists():
                os.kill(int(marker.read_text()), 9)


def test_missing_executable_does_not_echo_path(tmp_path):
    result = readiness.probe_stream_json(str(tmp_path / "private-missing-cli"))
    assert result["code"] == "CLI_MISSING"
    assert str(tmp_path) not in json.dumps(result)


def _venv_cli(root: Path) -> Path:
    executable = root / "venv" / ("Scripts" if os.name == "nt" else "bin") / ("hermes.exe" if os.name == "nt" else "hermes")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable")
    return executable


def test_same_cli_resolver_as_session_control_and_distinct_source_identity(tmp_path, monkeypatch):
    root = tmp_path / "private-agent"
    cli = _venv_cli(root)
    monkeypatch.setattr(readiness.shutil, "which", lambda _: "/wrong/hermes")
    monkeypatch.setattr(readiness.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("readiness must never execute installed code"))
    monkeypatch.setenv("HERMES_GPT_ENABLE_SESSION_CONTROL", "0")
    observation = readiness.readiness(root)
    assert session._hermes_executable(root) == str(cli)
    assert observation["cli"]["resolution"] == "agent_venv"
    assert observation["cli"]["matches_agent_source"] is True
    assert observation["cli"]["identity_sha256"] != observation["agent_source"]["identity_sha256"]
    assert observation["session_control_enabled"] is False
    assert observation["live_acceptance"] == "not_verified"
    assert observation["finite_protocol"]["state"] == "not_observed"
    assert str(tmp_path) not in json.dumps(observation)
    monkeypatch.setenv("HERMES_GPT_ENABLE_SESSION_CONTROL", "1")
    again = readiness.readiness(root)
    assert again["session_control_enabled"] is True


def test_path_cli_relationship_is_explicit_and_source_is_not_rediscovered(tmp_path, monkeypatch):
    root = tmp_path / "selected-agent"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "hermes"
    outside.parent.mkdir()
    outside.write_text("synthetic")
    monkeypatch.setattr(readiness.shutil, "which", lambda _: str(outside))
    monkeypatch.setattr(readiness.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("metadata read must not probe"))
    result = readiness.readiness(root)
    assert result["cli"]["resolution"] == "path"
    assert result["cli"]["matches_agent_source"] is False
    missing_source = readiness.readiness(None)
    assert missing_source["agent_source"]["state"] == "unavailable"
    assert missing_source["cli"]["matches_agent_source"] is None
    assert str(tmp_path) not in json.dumps(result)


def test_no_cli_is_unknown_without_launching_anything(monkeypatch, tmp_path):
    monkeypatch.setattr(readiness.shutil, "which", lambda _: None)
    monkeypatch.setattr(readiness.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("no executable should be invoked"))
    result = readiness.readiness(tmp_path)
    assert result["finite_protocol"]["code"] == "NOT_OBSERVED"
    assert result["finite_protocol"]["state"] == "not_observed"
    assert result["cli"]["identity_sha256"] is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("output,revision", [(b"a" * 40 + b"\n", "a" * 40), (b"private /path token=secret", None), (b"f" * 64, "f" * 64)])
def test_source_revision_is_strict_metadata_only(tmp_path, monkeypatch, output, revision):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_bytes(output)
    monkeypatch.setattr(readiness.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("revision read must not execute Git"))
    assert readiness._revision(tmp_path) == revision


def test_session_preflight_uses_same_fresh_closed_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(readiness, "probe_stream_json", lambda executable, env: calls.append((executable, env)) or {"state": "unavailable"})
    assert session._supports_stream_json("selected-cli", {"HERMES_PROFILE": "default"}) is False
    assert calls == [("selected-cli", {"HERMES_PROFILE": "default"})]


PARSER = '''
def _build_chat_parser(subparsers):
    chat_parser = subparsers.add_parser("chat")
    add, inherited, SUPPRESS = chat_parser.add_argument, None, None
    add("--format", choices=["text", "stream-json"])
    return chat_parser
'''


def _parser(root: Path, content: str = PARSER) -> Path:
    path = root / "hermes_cli" / "_parser.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_readiness_reads_real_shaped_source_and_revision_without_any_execution(tmp_path, monkeypatch):
    _venv_cli(tmp_path)
    _parser(tmp_path, "raise RuntimeError('source must never be imported')\n" + PARSER)
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/master\n")
    (git / "refs" / "heads" / "master").write_text("a" * 40)
    monkeypatch.setattr(readiness.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("readiness must never launch a subprocess"))
    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = readiness.readiness(tmp_path)
    assert result["source_protocol"]["state"] == "declared"
    assert result["agent_source"]["revision"] == "a" * 40
    assert result["finite_protocol"]["state"] == "not_observed"
    assert before == {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


@pytest.mark.parametrize("content,state", [
    (PARSER.replace('"stream-json"', '"json"'), "not_declared"),
    (PARSER.replace('choices=["text", "stream-json"]', 'choices=dynamic_choices'), "unavailable"),
    (PARSER.replace('    add("--format"', '    add = print\n    add("--format"'), "unavailable"),
    (PARSER.replace('    add("--format"', '    chat_parser = None\n    add("--format"'), "unavailable"),
    (PARSER.replace('    add, inherited', '    add("--format", choices=["stream-json"])\n    add, inherited'), "unavailable"),
    (PARSER.replace('    chat_parser = subparsers.add_parser("chat")\n    add, inherited, SUPPRESS = chat_parser.add_argument, None, None', '    add, inherited, SUPPRESS = chat_parser.add_argument, None, None\n    chat_parser = subparsers.add_parser("chat")'), "unavailable"),
    ("# --format stream-json\nraise RuntimeError('do not import')", "unavailable"),
    ("invalid syntax!!!!", "unavailable"),
    ("x" * (readiness.MAX_PROBE_BYTES + 1), "unavailable"),
])
def test_static_declaration_requires_recognized_literal_bindings(tmp_path, content, state):
    _parser(tmp_path, content)
    result = readiness.source_protocol(tmp_path)
    assert result["state"] == state
    assert content not in json.dumps(result)


@pytest.mark.skipif(os.name == "nt", reason="symlink permissions differ on Windows")
def test_parser_and_git_ref_cannot_escape_selected_metadata_roots(tmp_path):
    root = tmp_path / "agent"
    root.mkdir()
    outside = tmp_path / "private-source"
    outside.write_text(PARSER)
    (root / "hermes_cli").mkdir()
    (root / "hermes_cli" / "_parser.py").symlink_to(outside)
    assert readiness.source_protocol(root)["state"] == "unavailable"
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").symlink_to(outside)
    assert readiness._revision(root) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX named-pipe fixture")
def test_named_pipe_cannot_turn_source_metadata_read_into_a_blocking_read(tmp_path):
    parser = tmp_path / "hermes_cli" / "_parser.py"
    parser.parent.mkdir()
    os.mkfifo(parser)
    started = time.monotonic()
    assert readiness.source_protocol(tmp_path)["state"] == "unavailable"
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("state,code", [("supported", "SUPPORTED"), ("unsupported", "FORMAT_NOT_ADVERTISED"), ("unavailable", "PROBE_FAILED")])
def test_only_prior_matching_authorized_preflight_is_visible_and_changes_invalidate(tmp_path, monkeypatch, state, code):
    cli = _venv_cli(tmp_path)
    monkeypatch.setattr(readiness, "probe_stream_json", lambda *args: {"state": state, "code": code, "checked_at": "prior-observation"})
    assert readiness.readiness(tmp_path)["finite_protocol"]["state"] == "not_observed"
    assert session._supports_stream_json(str(cli), {}) is (state == "supported")
    result = readiness.readiness(tmp_path)
    assert result["finite_protocol"] == {"state": state, "code": code, "checked_at": "prior-observation", "provenance": "authorized_session_preflight"}
    assert result["checked_at"] != "prior-observation"
    stamp = cli.stat()
    replacement = cli.with_suffix(".replacement")
    replacement.write_bytes(b"X" * stamp.st_size)
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    replacement.replace(cli)
    assert readiness.readiness(tmp_path)["finite_protocol"]["state"] == "not_observed"
    readiness._preflights.clear()
    assert readiness.readiness(tmp_path)["finite_protocol"]["state"] == "not_observed"


def test_prior_observation_store_is_bounded_and_change_during_probe_is_not_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness, "probe_stream_json", lambda *args: {"state": "unavailable", "code": "PROBE_FAILED", "checked_at": "prior"})
    for index in range(20):
        readiness.observe_session_preflight(str(tmp_path / str(index)), {})
    assert len(readiness._preflights) <= 16
    cli = _venv_cli(tmp_path)
    def change(*args):
        cli.write_bytes(b"changed mid-probe")
        return {"state": "supported", "code": "SUPPORTED", "checked_at": "prior"}
    monkeypatch.setattr(readiness, "probe_stream_json", change)
    readiness.observe_session_preflight(str(cli), {})
    assert readiness.readiness(tmp_path)["finite_protocol"]["state"] == "not_observed"


def test_real_pinned_agent_source_declaration_without_import_or_execution(monkeypatch):
    configured = os.environ.get("HERMES_AGENT_ROOT")
    if not configured:
        if os.environ.get("HERMES_GPT_REQUIRE_AGENT_LOADER") == "1":
            pytest.fail("pinned Agent source required for the interface CI lane")
        pytest.skip("no explicit pinned Agent source checkout")
    root = Path(configured)
    monkeypatch.setattr(readiness.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("static interface inspection must not execute Agent code"))
    result = readiness.source_protocol(root)
    assert result["state"] == "declared", result
    assert result["parser_sha256"] in {
        "5b3f30a1e48799d563fdceec337c31c49f395242b80742ef15f6799e47e83cb3",
        "75fa3070d9cdcbeaa3fc1424e9c3dba06a403b9d43c74aaccc3b82829edf366c",
    }
