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
    monkeypatch.setattr(confinement, "_probe_confinement", lambda tool, *, writable=True: False)
    assert confinement.confinement_available() is False

    monkeypatch.setattr(confinement, "_probe_confinement", lambda tool, *, writable=True: True)
    assert confinement.confinement_available() is True
    assert confinement.confinement_available(writable=False) is True


def test_probe_failure_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        confinement,
        "_wrap_argv_with_tool",
        lambda argv, workspace, tool, *, writable=True: ["/usr/bin/fake-confinement", *argv],
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
        lambda argv, workspace, tool, *, writable=True: ["/usr/bin/fake-confinement", *argv],
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


def test_workspace_boundary_rejects_outward_symlink(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (ws / "escape-link").symlink_to(outside)
    with pytest.raises(PermissionError, match="outward symlink"):
        confinement.validate_workspace_boundary(ws)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX fifo support")
def test_workspace_boundary_rejects_special_files(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fifo = ws / "channel"
    os.mkfifo(fifo)
    with pytest.raises(PermissionError, match="special filesystem entry"):
        confinement.validate_workspace_boundary(ws)


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


def test_runtime_readonly_root_scopes_to_node_package(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    package = tmp_path / "lib" / "node_modules" / "@scope" / "pi-agent"
    executable = package / "dist" / "cli.js"
    executable.parent.mkdir(parents=True)
    executable.write_text("// cli", encoding="utf-8")
    assert confinement._runtime_readonly_root([str(executable)], ws) == package.resolve()


def test_macos_profile_escapes_workspace_literal():
    profile = confinement._macos_sandbox_profile('/tmp/a"b\\c\nworkspace', writable=True)
    assert 'subpath "/tmp/a\\"b\\\\c\\nworkspace"' in profile
    assert '\nworkspace")' not in profile


def test_macos_writable_profile_denies_host_reads_outside_runtime_and_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    profile = confinement._macos_sandbox_profile(str(ws), writable=True)
    assert "(deny file-read*)" in profile
    assert f'(allow file-read* (subpath "{ws}"))' in profile
    assert f'(allow file-write* (subpath "{ws}"))' in profile


def test_macos_profile_does_not_expose_broad_config_trees(tmp_path: Path):
    ws = tmp_path / "ws"
    profile = confinement._macos_sandbox_profile(str(ws), writable=False)
    assert '(subpath "/Library")' not in profile
    assert '(subpath "/etc")' not in profile
    assert '(subpath "/private/etc")' not in profile
    assert '(literal "/etc/hosts")' in profile
    assert '(literal "/private/etc/resolv.conf")' in profile
    assert '(literal "/private/var/run/resolv.conf")' in profile


def test_macos_node_runtime_root_scopes_homebrew_keg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    node_root = tmp_path / "homebrew" / "Cellar" / "node" / "22.0.0"
    executable = node_root / "bin" / "node"
    executable.parent.mkdir(parents=True)
    executable.write_text("node", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: str(executable) if name == "node" else None)
    assert confinement._macos_node_runtime_root() == node_root.resolve()


def test_macos_read_only_profile_denies_host_reads_and_workspace_writes(tmp_path: Path):
    ws = tmp_path / "ws"
    runtime = tmp_path / "runtime"
    profile = confinement._macos_sandbox_profile(
        str(ws),
        writable=False,
        runtime_root=runtime,
    )
    assert "(deny file-read*)" in profile
    assert "(deny file-write*)" in profile
    assert f'(allow file-read* (subpath "{ws}"))' in profile
    assert f'(allow file-read* (subpath "{runtime}"))' in profile
    assert "allow file-write*" not in profile


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
    assert "--proc" not in wrapped
    assert ["--dir", "/proc"] in [wrapped[index:index + 2] for index in range(len(wrapped) - 1)]
    assert ["--ro-bind-try", "/etc", "/etc"] not in [
        wrapped[index:index + 3] for index in range(len(wrapped) - 2)
    ]
    assert "--bind" in wrapped
    cap_drop = wrapped.index("--cap-drop")
    assert wrapped[cap_drop:cap_drop + 2] == ["--cap-drop", "ALL"]
    assert "--new-session" in wrapped
    assert str(ws.resolve()) in wrapped


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("bwrap") is None, reason="requires bwrap on linux")
def test_wrap_argv_linux_read_only_shape(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    wrapped = confinement.wrap_argv(["/usr/bin/true"], ws, writable=False)
    workspace = str(ws.resolve())
    assert ["--ro-bind", workspace, workspace] == wrapped[
        wrapped.index(workspace) - 1:wrapped.index(workspace) + 2
    ]
    assert ["--bind", workspace, workspace] not in [wrapped[index:index + 3] for index in range(len(wrapped) - 2)]


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("bwrap") is None, reason="requires bwrap on linux")
def test_read_only_linux_boundary_blocks_absolute_dotdot_and_symlink_reads(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    inside = ws / "inside.txt"
    outside = tmp_path / "outside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    escape_link = ws / "escape-link"
    dotdot = ws / ".." / outside.name
    script = (
        'test "$(cat "$1")" = inside || exit 81; '
        'cat "$2" >/dev/null 2>&1 && exit 82; '
        'cat "$3" >/dev/null 2>&1 && exit 83; '
        'cat "$4" >/dev/null 2>&1 && exit 84; '
        'printf mutate > "$1" 2>/dev/null && exit 85; '
        'exit 0'
    )
    wrapped = confinement.wrap_argv(
        [
            "/bin/sh",
            "-c",
            script,
            "read-boundary-test",
            str(inside),
            str(outside),
            str(dotdot),
            str(escape_link),
        ],
        ws,
        writable=False,
    )
    # Simulate a host-side symlink appearing after validation. The OS boundary
    # must still prevent the child from following it to the out-of-scope file.
    escape_link.symlink_to(outside)
    completed = subprocess.run(wrapped, capture_output=True, text=True, check=False, timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert inside.read_text(encoding="utf-8") == "inside"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_wrap_argv_fails_without_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(confinement, "confinement_tool", lambda: None)
    with pytest.raises(RuntimeError):
        confinement.wrap_argv(["pi"], tmp_path)
