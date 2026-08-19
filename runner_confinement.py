"""Filesystem confinement for runner sessions.

This module provides the real filesystem confinement boundary used by
``operator_runners`` for every Pi session. Read-only sessions are physically
scoped to the authorized workspace; write-capable sessions additionally permit
workspace writes only after the runner policy has authorized them.

Design
------
CWD is not a sandbox. A process started with ``cwd=workspace`` can still
touch absolute paths, traverse with ``..``, ``cd`` in a shell, or follow
symlinks out of the workspace. Confinement therefore uses OS-level
mechanisms rather than working-directory conventions:

- Linux: ``bubblewrap`` (``bwrap``) with only required system/runtime trees
  exposed read-only, private ``/tmp`` and ``/run``, and the authorized
  workspace bound read-only or read-write at its original absolute path.
- macOS: ``sandbox-exec`` with host reads denied outside the authorized
  workspace/runtime allowlist and workspace writes enabled only for writable
  posture.

If the OS confinement tool is unavailable or its posture-specific capability
probe fails, Pi dispatch is rejected before the child starts (fail closed).

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


_MACOS_RO_SUBPATHS = (
    "/System",
    "/usr/bin",
    "/usr/lib",
    "/usr/libexec",
    "/usr/share/zoneinfo",
    "/var/db/timezone/zoneinfo",
    "/bin",
    "/sbin",
)
_MACOS_RO_FILES = (
    "/dev/null",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
    "/etc/hosts",
    "/private/etc/hosts",
    "/etc/resolv.conf",
    "/private/etc/resolv.conf",
    "/var/run/resolv.conf",
    "/private/var/run/resolv.conf",
    "/etc/services",
    "/private/etc/services",
    "/etc/protocols",
    "/private/etc/protocols",
    "/etc/localtime",
    "/private/etc/localtime",
)


def _macos_node_runtime_root() -> Path | None:
    """Return a narrowly scoped non-system Node runtime tree, if required."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        executable = Path(node).resolve()
    except OSError:
        executable = Path(node)
    for system_root in (Path(item) for item in _MACOS_RO_SUBPATHS):
        if _path_within(executable, system_root):
            return None

    parts = executable.parts
    if "Cellar" in parts:
        index = parts.index("Cellar")
        # Homebrew: .../Cellar/node/<version>/bin/node. Expose only the Node
        # keg, not the rest of the Homebrew prefix.
        if len(parts) > index + 2:
            return Path(*parts[: index + 3])
    for marker in (".nvm", ".volta"):
        if marker in parts:
            index = parts.index(marker)
            # These managers keep runtime files below their own versioned tree.
            # The exact executable parent is sufficient for normal Node builds;
            # widen only to the manager-specific Node version root when known.
            if marker == ".nvm" and len(parts) > index + 4 and parts[index + 1:index + 3] == ("versions", "node"):
                return Path(*parts[: index + 4])
            if marker == ".volta" and "node" in parts[index + 1:]:
                node_index = parts.index("node", index + 1)
                if len(parts) > node_index + 1:
                    return Path(*parts[: node_index + 2])
    return executable.parent


def _macos_sandbox_profile(
    workspace: str,
    *,
    writable: bool,
    runtime_root: Path | None = None,
) -> str:
    """Return a sandbox-exec profile for the requested workspace posture."""
    escaped_workspace = _macos_quote(workspace)
    rules = ["(version 1)", "(allow default)", "(deny file-write*)", "(deny file-read*)"]

    # Every Pi posture treats allowed_scope.workspaces as a read boundary.
    # Permit only system code roots, concrete OS runtime files, the exact Pi/
    # Node runtime trees when installed outside system roots, and the workspace.
    # Broad configuration/data trees such as /Library or /private/etc are never
    # granted wholesale.
    for source in _MACOS_RO_SUBPATHS:
        rules.append(f'(allow file-read* (subpath "{_macos_quote(source)}"))')
    for source in _MACOS_RO_FILES:
        rules.append(f'(allow file-read* (literal "{_macos_quote(source)}"))')
    runtime_roots = [runtime_root, _macos_node_runtime_root()]
    seen_runtime_roots: set[str] = set()
    for root in runtime_roots:
        if root is None:
            continue
        root_value = str(root)
        if root_value in seen_runtime_roots:
            continue
        seen_runtime_roots.add(root_value)
        rules.append(f'(allow file-read* (subpath "{_macos_quote(root_value)}"))')
    rules.append(f'(allow file-read* (subpath "{escaped_workspace}"))')
    if writable:
        rules.append(f'(allow file-write* (subpath "{escaped_workspace}"))')
    return "".join(rules)


_LINUX_RO_PATHS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/nix")
_LINUX_ETC_RO_PATHS = (
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/host.conf",
    "/etc/nsswitch.conf",
    "/etc/gai.conf",
    "/etc/passwd",
    "/etc/group",
    "/etc/localtime",
    "/etc/timezone",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/protocols",
    "/etc/services",
)


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
    # Pi's package CLI is commonly installed below a node_modules tree. Expose
    # the package itself (including its nested dependencies), not the enclosing
    # package manager prefix. This must run before generic /opt handling so a
    # Homebrew Pi install does not expose all of /opt/homebrew.
    for parent in executable.parents:
        if parent.name != "node_modules":
            continue
        relative_parts = executable.relative_to(parent).parts
        if not relative_parts:
            break
        package_parts = relative_parts[:2] if relative_parts[0].startswith("@") else relative_parts[:1]
        return parent.joinpath(*package_parts)

    system_paths = _MACOS_RO_SUBPATHS if sys.platform == "darwin" else _LINUX_RO_PATHS
    for system_root in (Path(item) for item in system_paths):
        if _path_within(executable, system_root):
            return None
    if sys.platform.startswith("linux") and _path_within(executable, Path("/opt")):
        relative_parts = executable.relative_to("/opt").parts
        if relative_parts:
            return Path("/opt") / relative_parts[0]
    parent = executable.parent
    if _path_within(parent, workspace):
        return None
    return parent


def _wrap_argv_with_tool(
    argv: list[str],
    workspace: Path,
    tool: str,
    *,
    writable: bool = True,
) -> list[str]:
    """Build the platform confinement argv with an already-resolved tool."""
    workspace_path = Path(workspace).expanduser().resolve()
    workspace_resolved = str(workspace_path)
    runtime_root = _runtime_readonly_root(argv, workspace_path)
    if sys.platform.startswith("linux"):
        wrapped = [tool]
        for source in _LINUX_RO_PATHS:
            wrapped += ["--ro-bind-try", source, source]
        # Avoid exposing all of /etc, which may contain root-readable host
        # credentials when Hermes itself runs privileged. Bind only the public
        # runtime/network/TLS files Pi and its dynamic linker may need.
        wrapped += ["--dir", "/etc"]
        for source in _LINUX_ETC_RO_PATHS:
            wrapped += ["--ro-bind-try", source, source]
        # Do not expose host runtime sockets or host temporary files. Pi still
        # has normal writable scratch space, but it is private to the sandbox.
        # Keep /proc private and empty. A real procfs would expose the child's
        # own environment via /proc/self/environ, including the selected
        # provider credential. Pi does not require procfs for RPC operation.
        wrapped += ["--dir", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run"]
        if runtime_root is not None:
            runtime = str(runtime_root)
            wrapped += ["--ro-bind", runtime, runtime]
        workspace_bind = "--bind" if writable else "--ro-bind"
        wrapped += [
            workspace_bind, workspace_resolved, workspace_resolved,
            "--chdir", workspace_resolved,
            "--unshare-pid",
            "--cap-drop", "ALL",
            "--new-session",
            "--die-with-parent",
            *argv,
        ]
        return wrapped
    if sys.platform == "darwin":
        profile = _macos_sandbox_profile(
            workspace_resolved,
            writable=writable,
            runtime_root=runtime_root,
        )
        return [tool, "-p", profile, *argv]
    raise RuntimeError(f"confinement unsupported on platform {sys.platform!r}")


def _probe_confinement(tool: str, *, writable: bool = True) -> bool:
    """Prove the backend can enforce the requested workspace boundary.

    The probe is bounded and fail-closed. Both postures prove the workspace is
    readable while absolute, ``..``, and symlink reads cannot reach a sibling
    host path. Writable posture additionally proves an in-scope host write
    succeeds while an out-of-scope host write cannot occur; read-only posture
    proves the workspace cannot be mutated.
    """
    shell = "/bin/sh" if Path("/bin/sh").is_file() else shutil.which("sh")
    if not shell:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-gpt-confinement-probe-") as temp_dir:
            root = Path(temp_dir).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            inside = workspace / "inside.marker"
            outside = root / "outside.marker"
            inside.write_text("inside", encoding="utf-8")
            outside.write_text("outside", encoding="utf-8")

            escape_link = workspace / "escape-link"
            escape_link.symlink_to(outside)
            dotdot = workspace / ".." / outside.name
            if writable:
                probe_script = (
                    'cat "$1" >/dev/null 2>&1 || exit 71; '
                    'cat "$2" >/dev/null 2>&1 && exit 72; '
                    'cat "$3" >/dev/null 2>&1 && exit 73; '
                    'cat "$4" >/dev/null 2>&1 && exit 74; '
                    'printf ok > "$1" || exit 75; '
                    'printf escape > "$2" 2>/dev/null || true; '
                    'exit 0'
                )
            else:
                probe_script = (
                    'cat "$1" >/dev/null 2>&1 || exit 76; '
                    'cat "$2" >/dev/null 2>&1 && exit 77; '
                    'cat "$3" >/dev/null 2>&1 && exit 78; '
                    'cat "$4" >/dev/null 2>&1 && exit 79; '
                    'printf mutate > "$1" 2>/dev/null && exit 80; '
                    'exit 0'
                )
            argv = [
                shell,
                "-c",
                probe_script,
                "hermes-gpt-probe",
                str(inside),
                str(outside),
                str(dotdot),
                str(escape_link),
            ]

            wrapped = _wrap_argv_with_tool(argv, workspace, tool, writable=writable)
            completed = subprocess.run(
                wrapped,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            if completed.returncode != 0:
                return False
            if writable:
                return inside.read_text(encoding="utf-8") == "ok" and outside.read_text(encoding="utf-8") == "outside"
            return inside.read_text(encoding="utf-8") == "inside" and outside.read_text(encoding="utf-8") == "outside"
    except (OSError, subprocess.SubprocessError):
        return False


def confinement_available(*, writable: bool = True) -> bool:
    """Return True only when the opted-in confinement posture is usable.

    Binary presence alone is not sufficient. Availability requires a bounded
    capability probe for the exact read-only or writable posture requested, so
    restricted/containerized hosts fail closed.
    """
    if not confinement_enabled():
        return False
    tool = confinement_tool()
    return bool(tool and _probe_confinement(tool, writable=writable))


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

    A pre-existing hard link inside the workspace can reference the same inode
    as a file outside it. A mount sandbox cannot distinguish those aliases:
    reading the in-workspace name can disclose out-of-scope data, and writing it
    in writable posture can mutate the out-of-scope file. We therefore prove
    that every regular-file hard link is fully accounted for inside the
    workspace before any Pi child starts. Pre-existing symlinks that resolve
    outside the workspace and nested filesystems/mounts are also rejected so
    the authorized subtree cannot smuggle host data through an in-scope path.
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
                    try:
                        target = path.resolve(strict=False)
                    except OSError as exc:
                        raise PermissionError(f"unable to resolve workspace symlink: {path}") from exc
                    if not _path_within(target, root):
                        raise PermissionError(f"confined workspace contains an outward symlink: {path}")
                    continue
                if not (stat.S_ISDIR(entry_stat.st_mode) or stat.S_ISREG(entry_stat.st_mode)):
                    raise PermissionError(f"confined workspace contains a special filesystem entry: {path}")
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


def wrap_argv(argv: list[str], workspace: Path, *, writable: bool = True) -> list[str]:
    """Wrap ``argv`` so the child process is confined to ``workspace``.

    Linux exposes only required runtime trees read-only, provides private
    temporary/runtime filesystems, and binds the authorized workspace either
    read-write or read-only according to ``writable``. macOS applies the same
    workspace posture through ``sandbox-exec`` and, for read-only sessions,
    denies host reads outside the workspace/runtime allowlist. Raises
    ``RuntimeError`` when no confinement tool is installed; callers that need a
    trust-boundary decision must gate on :func:`confinement_available` for the
    same posture.
    """
    tool = confinement_tool()
    if tool is None:
        raise RuntimeError("no OS confinement tool available (install bubblewrap or sandbox-exec)")
    validated_workspace = validate_workspace_boundary(workspace)
    return _wrap_argv_with_tool(argv, validated_workspace, tool, writable=writable)
