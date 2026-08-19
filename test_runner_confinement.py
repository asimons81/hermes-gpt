"""Tests for runner filesystem confinement (runner_confinement)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import runner_confinement as confinement


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(confinement.CONFINEMENT_ENABLE_ENV, raising=False)


def test_confinement_disabled_by_default():
    assert confinement.confinement_enabled() is False
    assert confinement.confinement_available() is False


def test_confinement_requires_os_tool(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(confinement.CONFINEMENT_ENABLE_ENV, "1")
    monkeypatch.setattr(confinement, "confinement_tool", lambda: None)
    assert confinement.confinement_available() is False


def test_confinement_requires_successful_capability_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(confinement.CONFINEMENT_ENABLE_ENV, "1")
    monkeypatch.setattr(confinement, "confinement_tool", lambda: "/usr/bin/fake-confinement")
    monkeypatch.setattr(confinement, "_probe_confinement", lambda tool: False)
    assert confinement.confinement_available() is False

    monkeypatch.setattr(confinement, "_probe_confinement", lambda tool: True)
    assert confinement.confinement_available() is True


def test_probe_failure_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        confinement,
        "_wrap_argv_with_tool",
        lambda argv, workspace, tool: ["/usr/bin/fake-confinement", *argv],
    )
    monkeypatch.setattr(
        confinement.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1),
    )
    assert confinement._probe_confinement("/usr/bin/fake-confinement") is False


def test_probe_timeout_is_fail_closed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        confinement,
        "_wrap_argv_with_tool",
        lambda argv, workspace, tool: ["/usr/bin/fake-confinement", *argv],
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=confinement._PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(confinement.subprocess, "run", timeout)
    assert confinement._probe_confinement("/usr/bin/fake-confinement") is False


def test_confine_path_accepts_inside(tmp_path: Path):
    base = tmp_path / "ws"
    base.mkdir()
    inside = base / "sub" / "file.txt"
    resolved = confinement.confine_path(base, inside)
    assert str(resolved).startswith(str(base.resolve()))


def test_confine_path_rejects_dotdot_escape(tmp_path: Path):
    base = tmp_path / "ws"
    base.mkdir()
    with pytest.raises(PermissionError):
        confinement.confine_path(base, "../outside.txt")


def test_confine_path_rejects_absolute_outside(tmp_path: Path):
    base = tmp_path / "ws"
    base.mkdir()
    with pytest.raises(PermissionError):
        confinement.confine_path(base, tmp_path / "other" / "secret")


def test_confine_path_rejects_symlink_escape(tmp_path: Path):
    base = tmp_path / "ws"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = base / "escape"
    link.symlink_to(outside)
    with pytest.raises(PermissionError):
        confinement.confine_path(base, link)


def test_workspace_boundary_rejects_hardlink_alias_outside(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("safe")
    os.link(outside, ws / "alias.txt")
    with pytest.raises(PermissionError, match="hard link"):
        confinement.validate_workspace_boundary(ws)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux mountinfo only")
def test_linux_nested_mount_parser_handles_escaped_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "work space"
    ws.mkdir()
    escaped_ws = str(ws).replace(" ", "\\040")
    nested = ws / "nested mount"
    escaped_nested = str(nested).replace(" ", "\\040")
    mountinfo = (
        f"10 1 0:1 / {escaped_ws} rw - tmpfs tmpfs rw\n"
        f"11 10 0:2 / {escaped_nested} rw - tmpfs tmpfs rw\n"
    )
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: mountinfo)
    assert confinement._linux_nested_mounts(ws) == [nested]


def test_workspace_boundary_rejects_nested_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(confinement, "_linux_nested_mounts", lambda root: [root / "mounted"])
    with pytest.raises(PermissionError, match="nested mount point"):
        confinement.validate_workspace_boundary(ws)


def test_workspace_boundary_allows_hardlinks_fully_inside(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    first = ws / "first.txt"
    first.write_text("safe")
    os.link(first, ws / "second.txt")
    assert confinement.validate_workspace_boundary(ws) == ws.resolve()


def test_macos_profile_escapes_workspace_literal():
    profile = confinement._macos_sandbox_profile('/tmp/a"b\\c\nworkspace')
    assert 'subpath "/tmp/a\\"b\\\\c\\nworkspace"' in profile
    assert '\nworkspace")' not in profile


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("bwrap") is None, reason="requires bwrap on linux")
def test_wrap_argv_linux_shape(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    wrapped = confinement.wrap_argv(["/usr/bin/true"], ws)
    assert wrapped[0].endswith("bwrap")
    assert wrapped[1:4] == ["--ro-bind-try", "/usr", "/usr"]
    assert ["--tmpfs", "/tmp"] == wrapped[wrapped.index("--tmpfs"):wrapped.index("--tmpfs") + 2]
    assert "--run" not in wrapped
    assert "/run" in wrapped
    assert "--bind" in wrapped
    cap_drop = wrapped.index("--cap-drop")
    assert wrapped[cap_drop:cap_drop + 2] == ["--cap-drop", "ALL"]
    assert "--new-session" in wrapped
    assert str(ws.resolve()) in wrapped


def test_wrap_argv_fails_without_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(confinement, "confinement_tool", lambda: None)
    with pytest.raises(RuntimeError):
        confinement.wrap_argv(["pi"], tmp_path)
