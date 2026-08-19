"""Tests for confinement-aware pi_rpc tool policy in operator_runners."""

from __future__ import annotations

from pathlib import Path

import pytest

import operator_runners as runners
import runner_confinement as confinement
from test_operator_runners import _contract, _enable_workspace


@pytest.fixture(autouse=True)
def _clean_confinement_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(confinement.CONFINEMENT_ENABLE_ENV, raising=False)


def _confinement_active(monkeypatch: pytest.MonkeyPatch, active: bool) -> None:
    monkeypatch.setattr(confinement, "confinement_available", lambda *, writable=True: active)
    monkeypatch.setattr(confinement, "confinement_enabled", lambda: active)


def test_writable_pi_rejected_when_confinement_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _confinement_active(monkeypatch, False)
    raw = _contract(tmp_path, backend="pi_rpc", options={"tools": "read,write", "sandbox": "workspace-write"})
    with pytest.raises(PermissionError):
        runners._pi_tools(raw)


def test_writable_pi_allowed_when_confinement_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _confinement_active(monkeypatch, True)
    raw = _contract(tmp_path, backend="pi_rpc", options={"tools": "read,write,edit", "sandbox": "workspace-write"})
    assert runners._pi_tools(raw) == "read,write,edit"


@pytest.mark.parametrize("auth_class", ["none", "read_only"])
@pytest.mark.parametrize("write_tool", ["write", "edit", "bash"])
def test_read_authorization_cannot_gain_write_tools_with_confinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_class: str,
    write_tool: str,
):
    _confinement_active(monkeypatch, True)
    raw = _contract(
        tmp_path,
        backend="pi_rpc",
        options={"tools": f"read,{write_tool}", "sandbox": "workspace-write"},
    )
    raw["authorization"] = {"class": auth_class, "approved": True}
    with pytest.raises(PermissionError, match="authorization.class"):
        runners._pi_tools(raw)


def test_writable_pi_requires_workspace_write_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _confinement_active(monkeypatch, True)
    raw = _contract(tmp_path, backend="pi_rpc", options={"tools": "read,write", "sandbox": "read-only"})
    with pytest.raises(PermissionError, match="workspace-write"):
        runners._pi_tools(raw)


def test_read_only_pi_rejected_without_confinement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _confinement_active(monkeypatch, False)
    raw = _contract(tmp_path, backend="pi_rpc", options={"tools": "read"})
    raw["authorization"] = {"class": "read_only", "approved": True}
    with pytest.raises(PermissionError, match="read-only.*confinement"):
        runners._pi_tools(raw)

    raw_default = _contract(tmp_path, backend="pi_rpc")
    raw_default["authorization"] = {"class": "read_only", "approved": True}
    with pytest.raises(PermissionError, match="read-only.*confinement"):
        runners._pi_tools(raw_default)


def test_read_only_default_still_rejects_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _confinement_active(monkeypatch, True)
    raw = _contract(tmp_path, backend="pi_rpc", options={"tools": "read,bash"})
    # No sandbox specified -> write tools still rejected.
    with pytest.raises(PermissionError):
        runners._pi_tools(raw)


def test_worker_pi_wraps_argv_under_confinement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    _confinement_active(monkeypatch, True)
    captured: dict = {}

    def fake_wrap(argv, workspace, *, writable=True):
        captured["workspace"] = workspace
        captured["writable"] = writable
        return ["/usr/bin/bwrap", *argv]

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        raise RuntimeError("stop after spawn")

    monkeypatch.setattr(confinement, "wrap_argv", fake_wrap)
    monkeypatch.setattr(runners, "_popen_process_group", fake_popen)
    raw = _contract(ws, backend="pi_rpc", options={"tools": "read,write", "sandbox": "workspace-write"})
    with pytest.raises(RuntimeError, match="stop after spawn"):
        runners._worker_pi("/bin/true", raw, 1, tmp_path / "log.jsonl", root)
    assert captured["argv"][0] == "/usr/bin/bwrap"
    assert captured["workspace"] == ws.resolve()
    assert captured["writable"] is True


def test_worker_pi_read_only_uses_read_only_workspace_confinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    _confinement_active(monkeypatch, True)
    captured: dict = {}

    def fake_wrap(argv, workspace, *, writable=True):
        captured["workspace"] = workspace
        captured["writable"] = writable
        captured["inner_argv"] = list(argv)
        return ["/usr/bin/bwrap", *argv]

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        raise RuntimeError("stop after spawn")

    monkeypatch.setattr(confinement, "wrap_argv", fake_wrap)
    monkeypatch.setattr(runners, "_popen_process_group", fake_popen)
    raw = _contract(ws, backend="pi_rpc", options={"tools": "read", "sandbox": "read-only"})
    raw["authorization"] = {"class": "read_only", "approved": True}
    with pytest.raises(RuntimeError, match="stop after spawn"):
        runners._worker_pi("/bin/true", raw, 1, tmp_path / "log.jsonl", root)

    assert captured["argv"][0] == "/usr/bin/bwrap"
    assert captured["workspace"] == ws.resolve()
    assert captured["writable"] is False
    assert "--no-extensions" in captured["inner_argv"]
