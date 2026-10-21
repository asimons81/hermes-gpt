import hashlib
import json
import os
import stat
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# Package-runtime upgrade (--apply --version). Everything is injected: a fake
# venv runner, an in-memory PyPI, and small wheels built under tmp_path.
# ---------------------------------------------------------------------------

def FIXED_NOW():
    return datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def build_wheel(directory: Path, version: str, requires=("packaging>=23",)) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"hermes_gpt-{version}-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.1\nName: hermes-gpt\nVersion: {version}\n" + "".join(
        f"Requires-Dist: {item}\n" for item in requires
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"hermes_gpt-{version}.dist-info/METADATA", metadata)
        archive.writestr("versioning.py", f"VERSION = {version!r}\n")
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeVenv:
    """Simulates one venv's pip state behind the injected runner."""

    def __init__(self, root: Path, *, version: str = "0.15.0", requires=("packaging>=23",), direct_url=None):
        self.root = root
        self.python = updater._venv_python(root)  # bin/python, or Scripts/python.exe on Windows
        self.python.parent.mkdir(parents=True)
        self.python.write_text("#!/bin/sh\n", encoding="utf-8")
        self.version = version
        self.requires = list(requires)
        self.direct_url = direct_url
        self.fail: set[str] = set()
        self.import_override: str | None = None
        self.calls: list[tuple[list[str], Path | None]] = []

    def argv_list(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]

    def __call__(self, argv, cwd, timeout):
        self.calls.append((list(argv), cwd))
        if argv[0] in {"launchctl", "systemctl"}:
            return completed(argv, returncode=1 if "restart" in self.fail else 0)
        assert argv[0] == str(self.python), argv
        if argv[1:3] == ["-I", "-c"] and argv[3] == updater.PROBE_SNIPPET:
            payload = {"installed": True, "version": self.version, "requires": self.requires, "direct_url": self.direct_url}
            return completed(argv, json.dumps(payload))
        if argv[1:3] == ["-I", "-c"] and argv[3] == updater.IMPORT_SMOKE_SNIPPET:
            if "import" in self.fail:
                return completed(argv, "", "ImportError: boom", 1)
            return completed(argv, f"{self.import_override or self.version}\n")
        if argv[1:3] == ["-m", "pip"]:
            subcommand = argv[3]
            if subcommand == "freeze":
                return completed(argv, f"mcp==1.0.0\npackaging==26.3\nhermes-gpt=={self.version}\n")
            if subcommand == "check":
                return completed(argv, "", returncode=1 if "check" in self.fail else 0)
            if subcommand == "install":
                if "--force-reinstall" in argv:
                    if "rollback" in self.fail:
                        return completed(argv, "", "rollback exploded", 1)
                elif "install" in self.fail:
                    return completed(argv, "", "pip exploded", 1)
                wheel = Path(argv[-1])
                self.version = wheel.name.split("-")[1]
                self.requires = sorted(updater._wheel_metadata(wheel)[2])
                return completed(argv, "Successfully installed\n")
        raise AssertionError(f"unexpected command: {argv}")


class FakePyPI:
    def __init__(self) -> None:
        self.responses: dict[str, bytes] = {}
        self.calls: list[str] = []

    def publish(self, version: str, wheel: Path, *, digest: str | None = None) -> None:
        data = wheel.read_bytes()
        file_url = f"https://files.pythonhosted.org/packages/{wheel.name}"
        self.responses[file_url] = data
        document = {
            "info": {"version": version},
            "urls": [
                {
                    "filename": wheel.name,
                    "packagetype": "bdist_wheel",
                    "url": file_url,
                    "digests": {"sha256": digest or hashlib.sha256(data).hexdigest()},
                }
            ],
        }
        self.responses[updater.PYPI_JSON_URL.format(version=version)] = json.dumps(document).encode()

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.responses:
            return self.responses[url]
        raise OSError(f"404 for {url}")


def build_env(tmp_path, *, current="0.15.0", target="0.16.0", target_requires=("packaging>=23",), digest=None, venv_version=None, publish_current=True):
    wheels = tmp_path / "wheels"
    venv = FakeVenv(tmp_path / "venv", version=venv_version or current)
    pypi = FakePyPI()
    if publish_current:
        pypi.publish(current, build_wheel(wheels / "current", current))
    pypi.publish(target, build_wheel(wheels / "target", target, target_requires), digest=digest)
    return venv, pypi


def upgrade(venv, pypi, rollback, *, target="0.16.0", **kwargs):
    return updater.apply_package_upgrade(
        target_version=target,
        venv=venv.root,
        rollback_dir=rollback,
        runner=venv,
        fetch=pypi,
        now=FIXED_NOW,
        **kwargs,
    )


def normalized(calls):
    return [[("<wheel>" if arg.endswith(".whl") else arg) for arg in argv] for argv in calls]


def assert_refused(result, code, venv):
    assert result["ok"] is False, result
    assert result["code"] == code, result
    assert result["applied"] is False
    assert result["rolled_back"] is False
    assert not any(argv[2:4] == ["pip", "install"] for argv in venv.argv_list()), venv.argv_list()


def test_apply_happy_path_runs_exact_sequence_and_writes_receipt(tmp_path):
    venv, pypi = build_env(tmp_path)
    rollback = tmp_path / "rollback"
    result = upgrade(venv, pypi, rollback)

    assert result["ok"] is True, result
    assert result["applied"] is True
    assert result["rolled_back"] is False
    py = str(venv.python)
    assert normalized(venv.argv_list()) == [
        [py, "-I", "-c", updater.PROBE_SNIPPET],
        [py, "-m", "pip", "freeze"],
        [py, "-m", "pip", "install", "--no-deps", "--no-cache-dir", "<wheel>"],
        [py, "-m", "pip", "check"],
        [py, "-m", "pip", "freeze"],
        [py, "-I", "-c", updater.IMPORT_SMOKE_SNIPPET],
    ]
    assert venv.version == "0.16.0"

    # The import smoke test runs from the private staging directory, never the venv or the repository.
    smoke_cwd = venv.calls[-1][1]
    assert smoke_cwd is not None
    assert not smoke_cwd.is_relative_to(venv.root)
    assert not smoke_cwd.is_relative_to(Path(updater.__file__).resolve().parent)

    if os.name != "nt":  # POSIX permission bits; Windows ignores chmod modes
        assert stat.S_IMODE(rollback.stat().st_mode) == 0o700
    assert (rollback / "hermes_gpt-0.15.0-py3-none-any.whl").is_file()
    assert "hermes-gpt==0.15.0" in (rollback / "freeze-before.txt").read_text(encoding="utf-8")
    receipt = json.loads((rollback / "RECEIPT.json").read_text(encoding="utf-8"))
    assert receipt["previous_version"] == "0.15.0"
    assert receipt["target_version"] == "0.16.0"
    assert receipt["dependency_mode"] == "no-deps"
    assert receipt["wheels"]["target"]["source"] == "pypi"
    target_entry = json.loads(pypi.responses[updater.PYPI_JSON_URL.format(version="0.16.0")])["urls"][0]
    assert receipt["wheels"]["target"]["sha256"] == target_entry["digests"]["sha256"]
    assert receipt["wheels"]["target"]["filename"] == "hermes_gpt-0.16.0-py3-none-any.whl"
    assert all(step["status"] in {"ok", "skipped"} for step in receipt["steps"])
    assert "import-smoke" in [step["step"] for step in receipt["steps"]]
    assert "--force-reinstall" in (rollback / "ROLLBACK.md").read_text(encoding="utf-8")
    assert result["receipt"] == str(rollback / "RECEIPT.json")


def test_receipt_and_guides_contain_no_environment_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_GPT_TEST_SENTINEL", "sentinel-env-value-4242")
    venv, pypi = build_env(tmp_path)
    rollback = tmp_path / "rollback"
    assert upgrade(venv, pypi, rollback)["ok"] is True
    written = "".join(path.read_text(encoding="utf-8") for path in rollback.iterdir() if path.suffix in {".json", ".md", ".txt"})
    assert "sentinel-env-value-4242" not in written
    assert "HERMES_GPT_TEST_SENTINEL" not in written


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"venv_relative": True}, "VENV_NOT_ABSOLUTE"),
        ({"rollback_relative": True}, "ROLLBACK_DIR_NOT_ABSOLUTE"),
        ({"rollback_not_empty": True}, "ROLLBACK_DIR_NOT_EMPTY"),
        ({"target": "0.16.0rc1"}, "PRERELEASE_REFUSED"),
        ({"target": "v0.16.0"}, "INVALID_VERSION"),
        ({"service": "bad name"}, "SERVICE_NAME_INVALID"),
        ({"service": "../escape"}, "SERVICE_NAME_INVALID"),
        ({"service": "a" * 129}, "SERVICE_NAME_INVALID"),
        ({"wheel_relative": True}, "WHEEL_NOT_ABSOLUTE"),
        ({"venv_version": "0.16.0"}, "ALREADY_AT_TARGET"),
        ({"venv_version": "0.17.0"}, "DOWNGRADE_REFUSED"),
        ({"digest": "0" * 64}, "HASH_MISMATCH"),
        ({"target_requires": ("packaging>=24",)}, "DEPENDENCIES_CHANGED"),
        ({"publish_current": False}, "ROLLBACK_ARTIFACT_UNAVAILABLE"),
    ],
)
def test_refusals_happen_before_any_install(tmp_path, kwargs, code):
    options = dict(kwargs)
    env_options = {key: options.pop(key) for key in list(options) if key in {"target", "target_requires", "digest", "venv_version", "publish_current"}}
    target = env_options.get("target", "0.16.0")
    venv, pypi = build_env(tmp_path, **env_options)
    rollback = tmp_path / "rollback"
    if options.pop("rollback_not_empty", False):
        rollback.mkdir()
        (rollback / "old-artifact.whl").write_bytes(b"do not overwrite")
    venv_path = Path("relative-venv") if options.pop("venv_relative", False) else venv.root
    rollback_path = Path("relative-rollback") if options.pop("rollback_relative", False) else rollback
    wheel = Path("relative.whl") if options.pop("wheel_relative", False) else None
    service = options.pop("service", None)
    if service is not None:
        options["service"] = service
    result = updater.apply_package_upgrade(
        target_version=target,
        venv=venv_path,
        rollback_dir=rollback_path,
        runner=venv,
        fetch=pypi,
        now=FIXED_NOW,
        wheel=wheel,
        **options,
    )
    assert_refused(result, code, venv)
    if code == "ROLLBACK_DIR_NOT_EMPTY":
        assert (rollback / "old-artifact.whl").read_bytes() == b"do not overwrite"
    if code in {"HASH_MISMATCH", "DEPENDENCIES_CHANGED", "ROLLBACK_ARTIFACT_UNAVAILABLE"}:
        assert not rollback.exists()
    if code == "DEPENDENCIES_CHANGED":
        assert result["dependency_changes"] == {"added": ["packaging>=24"], "removed": ["packaging>=23"]}


def test_prerelease_is_allowed_only_with_explicit_flag(tmp_path):
    venv, pypi = build_env(tmp_path, target="0.16.0rc1")
    result = upgrade(venv, pypi, tmp_path / "rollback", target="0.16.0rc1", allow_prerelease=True)
    assert result["ok"] is True, result


def test_dependency_changes_install_normally_when_explicitly_allowed(tmp_path):
    venv, pypi = build_env(tmp_path, target_requires=("packaging>=24",))
    result = upgrade(venv, pypi, tmp_path / "rollback", allow_dependency_changes=True)
    assert result["ok"] is True, result
    install = next(argv for argv in venv.argv_list() if argv[2:4] == ["pip", "install"])
    assert "--no-deps" not in install
    # The no-deps freeze comparison does not apply, so only the pre-install snapshot freeze runs.
    freezes = [argv for argv in venv.argv_list() if argv[2:4] == ["pip", "freeze"]]
    assert len(freezes) == 1
    steps = {step["step"]: step["status"] for step in result["steps"]}
    assert steps["freeze-compare"] == "skipped"
    receipt = json.loads((tmp_path / "rollback" / "RECEIPT.json").read_text(encoding="utf-8"))
    assert receipt["dependency_mode"] == "dependencies-changed-allowed"


def test_operator_supplied_wheel_skips_target_download(tmp_path):
    venv, pypi = build_env(tmp_path)
    local_target = build_wheel(tmp_path / "local", "0.16.0")
    result = upgrade(venv, pypi, tmp_path / "rollback", wheel=local_target)
    assert result["ok"] is True, result
    assert not any("0.16.0" in url for url in pypi.calls)
    assert venv.version == "0.16.0"


def test_operator_supplied_wheel_must_match_target_version(tmp_path):
    venv, pypi = build_env(tmp_path)
    wrong = build_wheel(tmp_path / "local", "0.16.1")
    result = upgrade(venv, pypi, tmp_path / "rollback", wheel=wrong)
    assert_refused(result, "WHEEL_MISMATCH", venv)


def test_rollback_uses_installed_local_wheel_when_hash_matches(tmp_path):
    venv, pypi = build_env(tmp_path)
    local_current = build_wheel(tmp_path / "local", "0.15.0")
    venv.direct_url = {"url": local_current.as_uri(), "archive_info": {"hash": f"sha256={sha256_of(local_current)}"}}
    rollback = tmp_path / "rollback"
    result = upgrade(venv, pypi, rollback)
    assert result["ok"] is True, result
    assert not any("0.15.0" in url for url in pypi.calls)
    assert (rollback / "hermes_gpt-0.15.0-py3-none-any.whl").read_bytes() == local_current.read_bytes()
    receipt = json.loads((rollback / "RECEIPT.json").read_text(encoding="utf-8"))
    assert receipt["wheels"]["rollback"]["source"] == "local-file"


def test_directory_installs_have_no_verifiable_rollback_artifact(tmp_path):
    venv, pypi = build_env(tmp_path)
    venv.direct_url = {"url": (tmp_path / "src").as_uri(), "dir_info": {"editable": True}}
    result = upgrade(venv, pypi, tmp_path / "rollback")
    assert_refused(result, "ROLLBACK_ARTIFACT_UNAVAILABLE", venv)


def test_install_failure_rolls_back_to_snapshotted_wheel(tmp_path):
    venv, pypi = build_env(tmp_path)
    venv.fail = {"install"}
    rollback = tmp_path / "rollback"
    result = upgrade(venv, pypi, rollback)
    assert result["ok"] is False
    assert result["code"] == "INSTALL_FAILED"
    assert result["rolled_back"] is True
    assert venv.version == "0.15.0"
    py = str(venv.python)
    assert [py, "-m", "pip", "install", "--no-deps", "--no-cache-dir", "--force-reinstall", str(rollback / "hermes_gpt-0.15.0-py3-none-any.whl")] in venv.argv_list()
    receipt = json.loads((rollback / "RECEIPT.json").read_text(encoding="utf-8"))
    assert receipt["rolled_back"] is True and receipt["code"] == "INSTALL_FAILED"


def test_import_version_mismatch_triggers_rollback(tmp_path):
    venv, pypi = build_env(tmp_path)
    venv.import_override = "0.15.0"  # a stale import still resolves the old package
    result = upgrade(venv, pypi, tmp_path / "rollback")
    assert result["code"] == "IMPORT_VERSION_MISMATCH"
    assert result["rolled_back"] is True
    assert venv.version == "0.15.0"


def test_pip_check_failure_triggers_rollback(tmp_path):
    venv, pypi = build_env(tmp_path)
    venv.fail = {"check"}
    result = upgrade(venv, pypi, tmp_path / "rollback")
    assert result["code"] == "PIP_CHECK_FAILED"
    assert result["rolled_back"] is True


def test_failed_rollback_is_reported_for_manual_recovery(tmp_path):
    venv, pypi = build_env(tmp_path)
    venv.fail = {"install", "rollback"}
    result = upgrade(venv, pypi, tmp_path / "rollback")
    assert result["ok"] is False
    assert result["rolled_back"] is False
    assert result["rollback_failed"] is True


def test_restart_uses_launchctl_on_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater, "_current_uid", lambda: 501)
    venv, pypi = build_env(tmp_path)
    result = upgrade(venv, pypi, tmp_path / "rollback", service="hermes-gpt")
    assert result["ok"] is True, result
    assert venv.argv_list()[-1] == ["launchctl", "kickstart", "-k", "gui/501/hermes-gpt"]
    assert result["restart_command"] == ["launchctl", "kickstart", "-k", "gui/501/hermes-gpt"]


def test_restart_uses_systemd_user_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "linux")
    venv, pypi = build_env(tmp_path)
    result = upgrade(venv, pypi, tmp_path / "rollback", service="hermes-gpt.service")
    assert result["ok"] is True, result
    assert venv.argv_list()[-1] == ["systemctl", "--user", "restart", "hermes-gpt.service"]


def test_restart_is_refused_on_unsupported_platforms(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "win32")
    venv, pypi = build_env(tmp_path)
    (venv.root / "Scripts").mkdir(exist_ok=True)
    (venv.root / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    result = updater.apply_package_upgrade(
        target_version="0.16.0", venv=venv.root, rollback_dir=tmp_path / "rollback", service="hermes-gpt",
        runner=venv, fetch=pypi, now=FIXED_NOW,
    )
    assert_refused(result, "UNSUPPORTED_PLATFORM", venv)


def test_restart_failure_triggers_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater, "_current_uid", lambda: 501)
    venv, pypi = build_env(tmp_path)
    venv.fail = {"restart"}
    result = upgrade(venv, pypi, tmp_path / "rollback", service="hermes-gpt")
    assert result["code"] == "RESTART_FAILED"
    assert result["rolled_back"] is True
    assert venv.version == "0.15.0"


def test_venv_python_location_follows_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.sys, "platform", "win32")
    assert updater._venv_python(tmp_path) == tmp_path / "Scripts" / "python.exe"
    monkeypatch.setattr(updater.sys, "platform", "linux")
    assert updater._venv_python(tmp_path) == tmp_path / "bin" / "python"


def test_cli_apply_parses_package_options(monkeypatch, capsys, tmp_path):
    captured: dict = {}

    def fake_apply(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "applied": True}

    monkeypatch.setattr(updater, "apply_package_upgrade", fake_apply)
    updater.main([
        "--apply", "--version", "0.16.0", "--venv", str(tmp_path / "venv"), "--rollback-dir", str(tmp_path / "rb"),
        "--service", "hermes-gpt", "--wheel", str(tmp_path / "w.whl"), "--allow-dependency-changes",
    ])
    assert captured == {
        "target_version": "0.16.0",
        "venv": tmp_path / "venv",
        "rollback_dir": tmp_path / "rb",
        "service": "hermes-gpt",
        "wheel": tmp_path / "w.whl",
        "allow_dependency_changes": True,
        "allow_prerelease": False,
    }
    assert json.loads(capsys.readouterr().out)["applied"] is True


def test_cli_apply_exits_nonzero_on_refusal(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(updater, "apply_package_upgrade", lambda **kwargs: {"ok": False, "code": "VENV_INVALID"})
    with pytest.raises(SystemExit) as exc:
        updater.main(["--apply", "--version", "0.16.0", "--venv", str(tmp_path), "--rollback-dir", str(tmp_path / "rb")])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["code"] == "VENV_INVALID"


@pytest.mark.parametrize(
    "argv",
    [
        ["--version", "0.16.0", "--venv", "/x", "--rollback-dir", "/y"],
        ["--apply", "--version", "0.16.0", "--rollback-dir", "/y"],
        ["--apply", "--venv", "/x"],
        ["--apply", "--version", "0.16.0", "--venv", "/x", "--rollback-dir", "/y", "--pre"],
    ],
)
def test_cli_rejects_incomplete_or_mixed_package_options(argv):
    with pytest.raises(SystemExit) as exc:
        updater.main(argv)
    assert exc.value.code == 2


def test_cli_apply_without_version_keeps_checkout_update(monkeypatch, capsys):
    monkeypatch.setattr(updater, "check_for_update", lambda **kwargs: {"ok": True, **kwargs})
    updater.main(["--apply"])
    assert json.loads(capsys.readouterr().out) == {"ok": True, "apply": True, "include_prereleases": False}
