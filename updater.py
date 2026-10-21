"""Check-first, non-destructive update support for Hermes GPT."""

from __future__ import annotations

import argparse
import email
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from packaging.version import InvalidVersion, Version

import operator_policy as op_policy


PACKAGE_NAME = "hermes-gpt"
DEFAULT_BRANCH = "master"
Runner = Callable[[list[str], Path | None, int], subprocess.CompletedProcess[str]]


def _run(argv: list[str], cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, shell=False, timeout=timeout)


def _safe_text(value: str) -> str:
    return op_policy.redact_output(value.strip())


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": _safe_text(message), **extra}


def _current_version(source_root: Path | None) -> str:
    if source_root:
        pyproject = source_root / "pyproject.toml"
        if pyproject.exists():
            match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
            if match:
                return match.group(1)
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _find_git_root(start: Path, runner: Runner) -> Path | None:
    try:
        result = runner(["git", "rev-parse", "--show-toplevel"], start, 10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return Path(result.stdout.strip()).resolve()
    except OSError:
        return None


def _git_output(argv: list[str], root: Path, runner: Runner, timeout: int = 30) -> tuple[str | None, dict[str, Any] | None]:
    try:
        result = runner(argv, root, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, _error("GIT_UNAVAILABLE", str(exc))
    if result.returncode != 0:
        return None, _error("GIT_COMMAND_FAILED", result.stderr or result.stdout or "Git command failed.")
    return result.stdout.strip(), None


def _source_update(*, root: Path, apply: bool, runner: Runner) -> dict[str, Any]:
    tracked_changes, error = _git_output(["git", "status", "--porcelain", "--untracked-files=no"], root, runner)
    if error:
        return error
    worktree_dirty = bool(tracked_changes)
    if worktree_dirty and apply:
        return _error(
            "WORKTREE_DIRTY",
            "Tracked changes are present, so no update was attempted.",
            suggested_action="Commit, stash, or discard the tracked changes before running hermes-gpt update --apply.",
        )

    branch, error = _git_output(["git", "branch", "--show-current"], root, runner)
    if error:
        return error
    default_ref, error = _git_output(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], root, runner)
    if error:
        default_ref = f"origin/{DEFAULT_BRANCH}"
    default_branch = default_ref.removeprefix("origin/") if default_ref else DEFAULT_BRANCH
    if branch != default_branch:
        return _error(
            "NOT_ON_DEFAULT_BRANCH",
            f"Updates are allowed only from the default branch {default_branch!r}; current branch is {branch!r}.",
            branch=branch,
            default_branch=default_branch,
            suggested_action=f"Switch to {default_branch!r}, then run hermes-gpt update again.",
        )

    current_sha, error = _git_output(["git", "rev-parse", "HEAD"], root, runner)
    if error:
        return error
    remote_head, error = _git_output(["git", "ls-remote", "--heads", "origin", default_branch], root, runner)
    if error:
        return error
    fields = (remote_head or "").split()
    if not fields:
        return _error("REMOTE_BRANCH_MISSING", f"origin/{default_branch} could not be found.")
    available_sha = fields[0]
    update_available = current_sha != available_sha
    result: dict[str, Any] = {
        "ok": True,
        "mode": "git",
        "current_version": _current_version(root),
        "branch": branch,
        "current_revision": current_sha,
        "available_revision": available_sha,
        "update_available": update_available,
        "applied": False,
        "tracked_changes_present": worktree_dirty,
        "untracked_files_ignored": True,
    }
    if not update_available:
        result["message"] = "Hermes GPT checkout is already up to date."
        return result
    if not apply:
        result["message"] = "An update is available. No changes were made; rerun with --apply to fast-forward."
        result["next_command"] = "hermes-gpt update --apply"
        if worktree_dirty:
            result["apply_blocked_by"] = "Tracked changes must be resolved before --apply can fast-forward the checkout."
        return result

    fetched, error = _git_output(["git", "fetch", "--prune", "origin", default_branch], root, runner, timeout=60)
    if error:
        return error
    _, error = _git_output(["git", "merge", "--ff-only", f"origin/{default_branch}"], root, runner, timeout=60)
    if error:
        return _error(
            "FAST_FORWARD_REQUIRED",
            "The checkout was not fast-forwardable, so it was not merged.",
            suggested_action="Resolve the branch divergence manually; hermes-gpt update never creates merge commits or rebases.",
        )
    new_sha, error = _git_output(["git", "rev-parse", "HEAD"], root, runner)
    if error:
        return error
    result.update({"applied": True, "current_revision": new_sha, "message": "Hermes GPT checkout was fast-forwarded safely."})
    return result


def _pip_latest_version(*, include_prereleases: bool, runner: Runner) -> tuple[str | None, dict[str, Any] | None]:
    command = [sys.executable, "-m", "pip", "index", "versions", PACKAGE_NAME, "--disable-pip-version-check"]
    if include_prereleases:
        command.append("--pre")
    try:
        result = runner(command, None, 45)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, _error("PIP_UNAVAILABLE", str(exc))
    if result.returncode != 0:
        return None, _error("UPDATE_CHECK_FAILED", result.stderr or result.stdout or "pip could not check for releases.")
    match = re.search(rf"^{re.escape(PACKAGE_NAME)}\s*\(([^)]+)\)", result.stdout, re.MULTILINE | re.IGNORECASE)
    if not match:
        return None, _error("UPDATE_CHECK_FAILED", "pip did not report a latest Hermes GPT version.")
    return match.group(1), None


def _pip_update(*, apply: bool, include_prereleases: bool, runner: Runner) -> dict[str, Any]:
    current = _current_version(None)
    latest, error = _pip_latest_version(include_prereleases=include_prereleases, runner=runner)
    if error:
        return error
    try:
        update_available = Version(latest) > Version(current)
    except InvalidVersion:
        return _error("INVALID_VERSION", f"Could not safely compare installed version {current!r} and available version {latest!r}.")
    result: dict[str, Any] = {
        "ok": True,
        "mode": "pip",
        "current_version": current,
        "available_version": latest,
        "update_available": update_available,
        "applied": False,
    }
    if not update_available:
        result["message"] = "Installed Hermes GPT is already up to date; no downgrade was attempted."
        return result
    if not apply:
        result["message"] = "An update is available. No packages were changed; rerun with --apply to install it."
        result["next_command"] = "hermes-gpt update --apply"
        return result
    command = [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", PACKAGE_NAME]
    if include_prereleases:
        command.append("--pre")
    try:
        completed = runner(command, None, 180)
    except (OSError, subprocess.SubprocessError) as exc:
        return _error("UPDATE_INSTALL_FAILED", str(exc))
    if completed.returncode != 0:
        return _error("UPDATE_INSTALL_FAILED", completed.stderr or completed.stdout or "pip failed to install the update.")
    result.update({"applied": True, "restart_required": True, "message": "Hermes GPT was updated. Restart any running Hermes GPT or Codex MCP process."})
    return result


def check_for_update(*, apply: bool = False, include_prereleases: bool = False, start: Path | None = None, runner: Runner = _run) -> dict[str, Any]:
    """Check for an update, or apply only a safe, explicit update path."""
    source_root = _find_git_root((start or Path(__file__).resolve().parent), runner)
    if source_root:
        return _source_update(root=source_root, apply=apply, runner=runner)
    return _pip_update(apply=apply, include_prereleases=include_prereleases, runner=runner)


# ---------------------------------------------------------------------------
# Package-runtime upgrade (`hermes-gpt update --apply --version X.Y.Z ...`).
#
# Every subprocess is a fixed argv with shell=False, every download is injected
# (`fetch`) and restricted to PyPI hosts, and nothing is written outside the
# private staging directory and the operator-supplied rollback directory. The
# updater never touches launchers, service definitions, environment files or
# credentials. Post-restart authenticated verification is left to the client.
# ---------------------------------------------------------------------------

UPGRADE_SCHEMA = "hermes-gpt.package-upgrade/1"
PYPI_JSON_URL = "https://pypi.org/pypi/hermes-gpt/{version}/json"
PYPI_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})
WHEEL_SUFFIX = "-py3-none-any.whl"
# Used with fullmatch; equivalent to the anchored pattern ^[A-Za-z0-9_.@-]{1,128}$.
SERVICE_NAME_RE = re.compile(r"[A-Za-z0-9_.@-]{1,128}")
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
IMPORT_SMOKE_SNIPPET = "import versioning, server; print(versioning.VERSION)"
PROBE_SNIPPET = """
import importlib.metadata as m, json
try:
    d = m.distribution('hermes-gpt')
except m.PackageNotFoundError:
    print(json.dumps({'installed': False}))
else:
    raw = d.read_text('direct_url.json')
    print(json.dumps({
        'installed': True,
        'version': d.version,
        'requires': d.metadata.get_all('Requires-Dist') or [],
        'direct_url': json.loads(raw) if raw else None,
    }))
"""

Fetch = Callable[[str], bytes]


def _https_fetch(url: str) -> bytes:
    """Default downloader. Callers pass only PyPI URLs; tests inject a fake."""
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("download exceeds the size limit")
    return data


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _current_uid() -> int:
    return os.getuid()


def _venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize_requirement(requirement: str) -> str:
    return " ".join(requirement.split())


def _same_version(left: str, right: str) -> bool:
    try:
        return Version(left) == Version(right)
    except InvalidVersion:
        return False


def _wheel_metadata(path: Path) -> tuple[str, str, set[str]]:
    """Read Name, Version and Requires-Dist from a wheel without extracting it."""
    try:
        with zipfile.ZipFile(path) as archive:
            candidates = [name for name in archive.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", name)]
            if len(candidates) != 1:
                raise _Refusal("WHEEL_INVALID", f"{path.name} must contain exactly one top-level METADATA file.")
            metadata = email.message_from_bytes(archive.read(candidates[0]))
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise _Refusal("WHEEL_INVALID", f"{path.name} could not be read as a wheel: {exc}") from exc
    requires = {_normalize_requirement(item) for item in metadata.get_all("Requires-Dist") or []}
    return str(metadata.get("Name", "")), str(metadata.get("Version", "")), requires


def _freeze_without_package(text: str) -> set[str]:
    lines: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.match(r"[A-Za-z0-9._-]+", line)
        if name and _canonical_name(name.group(0)) == PACKAGE_NAME:
            continue
        lines.add(line)
    return lines


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _tail(result: subprocess.CompletedProcess[str], default: str) -> str:
    return _safe_text((result.stderr or result.stdout or default).strip())[-400:]


class _UpgradeStop(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class _Refusal(_UpgradeStop):
    """Stop before the venv is modified."""


class _Failed(_UpgradeStop):
    """A failure after the venv was modified; triggers automatic rollback."""


@dataclass(frozen=True)
class _Artifact:
    path: Path
    filename: str
    sha256: str
    source: str
    url: str | None = None

    def describe(self) -> dict[str, Any]:
        described: dict[str, Any] = {"filename": self.filename, "sha256": self.sha256, "source": self.source}
        if self.url:
            described["url"] = self.url
        return described


class _PackageUpgrade:
    def __init__(
        self,
        *,
        target_version: str,
        venv: Path,
        rollback_dir: Path,
        service: str | None,
        wheel: Path | None,
        allow_dependency_changes: bool,
        allow_prerelease: bool,
        runner: Runner,
        fetch: Fetch,
        now: Callable[[], datetime],
    ) -> None:
        self.target_text = target_version
        self.venv = Path(venv)
        self.rollback_dir = Path(rollback_dir)
        self.service = service
        self.wheel = Path(wheel) if wheel is not None else None
        self.allow_dependency_changes = allow_dependency_changes
        self.allow_prerelease = allow_prerelease
        self.runner = runner
        self.fetch = fetch
        self.now = now
        self.started_at = now().isoformat()
        self.steps: list[dict[str, str]] = []
        self.staging = Path(".")
        self.python = Path(".")
        self.target: Version | None = None
        self.previous = ""
        self.installed_requires: set[str] = set()
        self.direct_url: Any = None
        self.target_requires: set[str] = set()
        self.target_artifact: _Artifact | None = None
        self.rollback_artifact: _Artifact | None = None
        self.rollback_wheel: Path | None = None
        self.restart_argv: list[str] | None = None
        self.no_deps = True
        self.dependency_changes: dict[str, list[str]] | None = None
        self.freeze_before = ""
        self.rollback_dir_ready = False
        self.owns_rollback_dir = False
        self.applied = False
        self.rolled_back = False

    # -- orchestration -----------------------------------------------------

    def run(self) -> dict[str, Any]:
        pre_phases = (
            ("validate", self._validate),
            ("inspect-installed", self._inspect),
            ("fetch-target", self._fetch_target),
            ("dependency-guard", self._guard_dependencies),
            ("fetch-rollback", self._fetch_rollback),
            ("snapshot", self._snapshot),
        )
        post_phases = (
            ("install", self._install),
            ("pip-check", self._pip_check),
            ("freeze-compare", self._freeze_compare),
            ("import-smoke", self._import_smoke),
            ("restart", self._restart),
        )
        with tempfile.TemporaryDirectory(prefix="hermes-gpt-update-") as staging:
            self.staging = Path(staging)
            for name, phase in pre_phases:
                try:
                    phase()
                except _Refusal as stop:
                    self._step(name, "refused", f"{stop.code}: {stop.message}")
                    return self._finish(stop)
            for name, phase in post_phases:
                try:
                    phase()
                except _Failed as stop:
                    self._step(name, "failed", f"{stop.code}: {stop.message}")
                    restored = self._rollback(stop)
                    return self._finish(stop, rollback_ok=restored)
        self.applied = True
        return self._finish(None)

    def _step(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append({"step": name, "status": status, "detail": _safe_text(detail)[:400]})

    def _call(self, argv: list[str], *, cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(argv, cwd, timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return subprocess.CompletedProcess(argv, 127, "", str(exc))

    # -- pre-change phases (refusals only) ---------------------------------

    def _validate(self) -> None:
        try:
            target = Version(self.target_text)
        except InvalidVersion as exc:
            raise _Refusal("INVALID_VERSION", f"{self.target_text!r} is not a valid package version.") from exc
        if str(target) != self.target_text or target.local is not None:
            raise _Refusal("INVALID_VERSION", f"Use a canonical release version such as 0.16.0, not {self.target_text!r}.")
        if target.is_prerelease and not self.allow_prerelease:
            raise _Refusal("PRERELEASE_REFUSED", f"{self.target_text!r} is a prerelease. Pass --allow-prerelease to install it explicitly.")
        self.target = target
        if not self.venv.is_absolute():
            raise _Refusal("VENV_NOT_ABSOLUTE", "--venv must be an absolute path.")
        self.python = _venv_python(self.venv)
        if not self.venv.is_dir() or not self.python.is_file():
            raise _Refusal("VENV_INVALID", f"{self.venv} is not a virtual environment containing {self.python.relative_to(self.venv)}.")
        if not self.rollback_dir.is_absolute():
            raise _Refusal("ROLLBACK_DIR_NOT_ABSOLUTE", "--rollback-dir must be an absolute path.")
        if self.rollback_dir.exists():
            if not self.rollback_dir.is_dir() or any(self.rollback_dir.iterdir()):
                raise _Refusal(
                    "ROLLBACK_DIR_NOT_EMPTY",
                    "The rollback directory exists and is not empty. Choose a new path; existing rollback artifacts are never overwritten.",
                )
        elif not self.rollback_dir.parent.is_dir():
            raise _Refusal("ROLLBACK_DIR_PARENT_MISSING", "The parent of --rollback-dir must already exist.")
        if self.wheel is not None and not self.wheel.is_absolute():
            raise _Refusal("WHEEL_NOT_ABSOLUTE", "--wheel must be an absolute path.")
        if self.service is not None:
            if not SERVICE_NAME_RE.fullmatch(self.service):
                raise _Refusal("SERVICE_NAME_INVALID", "Service names must be 1 to 128 characters from A-Z, a-z, 0-9, '_', '.', '@' and '-'.")
            self.restart_argv = self._restart_argv()
        self._step("validate", "ok", "Target version, venv, rollback directory, wheel and service name are acceptable.")

    def _restart_argv(self) -> list[str]:
        assert self.service is not None
        if sys.platform == "darwin":
            return ["launchctl", "kickstart", "-k", f"gui/{_current_uid()}/{self.service}"]
        if sys.platform.startswith("linux"):
            return ["systemctl", "--user", "restart", self.service]
        raise _Refusal("UNSUPPORTED_PLATFORM", "--service restarts are supported only on macOS (launchctl) and Linux (systemctl --user).")

    def _inspect(self) -> None:
        assert self.target is not None
        result = self._call([str(self.python), "-I", "-c", PROBE_SNIPPET], timeout=60)
        if result.returncode != 0:
            raise _Refusal("PROBE_FAILED", _tail(result, "Could not read hermes-gpt metadata from the venv."))
        try:
            probe = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise _Refusal("PROBE_FAILED", "The venv metadata probe did not return JSON.") from exc
        if not isinstance(probe, dict) or not probe.get("installed"):
            raise _Refusal("NOT_INSTALLED", "hermes-gpt is not installed in this venv. --apply upgrades an existing package installation only.")
        raw_version = str(probe.get("version", ""))
        try:
            current = Version(raw_version)
        except InvalidVersion as exc:
            raise _Refusal("INVALID_VERSION", f"Installed version {raw_version!r} is not a valid package version.") from exc
        if current == self.target:
            raise _Refusal("ALREADY_AT_TARGET", f"hermes-gpt {raw_version} is already installed in this venv.")
        if current > self.target:
            raise _Refusal("DOWNGRADE_REFUSED", f"Installed hermes-gpt {raw_version} is newer than {self.target_text}; --apply never downgrades.")
        self.previous = raw_version
        self.installed_requires = {_normalize_requirement(item) for item in probe.get("requires") or []}
        self.direct_url = probe.get("direct_url")
        self._step("inspect-installed", "ok", f"Installed hermes-gpt {raw_version}.")

    def _fetch_target(self) -> None:
        if self.wheel is not None:
            artifact = self._stage_file(self.wheel, "target", "operator-supplied")
        else:
            artifact = self._fetch_pypi_wheel(self.target_text, "target")
        self.target_requires = self._check_wheel(artifact, self.target_text)
        self.target_artifact = artifact
        self._step("fetch-target", "ok", f"{artifact.filename} sha256 {artifact.sha256} ({artifact.source}).")

    def _guard_dependencies(self) -> None:
        added = sorted(self.target_requires - self.installed_requires)
        removed = sorted(self.installed_requires - self.target_requires)
        if not added and not removed:
            self.no_deps = True
            self._step("dependency-guard", "ok", "Requires-Dist is identical; the install will use --no-deps.")
            return
        self.dependency_changes = {"added": added, "removed": removed}
        if not self.allow_dependency_changes:
            raise _Refusal(
                "DEPENDENCIES_CHANGED",
                "The target release changes hermes-gpt's dependencies. Upgrade manually, or pass --allow-dependency-changes after reviewing the change.",
                dependency_changes=self.dependency_changes,
            )
        self.no_deps = False
        self._step("dependency-guard", "ok", "Dependencies changed and --allow-dependency-changes was given; installing with dependency resolution.")

    def _fetch_rollback(self) -> None:
        local, expected = self._rollback_source()
        if local is not None:
            artifact = self._stage_file(local, "rollback", "local-file")
        else:
            try:
                artifact = self._fetch_pypi_wheel(self.previous, "rollback")
            except _Refusal as exc:
                raise _Refusal(
                    "ROLLBACK_ARTIFACT_UNAVAILABLE",
                    f"No verified rollback wheel for hermes-gpt {self.previous} is available: {exc.message}",
                ) from exc
            if expected is not None and artifact.sha256 != expected:
                raise _Refusal(
                    "ROLLBACK_ARTIFACT_UNAVAILABLE",
                    f"PyPI's hermes-gpt {self.previous} wheel does not match the hash recorded for the installed package.",
                )
        self._check_wheel(artifact, self.previous)
        self.rollback_artifact = artifact
        self._step("fetch-rollback", "ok", f"{artifact.filename} sha256 {artifact.sha256} ({artifact.source}).")

    def _rollback_source(self) -> tuple[Path | None, str | None]:
        """Return a local wheel matching the installed artifact, or the digest a PyPI copy must match."""
        if self.direct_url is None:
            return None, None
        unavailable = (
            "hermes-gpt was installed from a directory, VCS or unhashed source, so no exact rollback wheel can be prepared. "
            "Upgrade manually."
        )
        if not isinstance(self.direct_url, dict):
            raise _Refusal("ROLLBACK_ARTIFACT_UNAVAILABLE", unavailable)
        url = self.direct_url.get("url")
        archive = self.direct_url.get("archive_info")
        recorded = archive.get("hash") if isinstance(archive, dict) else None
        if (
            not isinstance(url, str)
            or "dir_info" in self.direct_url
            or "vcs_info" in self.direct_url
            or not isinstance(recorded, str)
        ):
            raise _Refusal("ROLLBACK_ARTIFACT_UNAVAILABLE", unavailable)
        parsed = urlparse(url)
        algorithm, _, digest = recorded.partition("=")
        if parsed.scheme != "file" or algorithm != "sha256" or not digest:
            raise _Refusal("ROLLBACK_ARTIFACT_UNAVAILABLE", unavailable)
        path = Path(url2pathname(unquote(parsed.path)))
        if path.name.endswith(WHEEL_SUFFIX) and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            return path, digest
        return None, digest

    def _snapshot(self) -> None:
        assert self.rollback_artifact is not None
        result = self._call([str(self.python), "-m", "pip", "freeze"], timeout=120)
        if result.returncode != 0:
            raise _Refusal("SNAPSHOT_FAILED", _tail(result, "pip freeze failed; nothing was installed."))
        self.freeze_before = result.stdout
        self.owns_rollback_dir = not self.rollback_dir.exists()
        try:
            self.rollback_dir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(self.rollback_dir, 0o700)
            _write_private(self.rollback_dir / "freeze-before.txt", self.freeze_before)
            self.rollback_wheel = self.rollback_dir / self.rollback_artifact.filename
            shutil.copyfile(self.rollback_artifact.path, self.rollback_wheel)
        except OSError as exc:
            if self.owns_rollback_dir:
                shutil.rmtree(self.rollback_dir, ignore_errors=True)
            raise _Refusal("SNAPSHOT_FAILED", f"Could not write the rollback snapshot: {exc}") from exc
        self.rollback_dir_ready = True
        self._step("snapshot", "ok", f"Recorded freeze-before.txt and copied the rollback wheel into {self.rollback_dir}.")

    # -- post-change phases (failures roll back) ---------------------------

    def _install(self) -> None:
        assert self.target_artifact is not None
        argv = [str(self.python), "-m", "pip", "install"]
        if self.no_deps:
            argv.append("--no-deps")
        argv += ["--no-cache-dir", str(self.target_artifact.path)]
        result = self._call(argv, timeout=900)
        if result.returncode != 0:
            raise _Failed("INSTALL_FAILED", _tail(result, "pip failed to install the target wheel."))
        self._step("install", "ok", "Installed the target wheel" + (" with --no-deps." if self.no_deps else " with dependency resolution."))

    def _pip_check(self) -> None:
        result = self._call([str(self.python), "-m", "pip", "check"], timeout=120)
        if result.returncode != 0:
            raise _Failed("PIP_CHECK_FAILED", _tail(result, "pip check reported broken requirements."))
        self._step("pip-check", "ok", "pip check passed.")

    def _freeze_compare(self) -> None:
        if not self.no_deps:
            self._step("freeze-compare", "skipped", "Dependency changes were explicitly allowed, so the freeze comparison does not apply.")
            return
        result = self._call([str(self.python), "-m", "pip", "freeze"], timeout=120)
        if result.returncode != 0:
            raise _Failed("FREEZE_FAILED", _tail(result, "pip freeze failed after the install."))
        if _freeze_without_package(result.stdout) != _freeze_without_package(self.freeze_before):
            raise _Failed("FREEZE_CHANGED", "Packages other than hermes-gpt changed during a --no-deps install.")
        self._step("freeze-compare", "ok", "Every package other than hermes-gpt is unchanged.")

    def _import_smoke(self) -> None:
        assert self.target is not None
        seen = self._import_check(self.target_text)
        self._step("import-smoke", "ok", f"A neutral-directory import reports {seen}.")

    def _restart(self, step: str = "restart") -> None:
        if self.restart_argv is None:
            self._step(step, "skipped", "No service was named. Restart the Hermes GPT process that uses this venv.")
            return
        result = self._call(self.restart_argv, timeout=120)
        if result.returncode != 0:
            raise _Failed("RESTART_FAILED", _tail(result, "The service manager rejected the restart."))
        self._step(step, "ok", f"Ran {' '.join(self.restart_argv)}.")

    def _import_check(self, expected: str) -> str:
        # -I ignores PYTHON* variables, the working directory and user site; the staging
        # directory is private and contains no repository files.
        result = self._call([str(self.python), "-I", "-c", IMPORT_SMOKE_SNIPPET], cwd=self.staging, timeout=120)
        if result.returncode != 0:
            raise _Failed("IMPORT_FAILED", _tail(result, "The neutral-directory import failed."))
        lines = result.stdout.strip().splitlines()
        seen = lines[-1].strip() if lines else ""
        if seen != expected:
            raise _Failed("IMPORT_VERSION_MISMATCH", f"Expected a neutral-directory import of {expected!r}, got {seen!r}.")
        return seen

    def _rollback(self, stop: _UpgradeStop) -> bool:
        assert self.rollback_wheel is not None
        self._step("rollback", "started", f"Reinstalling hermes-gpt {self.previous} after {stop.code}.")
        argv = [str(self.python), "-m", "pip", "install", "--no-deps", "--no-cache-dir", "--force-reinstall", str(self.rollback_wheel)]
        try:
            result = self._call(argv, timeout=900)
            if result.returncode != 0:
                raise _Failed("ROLLBACK_INSTALL_FAILED", _tail(result, "pip could not reinstall the rollback wheel."))
            seen = self._import_check(self.previous)
        except _Failed as exc:
            self._step("rollback", "failed", f"{exc.code}: {exc.message}")
            return False
        self.rolled_back = True
        self._step("rollback", "ok", f"Reinstalled the rollback wheel; import reports {seen}.")
        if self.restart_argv is not None:
            try:
                self._restart(step="rollback-restart")
            except _Failed as exc:
                self._step("rollback-restart", "failed", f"{exc.code}: {exc.message}")
        return True

    # -- artifacts ---------------------------------------------------------

    def _download(self, url: str, code: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in PYPI_HOSTS:
            raise _Refusal(code, "Refusing to download from a host outside PyPI.")
        try:
            data = self.fetch(url)
        except (OSError, ValueError) as exc:
            raise _Refusal(code, f"Could not download {url}: {exc}") from exc
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise _Refusal(code, "The download exceeds the size limit.")
        return data

    def _fetch_pypi_wheel(self, version: str, role: str) -> _Artifact:
        raw_document = self._download(PYPI_JSON_URL.format(version=version), "PYPI_UNAVAILABLE")
        try:
            document = json.loads(raw_document)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _Refusal("PYPI_UNAVAILABLE", "PyPI returned an unreadable release document.") from exc
        if not isinstance(document, dict):
            raise _Refusal("PYPI_UNAVAILABLE", "PyPI returned an unreadable release document.")
        files = [
            item
            for item in document.get("urls") or []
            if isinstance(item, dict) and item.get("packagetype") == "bdist_wheel" and str(item.get("filename", "")).endswith(WHEEL_SUFFIX)
        ]
        if len(files) != 1:
            raise _Refusal("WHEEL_UNAVAILABLE", f"PyPI does not list exactly one py3-none-any wheel for hermes-gpt {version}.")
        entry = files[0]
        digests = entry.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        url = entry.get("url")
        if not isinstance(digest, str) or not isinstance(url, str):
            raise _Refusal("WHEEL_UNAVAILABLE", "PyPI release metadata has no usable sha256 digest or download URL.")
        data = self._download(url, "PYPI_UNAVAILABLE")
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest.lower():
            raise _Refusal(
                "HASH_MISMATCH",
                f"The downloaded {entry.get('filename')} does not match PyPI's sha256 digest; nothing was installed.",
                expected_sha256=digest,
                actual_sha256=actual,
            )
        return self._stage_bytes(str(entry["filename"]), data, role, "pypi", url)

    def _stage_file(self, source: Path, role: str, origin: str) -> _Artifact:
        if not source.is_file() or not source.name.endswith(WHEEL_SUFFIX):
            raise _Refusal("WHEEL_INVALID", f"{source} is not a readable {WHEEL_SUFFIX} file.")
        return self._stage_bytes(source.name, source.read_bytes(), role, origin, None)

    def _stage_bytes(self, filename: str, data: bytes, role: str, source: str, url: str | None) -> _Artifact:
        if Path(filename).name != filename or not filename.endswith(WHEEL_SUFFIX):
            raise _Refusal("WHEEL_INVALID", f"Refusing unexpected artifact name {filename!r}.")
        directory = self.staging / role
        directory.mkdir(exist_ok=True)
        path = directory / filename
        path.write_bytes(data)
        return _Artifact(path=path, filename=filename, sha256=hashlib.sha256(data).hexdigest(), source=source, url=url)

    def _check_wheel(self, artifact: _Artifact, expected_version: str) -> set[str]:
        name, version, requires = _wheel_metadata(artifact.path)
        if _canonical_name(name) != PACKAGE_NAME or not _same_version(version, expected_version):
            raise _Refusal("WHEEL_MISMATCH", f"{artifact.filename} is not hermes-gpt {expected_version}.")
        return requires

    # -- result and receipt ------------------------------------------------

    def _finish(self, stop: _UpgradeStop | None, *, rollback_ok: bool = True) -> dict[str, Any]:
        if stop is None:
            result: dict[str, Any] = {
                "ok": True,
                "mode": "package-apply",
                "previous_version": self.previous,
                "target_version": self.target_text,
                "applied": True,
                "rolled_back": False,
                "restart_required": self.restart_argv is None,
                "message": f"hermes-gpt was upgraded from {self.previous} to {self.target_text} and verified by a neutral-directory import.",
                "next_step": (
                    "Verify serverInfo.version with your MCP client after the restart; the updater does not authenticate to the service."
                    if self.restart_argv
                    else "Restart the Hermes GPT process that uses this venv, then verify serverInfo.version with your MCP client."
                ),
            }
        else:
            result = {
                "ok": False,
                "code": stop.code,
                "message": _safe_text(stop.message),
                "mode": "package-apply",
                "previous_version": self.previous,
                "target_version": self.target_text,
                "applied": False,
                "rolled_back": self.rolled_back,
                **stop.extra,
            }
            if isinstance(stop, _Failed):
                result["rollback_attempted"] = True
                if not rollback_ok:
                    result["rollback_failed"] = True
                    result["next_step"] = "Restore the previous package manually with the commands in ROLLBACK.md."
        result["steps"] = self.steps
        if self.restart_argv is not None:
            result["restart_command"] = self.restart_argv
        if self.rollback_dir_ready:
            result["rollback_dir"] = str(self.rollback_dir)
            try:
                receipt = self._write_receipt(result)
            except OSError as exc:
                result["receipt_error"] = _safe_text(str(exc))
            else:
                result["receipt"] = str(receipt)
                result["rollback_guide"] = str(self.rollback_dir / "ROLLBACK.md")
        return result

    def _write_receipt(self, result: dict[str, Any]) -> Path:
        def describe(artifact: _Artifact | None) -> dict[str, Any] | None:
            return artifact.describe() if artifact is not None else None

        # Deliberately excludes the process environment, environment-file contents and any credentials.
        receipt = {
            "schema": UPGRADE_SCHEMA,
            "ok": result["ok"],
            "code": result.get("code"),
            "applied": result["applied"],
            "rolled_back": result["rolled_back"],
            "previous_version": self.previous,
            "target_version": self.target_text,
            "platform": sys.platform,
            "venv": str(self.venv),
            "service": self.service,
            "restart_command": self.restart_argv,
            "dependency_mode": "no-deps" if self.no_deps else "dependencies-changed-allowed",
            "dependency_changes": self.dependency_changes,
            "wheels": {"target": describe(self.target_artifact), "rollback": describe(self.rollback_artifact)},
            "freeze_before": "freeze-before.txt",
            "steps": self.steps,
            "started_at": self.started_at,
            "finished_at": self.now().isoformat(),
        }
        path = self.rollback_dir / "RECEIPT.json"
        _write_private(path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        _write_private(self.rollback_dir / "ROLLBACK.md", self._rollback_markdown())
        return path

    def _rollback_markdown(self) -> str:
        python = str(self.python)
        freeze_path = self.rollback_dir / "freeze-before.txt"
        lines = [
            f"# Roll back hermes-gpt {self.target_text} to {self.previous}",
            "",
            "Written by `hermes-gpt update --apply`. These commands restore only the Python package. They do not change launchers, launchd plists, systemd units, environment files or credentials.",
            "",
            "## 1. Reinstall the previous package",
            "",
            "```sh",
            f'"{python}" -m pip install --no-deps --no-cache-dir --force-reinstall "{self.rollback_wheel}"',
            "```",
            "",
            "## 2. Verify the restored import",
            "",
            "```sh",
            f'cd "{self.rollback_dir}" && "{python}" -I -c "import versioning, server; print(versioning.VERSION)"',
            "```",
            "",
            f"The command should print `{self.previous}`.",
            "",
            "## 3. Restart",
            "",
        ]
        if self.restart_argv is not None:
            lines += ["```sh", " ".join(self.restart_argv), "```"]
        else:
            lines.append("Restart the Hermes GPT process that uses this venv.")
        lines += [
            "",
            "## 4. Verify",
            "",
            "Verify `serverInfo.version` with your MCP client after restarting, then refresh the client's cached tool list.",
        ]
        if not self.no_deps:
            lines += [
                "",
                "## Dependency changes",
                "",
                "This upgrade changed dependencies. Restore the recorded set with:",
                "",
                "```sh",
                f'"{python}" -m pip install -r "{freeze_path}"',
                "```",
            ]
        return "\n".join(lines) + "\n"


def apply_package_upgrade(
    *,
    target_version: str,
    venv: Path,
    service: str | None = None,
    rollback_dir: Path,
    wheel: Path | None = None,
    allow_dependency_changes: bool = False,
    allow_prerelease: bool = False,
    runner: Runner = _run,
    fetch: Fetch = _https_fetch,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Upgrade a package-installed hermes-gpt in an existing venv, with automatic rollback.

    Refuses before any change when validation, version, hash or dependency checks fail.
    Failures after the install roll the venv back to the snapshotted wheel. Never
    restarts anything other than the named service.
    """
    upgrade = _PackageUpgrade(
        target_version=target_version,
        venv=venv,
        rollback_dir=rollback_dir,
        service=service,
        wheel=wheel,
        allow_dependency_changes=allow_dependency_changes,
        allow_prerelease=allow_prerelease,
        runner=runner,
        fetch=fetch,
        now=now,
    )
    return upgrade.run()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="hermes-gpt update", description="Check for a Hermes GPT update; use --apply to perform a safe update.")
    parser.add_argument("--apply", action="store_true", help="Apply the available update. The default is check-only.")
    parser.add_argument("--pre", action="store_true", help="Include prerelease package versions for installed-package updates.")
    parser.add_argument("--version", help="With --apply: upgrade a package-installed venv to this exact release version (X.Y.Z).")
    parser.add_argument("--venv", help="With --apply --version: absolute path to the service's virtual environment.")
    parser.add_argument("--rollback-dir", help="With --apply --version: absolute path of a new or empty directory for the rollback snapshot.")
    parser.add_argument("--service", help="With --apply --version: the one service to restart (launchd label on macOS, systemd --user unit on Linux).")
    parser.add_argument("--wheel", help="With --apply --version: install this local py3-none-any wheel instead of downloading it from PyPI.")
    parser.add_argument("--allow-dependency-changes", action="store_true", help="Allow an upgrade whose Requires-Dist differs from the installed package.")
    parser.add_argument("--allow-prerelease", action="store_true", help="Allow a prerelease --version.")
    args = parser.parse_args(argv)

    package_only = {
        "--venv": args.venv,
        "--rollback-dir": args.rollback_dir,
        "--service": args.service,
        "--wheel": args.wheel,
    }
    if args.version is None:
        if any(value is not None for value in package_only.values()) or args.allow_dependency_changes or args.allow_prerelease:
            parser.error("--venv, --rollback-dir, --service, --wheel and --allow-* require --apply --version X.Y.Z")
        print(json.dumps(check_for_update(apply=args.apply, include_prereleases=args.pre), indent=2, default=str))
        return

    if not args.apply:
        parser.error("--version requires --apply")
    if args.pre:
        parser.error("--pre applies only to the installed-package check, not to --version")
    if args.venv is None or args.rollback_dir is None:
        parser.error("--apply --version requires --venv and --rollback-dir")
    result = apply_package_upgrade(
        target_version=args.version,
        venv=Path(args.venv),
        rollback_dir=Path(args.rollback_dir),
        service=args.service,
        wheel=Path(args.wheel) if args.wheel is not None else None,
        allow_dependency_changes=args.allow_dependency_changes,
        allow_prerelease=args.allow_prerelease,
    )
    print(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise SystemExit(1)
