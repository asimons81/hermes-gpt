"""Tests for runner filesystem confinement (runner_confinement)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import runner_confinement as confinement


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(confinement.CONFINEMENT_ENABLE_ENV, raising=False)
    monkeypatch.delenv(confinement.WORKSPACE_ROOT_ENV, raising=False)


def test_confinement_disabled_by_default():
    assert confinement.confinement_enabled() is False
    assert confinement.confinement_available() is False


def test_confinement_requires_os_tool(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(confinement.CONFINEMENT_ENABLE_ENV, "1")
    if confinement.confinement_tool() is None:
        # Enabled but no tool: must fail closed.
        assert confinement.confinement_available() is False
    else:
        assert confinement.confinement_available() is True


def test_workspace_root_default(tmp_path: Path):
    root = confinement.confinement_workspace_root(tmp_path)
    assert root == tmp_path / confinement.DEFAULT_WORKSPACE_DIR


def test_workspace_root_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    override = tmp_path / "custom"
    monkeypatch.setenv(confinement.WORKSPACE_ROOT_ENV, str(override))
    assert confinement.confinement_workspace_root(tmp_path) == override


def test_confined_workspace_creates_directory(tmp_path: Path):
    ws = confinement.confined_workspace("task-123", tmp_path)
    assert ws.is_dir()
    assert ws == tmp_path / confinement.DEFAULT_WORKSPACE_DIR / "task-123" / "workspace"


def test_confined_workspace_rejects_hostile_task_id(tmp_path: Path):
    # Traversal characters are sanitized away: the task id must resolve to a
    # single component under the workspace root.
    ws = confinement.confined_workspace("../escape", tmp_path)
    assert ws.parent.parent == tmp_path / confinement.DEFAULT_WORKSPACE_DIR
    assert "/" not in ws.parent.name and ".." not in ws.parent.name
    with pytest.raises(ValueError):
        confinement.confined_workspace("", tmp_path)


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


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("bwrap") is None, reason="requires bwrap on linux")
def test_wrap_argv_linux_shape(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    wrapped = confinement.wrap_argv(["pi", "--mode", "rpc"], ws)
    assert wrapped[0].endswith("bwrap")
    assert "--bind" in wrapped
    assert str(ws.resolve()) in wrapped


def test_wrap_argv_fails_without_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(confinement, "confinement_tool", lambda: None)
    with pytest.raises(RuntimeError):
        confinement.wrap_argv(["pi"], tmp_path)
