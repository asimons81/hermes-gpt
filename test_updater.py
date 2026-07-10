import json
import subprocess

import updater


def completed(argv, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class GitRunner:
    def __init__(self, *, dirty: str = "", updated: bool = False):
        self.dirty = dirty
        self.updated = updated
        self.calls: list[list[str]] = []
        self.rev_calls = 0

    def __call__(self, argv, cwd, timeout):
        self.calls.append(argv)
        if argv[:3] == ["git", "status", "--porcelain"]:
            return completed(argv, self.dirty)
        if argv == ["git", "branch", "--show-current"]:
            return completed(argv, "master\n")
        if argv[:3] == ["git", "symbolic-ref", "--quiet"]:
            return completed(argv, "origin/master\n")
        if argv == ["git", "rev-parse", "HEAD"]:
            self.rev_calls += 1
            return completed(argv, ("b" * 40 if self.updated and self.rev_calls > 1 else "a" * 40) + "\n")
        if argv[:3] == ["git", "ls-remote", "--heads"]:
            return completed(argv, "b" * 40 + "\trefs/heads/master\n")
        if argv[:3] == ["git", "fetch", "--prune"]:
            return completed(argv)
        if argv[:3] == ["git", "merge", "--ff-only"]:
            return completed(argv)
        raise AssertionError(f"Unexpected command: {argv}")


def test_git_update_checks_only_by_default(tmp_path):
    runner = GitRunner()
    result = updater._source_update(root=tmp_path, apply=False, runner=runner)
    assert result["ok"] is True
    assert result["update_available"] is True
    assert result["applied"] is False
    assert result["next_command"] == "hermes-gpt update --apply"
    assert not any(call[1] in {"fetch", "merge"} for call in runner.calls)


def test_git_update_refuses_tracked_changes(tmp_path):
    runner = GitRunner(dirty=" M server.py\n")
    result = updater._source_update(root=tmp_path, apply=True, runner=runner)
    assert result["ok"] is False
    assert result["code"] == "WORKTREE_DIRTY"
    assert len(runner.calls) == 1


def test_git_check_still_reports_an_update_when_tracked_changes_exist(tmp_path):
    runner = GitRunner(dirty=" M server.py\n")
    result = updater._source_update(root=tmp_path, apply=False, runner=runner)
    assert result["ok"] is True
    assert result["update_available"] is True
    assert result["tracked_changes_present"] is True
    assert "apply_blocked_by" in result


def test_git_update_applies_only_a_fast_forward(tmp_path):
    runner = GitRunner(updated=True)
    result = updater._source_update(root=tmp_path, apply=True, runner=runner)
    assert result["ok"] is True
    assert result["applied"] is True
    assert result["current_revision"] == "b" * 40
    assert ["git", "fetch", "--prune", "origin", "master"] in runner.calls
    assert ["git", "merge", "--ff-only", "origin/master"] in runner.calls


def test_pip_update_checks_only_until_apply(monkeypatch):
    calls = []

    def runner(argv, cwd, timeout):
        calls.append(argv)
        if argv[2:4] == ["pip", "index"]:
            return completed(argv, "hermes-gpt (0.5.0)\nAvailable versions: 0.5.0, 0.4.0\n")
        if argv[2:4] == ["pip", "install"]:
            return completed(argv, "Successfully installed hermes-gpt-0.5.0\n")
        raise AssertionError(argv)

    monkeypatch.setattr(updater, "_current_version", lambda source_root: "0.4.0")
    checked = updater._pip_update(apply=False, include_prereleases=False, runner=runner)
    assert checked["ok"] is True
    assert checked["update_available"] is True
    assert checked["applied"] is False
    assert not any(command[3] == "install" for command in calls)

    applied = updater._pip_update(apply=True, include_prereleases=False, runner=runner)
    assert applied["ok"] is True
    assert applied["applied"] is True
    assert applied["restart_required"] is True
    assert any(command[3] == "install" for command in calls)


def test_pip_updater_never_downgrades(monkeypatch):
    def runner(argv, cwd, timeout):
        assert argv[2:4] == ["pip", "index"]
        return completed(argv, "hermes-gpt (0.4.0)\nAvailable versions: 0.4.0\n")

    monkeypatch.setattr(updater, "_current_version", lambda source_root: "0.5.0b2")
    result = updater._pip_update(apply=True, include_prereleases=False, runner=runner)
    assert result["ok"] is True
    assert result["update_available"] is False
    assert result["applied"] is False


def test_update_cli_emits_check_result(monkeypatch, capsys):
    monkeypatch.setattr(updater, "check_for_update", lambda **kwargs: {"ok": True, **kwargs})
    updater.main(["--pre"])
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": True, "apply": False, "include_prereleases": True}
