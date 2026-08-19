"""Filesystem confinement for runner sessions.

This module provides the real filesystem confinement boundary that
``operator_runners`` needs before write-capable runner tools (Pi
``edit``/``write``/``bash``) may be enabled.

Design
------
CWD is not a sandbox. A process started with ``cwd=workspace`` can still
touch absolute paths, traverse with ``..``, ``cd`` in a shell, or follow
symlinks out of the workspace. Confinement therefore uses OS-level
mechanisms rather than working-directory conventions:

- Linux: ``bubblewrap`` (``bwrap``) with the contract workspace bound
  read-write at ``/workspace`` and the host filesystem bound read-only.
  The confined process physically cannot write outside the workspace.
- macOS: ``sandbox-exec`` with an equivalent no-write-outside profile.

If the OS confinement tool is unavailable, confinement is not considered
active and write-capable runner tools stay disabled (fail closed).

The module also provides ``confine_path`` — a pure path-containment
check (absolute-escape, ``..`` traversal, symlink escape) used to
validate contract artifact paths against the authorized workspace.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

CONFINEMENT_ENABLE_ENV = "HERMES_GPT_ENABLE_RUNNER_CONFINEMENT"
WORKSPACE_ROOT_ENV = "HERMES_GPT_RUNNER_WORKSPACE_ROOT"

DEFAULT_WORKSPACE_DIR = "runner-workspaces"

_LINUX_TOOL = "bwrap"
_MACOS_TOOL = "sandbox-exec"


def confinement_enabled() -> bool:
    """Return True only when confinement is explicitly opted into."""
    return os.environ.get(CONFINEMENT_ENABLE_ENV, "").strip().lower() in {"1", "true", "yes"}


def confinement_tool() -> str | None:
    """Return the absolute path of the OS confinement binary, if present."""
    if os.name != "posix":
        return None
    import sys

    if sys.platform.startswith("linux"):
        path = shutil.which(_LINUX_TOOL)
    elif sys.platform == "darwin":
        path = shutil.which(_MACOS_TOOL)
    else:
        path = None
    return str(Path(path).resolve()) if path else None


def confinement_available() -> bool:
    """Return True when confinement is enabled AND an OS tool backs it.

    This is the gate write-capable runner tools must check: it fails closed
    when the binary is missing so a missing ``bwrap`` can never silently
    downgrade confinement to a CWD-only pretense.
    """
    return confinement_enabled() and confinement_tool() is not None


def confinement_workspace_root(hermes_root: Path | None = None) -> Path:
    """Return the root directory for confined runner workspaces.

    Default: ``<hermes_root>/runner-workspaces``. Override with
    ``HERMES_GPT_RUNNER_WORKSPACE_ROOT``.
    """
    configured = os.environ.get(WORKSPACE_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    root = hermes_root if hermes_root is not None else Path.home() / ".hermes"
    return Path(root).expanduser() / DEFAULT_WORKSPACE_DIR


def confined_workspace(task_id: str, hermes_root: Path | None = None) -> Path:
    """Create and return the per-job confined workspace directory.

    The task id is sanitized to a single path component so a hostile task id
    cannot traverse outside the workspace root.
    """
    safe = "".join(ch if (ch.isalnum() or ch in {"-", "_"}) else "_" for ch in task_id)
    if not safe or safe.startswith("."):
        raise ValueError("task id must contain at least one alphanumeric character")
    workspace = confinement_workspace_root(hermes_root) / safe / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def confine_path(base: Path, candidate: str | Path) -> Path:
    """Validate ``candidate`` lies inside ``base`` and return the resolved path.

    Rejects absolute paths outside the workspace, ``..`` traversal that
    escapes it, and symlinks that resolve outside it. Raises
    ``PermissionError`` on any escape.
    """
    base_resolved = Path(base).expanduser().resolve()
    raw = Path(str(candidate)).expanduser()
    candidate_resolved = raw.resolve() if raw.is_absolute() else (base_resolved / raw).resolve()
    try:
        candidate_resolved.relative_to(base_resolved)
    except ValueError:
        raise PermissionError(
            f"path {str(candidate)!r} escapes the confined workspace {str(base_resolved)!r}"
        ) from None
    return candidate_resolved


def _macos_sandbox_profile(workspace: str) -> str:
    return (
        "(version 1)(allow default)(deny file-write*)"
        f"(allow file-write* (subpath \"{workspace}\"))"
    )


def wrap_argv(argv: list[str], workspace: Path) -> list[str]:
    """Wrap ``argv`` so the child process is confined to ``workspace``.

    Linux: bubblewrap with the workspace bound read-write at its own path
    and host system directories bound read-only. macOS: ``sandbox-exec``
    with a no-write-outside profile. Raises ``RuntimeError`` when no
    confinement tool is available (callers must gate on
    ``confinement_available()`` first).
    """
    import sys

    workspace_resolved = str(Path(workspace).expanduser().resolve())
    tool = confinement_tool()
    if tool is None:
        raise RuntimeError("no OS confinement tool available (install bubblewrap or sandbox-exec)")
    if sys.platform.startswith("linux"):
        return [
            tool,
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/sbin", "/sbin",
            "--ro-bind-try", "/etc", "/etc",
            "--ro-bind-try", "/opt", "/opt",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", workspace_resolved, workspace_resolved,
            "--chdir", workspace_resolved,
            "--unshare-pid",
            "--die-with-parent",
            *argv,
        ]
    if sys.platform == "darwin":
        return [tool, "-p", _macos_sandbox_profile(workspace_resolved), *argv]
    raise RuntimeError(f"confinement unsupported on platform {sys.platform!r}")
