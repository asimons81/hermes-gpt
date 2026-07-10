"""Check-first, non-destructive update support for Hermes GPT."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

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


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="hermes-gpt update", description="Check for a Hermes GPT update; use --apply to perform a safe update.")
    parser.add_argument("--apply", action="store_true", help="Apply the available update. The default is check-only.")
    parser.add_argument("--pre", action="store_true", help="Include prerelease package versions for installed-package updates.")
    args = parser.parse_args(argv)
    print(json.dumps(check_for_update(apply=args.apply, include_prereleases=args.pre), indent=2, default=str))
