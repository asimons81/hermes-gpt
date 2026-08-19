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

- Linux: ``bubblewrap`` (``bwrap``) with only required system/runtime trees
  exposed read-only, private ``/tmp`` and ``/run``, and the authorized
  workspace bound read-write at its original absolute path.
- macOS: ``sandbox-exec`` with an equivalent no-host-write-outside profile.

If the OS confinement tool is unavailable, confinement is not considered
active and write-capable runner tools stay disabled (fail closed).

The module also provides ``confine_path`` — a pure path-containment
check (absolute-escape, ``..`` traversal, symlink escape) used to
validate contract artifact paths against the authorized workspace.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

CONFINEMENT_ENABLE_ENV = "HERMES_GPT_ENABLE_RUNNER_CONFINEMENT"

_LINUX_TOOL = "bwrap"
_MACOS_TOOL = "sandbox-exec"
_PROBE_TIMEOUT_SECONDS = 5
_MAX_WORKSPACE_SCAN_ENTRIES = 200_000


def confinement_enabled() -> bool:
    """Return True only when confinement is explicitly opted into."""
    return os.environ.get(CONFINEMENT_ENABLE_ENV, "").strip().lower() in {"1", "true", "yes"}


def confinement_tool() -> str | None:
    """Return the absolute path of the OS confinement binary, if present."""
    if os.name != "posix":
        return None
    if sys.platform.startswith("linux"):
        path = shutil.which(_LINUX_TOOL)
    elif sys.platform == "darwin":
        path = shutil.which(_MACOS_TOOL)
    else:
        path = None
    return str(Path(path).resolve()) if path else None


def _macos_quote(value: str) -> str:
    """Escape a path for a sandbox-exec profile string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _macos_sandbox_profile(workspace: str) -> str:
    escaped = _macos_quote(workspace)
    return (
        "(version 1)(allow default)(deny file-write*)"
        f'(allow file-write* (subpath "{escaped}"))'
    )


_LINUX_RO_PATHS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt", "/nix")


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _decode_mountinfo_path(value: str) -> str:
    """Decode the octal escapes used for mount paths in /proc/*/mountinfo."""
    replacements = {"\\040": " ", "\\011": "\t", "\\012": "\n", "\\134": "\\"}
    for encoded, decoded in replacements.items():
        value = value.replace(encoded, decoded)
    return value


def _linux_nested_mounts(root: Path) -> list[Path]:
    """Return mount points strictly below ``root`` from Linux mountinfo."""
    if not sys.platform.startswith("linux"):
        return []
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PermissionError("unable to inspect Linux mount table for confined workspace") from exc

    nested: list[Path] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            raise PermissionError("malformed Linux mount table while validating confined workspace")
        mount_point = Path(_decode_mountinfo_path(fields[4]))
        if mount_point != root and _path_within(mount_point, root):
            nested.append(mount_point)
    return nested


def _runtime_readonly_root(argv: list[str], workspace: Path) -> Path | None:
    """Return the smallest practical extra read-only tree needed by argv[0]."""
    if not argv:
        return None
    raw = Path(argv[0]).expanduser()
    if not raw.is_absolute():
        return None
    try:
        executable = raw.resolve()
    except OSError:
        executable = raw
    for system_root in (Path(item) for item in _LINUX_RO_PATHS):
        if _path_within(executable, system_root):
            return None
    # Pi's package CLI is commonly installed below ~/.local/.../node_modules.
    # Bind the node_modules tree so relative package imports keep working while
    # the rest of the user's home directory remains absent from the sandbox.
    for parent in executable.parents:
        if parent.name == "node_modules":
            return parent
    parent = executable.parent
    if _path_within(parent, workspace):
        return None
    return parent


def _wrap_argv_with_tool(argv: list[str], workspace: Path, tool: str) -> list[str]:
    """Build the platform confinement argv with an already-resolved tool."""
    workspace_path = Path(workspace).expanduser().resolve()
    workspace_resolved = str(workspace_path)
    if sys.platform.startswith("linux"):
        wrapped = [tool]
        for source in _LINUX_RO_PATHS:
            wrapped += ["--ro-bind-try", source, source]
        # Do not expose host runtime sockets or host temporary files. Pi still
        # has normal writable scratch space, but it is private to the sandbox.
        wrapped += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run"]
        runtime_root = _runtime_readonly_root(argv, workspace_path)
        if runtime_root is not None:
            runtime = str(runtime_root)
            wrapped += ["--ro-bind", runtime, runtime]
        wrapped += [
            "--bind", workspace_resolved, workspace_resolved,
            "--chdir", workspace_resolved,
            "--unshare-pid",
            "--cap-drop", "ALL",
            "--new-session",
            "--die-with-parent",
            *argv,
        ]
        return wrapped
    if sys.platform == "darwin":
        return [tool, "-p", _macos_sandbox_profile(workspace_resolved), *argv]
    raise RuntimeError(f"confinement unsupported on platform {sys.platform!r}")


def _probe_confinement(tool: str) -> bool:
    """Prove the backend can enforce the intended write boundary on this host.

    The probe is deliberately bounded and fail-closed. A child must be able to
    create a host-visible marker inside a temporary workspace while an attempted
    sibling write outside that workspace must not modify the host path (it may
    be denied or land in a private sandbox filesystem). This catches hosts where
    the confinement binary exists but cannot create the required namespace/
    profile (for example restricted Linux user namespaces in containers).
    """
    shell = "/bin/sh" if Path("/bin/sh").is_file() else shutil.which("sh")
    if not shell:
        return False
    probe_script = 'printf ok > "$1" || exit 71; printf escape > "$2" 2>/dev/null || true; exit 0'
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-gpt-confinement-probe-") as temp_dir:
            root = Path(temp_dir).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            inside = workspace / "inside.marker"
            outside = root / "outside.marker"
            wrapped = _wrap_argv_with_tool(
                [shell, "-c", probe_script, "hermes-gpt-probe", str(inside), str(outside)],
                workspace,
                tool,
            )
            completed = subprocess.run(
                wrapped,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            return completed.returncode == 0 and inside.is_file() and not outside.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def confinement_available() -> bool:
    """Return True only when the opted-in confinement backend is usable.

    Binary presence alone is not sufficient. Availability requires a bounded
    capability probe that demonstrates a host-writable workspace while an
    attempted write outside it cannot modify the corresponding host path, so
    restricted/containerized hosts fail closed.
    """
    if not confinement_enabled():
        return False
    tool = confinement_tool()
    return bool(tool and _probe_confinement(tool))


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


def validate_workspace_boundary(workspace: Path) -> Path:
    """Fail closed on workspace aliases that can bypass path/mount isolation.

    A pre-existing hard link inside the writable workspace can reference the
    same inode as a file outside it. A bind-mount sandbox cannot distinguish
    those aliases: writing the in-workspace name would mutate the out-of-scope
    file. We therefore prove that every regular-file hard link is fully
    accounted for inside the workspace before granting write tools. Nested
    filesystems/mounts are also rejected so the writable subtree cannot smuggle
    a second host filesystem through the authorized path.
    """
    root = Path(workspace).expanduser().resolve()
    try:
        root_stat = root.stat()
    except OSError as exc:
        raise PermissionError(f"confined workspace is not accessible: {root}") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise PermissionError(f"confined workspace is not a directory: {root}")

    nested_mounts = _linux_nested_mounts(root)
    if nested_mounts:
        raise PermissionError(f"confined workspace contains a nested mount point: {nested_mounts[0]}")

    inode_counts: dict[tuple[int, int], int] = {}
    inode_nlinks: dict[tuple[int, int], int] = {}
    inode_sample: dict[tuple[int, int], Path] = {}
    entries = 0

    def _walk_error(exc: OSError) -> None:
        raise exc

    try:
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=_walk_error):
            current_path = Path(current)
            for name in [*dirs, *files]:
                entries += 1
                if entries > _MAX_WORKSPACE_SCAN_ENTRIES:
                    raise PermissionError(
                        f"confined workspace exceeds {_MAX_WORKSPACE_SCAN_ENTRIES} scan entries"
                    )
                path = current_path / name
                entry_stat = path.lstat()
                if stat.S_ISLNK(entry_stat.st_mode):
                    continue
                if entry_stat.st_dev != root_stat.st_dev:
                    raise PermissionError(f"confined workspace contains a nested filesystem: {path}")
                if stat.S_ISREG(entry_stat.st_mode):
                    key = (entry_stat.st_dev, entry_stat.st_ino)
                    inode_counts[key] = inode_counts.get(key, 0) + 1
                    inode_nlinks[key] = int(entry_stat.st_nlink)
                    inode_sample.setdefault(key, path)
    except PermissionError:
        raise
    except OSError as exc:
        raise PermissionError(f"unable to validate confined workspace boundary: {root}") from exc

    for key, count in inode_counts.items():
        if inode_nlinks[key] > count:
            raise PermissionError(
                f"confined workspace contains a hard link with an alias outside the workspace: {inode_sample[key]}"
            )
    return root


def wrap_argv(argv: list[str], workspace: Path) -> list[str]:
    """Wrap ``argv`` so the child process is confined to ``workspace``.

    Linux: bubblewrap exposes only required runtime trees read-only, provides
    private temporary/runtime filesystems, and binds only the authorized
    workspace from the host read-write. macOS: ``sandbox-exec`` applies an
    equivalent no-host-write-outside profile. Raises ``RuntimeError`` when no
    confinement tool is installed; callers that need a trust-boundary decision
    must gate on :func:`confinement_available`, which additionally probes that
    the backend is usable on this host.
    """
    tool = confinement_tool()
    if tool is None:
        raise RuntimeError("no OS confinement tool available (install bubblewrap or sandbox-exec)")
    validated_workspace = validate_workspace_boundary(workspace)
    return _wrap_argv_with_tool(argv, validated_workspace, tool)
