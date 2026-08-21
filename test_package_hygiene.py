"""Package-content hygiene regression tests.

Builds the wheel + sdist and asserts the release-blocking hygiene guard
(tools/check_package_hygiene.py) reports NO forbidden private/operational
patterns or private/cache member names in either artifact.

Also unit-checks the scanner's pattern logic on synthetic content so a
regression in the guard itself is caught without a full build.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
GUARD = REPO_ROOT / "tools" / "check_package_hygiene.py"

sys.path.insert(0, str(REPO_ROOT / "tools"))
import check_package_hygiene as guard


@pytest.mark.parametrize(
    "text",
    [
        "/home/tony/projects/nexusOS",
        "workdir: /home/asimo/.hermes",
        "path=/home/realuser/.ssh/id_rsa",
    ],
)
def test_scan_flags_machine_home_paths(text):
    findings = guard.scan_text(text)
    assert any(name == "absolute_home_path" for name, _ in findings), findings


@pytest.mark.parametrize(
    "text",
    [
        "/home/user/.env",
        "/home/example/projects",
        "C:\\Users\\Alice\\hermes\\server.py",
        "bind 127.0.0.1:4750",
        "localhost:4750/mcp",
    ],
)
def test_scan_allows_placeholders_and_localhost(text):
    assert guard.scan_text(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "http://192.168.4.41:8765/",
        "10.0.0.8",
        "172.16.5.5",
        "172.31.255.255",
        "100.64.0.1",
        "100.127.255.255",
    ],
)
def test_scan_flags_private_and_tailscale_ips(text):
    findings = guard.scan_text(text)
    assert any(name in ("rfc1918_ip", "tailscale_ip") for name, _ in findings), findings


@pytest.mark.parametrize("text", ["TONY-GAMING-TOP", "Hermex"])
def test_scan_flags_machine_hostnames(text):
    findings = guard.scan_text(text)
    assert any(name == "machine_hostname" for name, _ in findings), findings


@pytest.mark.parametrize(
    "text",
    [
        "serves 9 profiles",
        "operator audit log records 2,491 actions",
        "1,453 sessions, 182,510 messages",
        "37 async delegations",
        "426 fleet work orders",
        "191 dispatches",
        "10 credentials",
        "919 system prompts",
    ],
)
def test_scan_flags_operational_metrics(text):
    findings = guard.scan_text(text)
    assert any(name == "operational_metric" for name, _ in findings), findings


@pytest.mark.parametrize(
    "text",
    [
        "the server binds 127.0.0.1 by default",
        "supported sizes: 64 KB / 128 KB / 256 KB / 512 KB",
        "runs on Python 3.10+",
        "no profiles configured yet",
        "zero records in store",
        "version 0.6.0",
        "mcp[cli]>=1.0,<2",
    ],
)
def test_scan_does_not_false_positive_on_public_content(text):
    assert guard.scan_text(text) == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("pkg/.env", "private_env_file"),
        ("pkg/.env.production", "private_env_file"),
        ("pkg/client.pem", "private_key_file"),
        ("pkg/private.key", "private_key_file"),
        ("pkg/id_ed25519", "private_key_file"),
        ("pkg/client-identity.p12", "private_key_file"),
        ("pkg/.ssh/config", "private_config_path"),
        ("pkg/nested/.aws/credentials", "private_config_path"),
        ("pkg/runtime.log", "private_log_file"),
        ("pkg/__pycache__/server.cpython-312.pyc", "private_cache_path"),
        ("pkg/.pytest_cache/v/cache/nodeids", "private_cache_path"),
    ],
)
def test_scan_flags_private_artifact_member_names(name, expected):
    findings = guard.scan_member_name(name)
    assert any(kind == expected for kind, _ in findings), findings


@pytest.mark.parametrize(
    "name",
    [
        "hermes_gpt/token_store.py",
        "hermes_gpt/oauth_auth.py",
        "share/hermes-gpt/docs/oauth.md",
        "share/hermes-gpt/examples/fleet-authority.example.json",
    ],
)
def test_member_name_scan_allows_public_auth_related_modules_and_docs(name):
    assert guard.scan_member_name(name) == []


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_synthetic_archives_reject_nested_private_configuration_and_key_bundles(tmp_path, kind):
    members = {
        "hermes_gpt/oauth_auth.py": b"# legitimate public auth source\n",
        "share/hermes-gpt/docs/oauth.md": b"Public OAuth documentation.\n",
        "pkg/nested/.kube/config": b"private configuration\n",
        "pkg/identity.pfx": b"private key bundle\n",
    }
    if kind == "wheel":
        artifact = tmp_path / "synthetic.whl"
        with zipfile.ZipFile(artifact, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
    else:
        artifact = tmp_path / "synthetic.tar.gz"
        with tarfile.open(artifact, "w:gz") as tf:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    findings = guard.scan_artifact(artifact)
    by_member = {(member, pattern) for member, pattern, _matched in findings}
    assert ("pkg/nested/.kube/config", "private_config_path") in by_member
    assert ("pkg/identity.pfx", "private_key_file") in by_member
    assert not any(member.endswith(("oauth_auth.py", "oauth.md")) for member, _pattern in by_member)


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory):
    """Build wheel + sdist once per module and return the artifact paths."""
    outdir = tmp_path_factory.mktemp("dist")
    try:
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(outdir)],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip(f"python -m build unavailable or failed: {exc}")
    artifacts = sorted(outdir.glob("*.whl")) + sorted(outdir.glob("*.tar.gz"))
    assert artifacts, "build produced no artifacts"
    return artifacts


def test_wheel_and_sdist_are_hygiene_clean(built_artifacts):
    """The release-blocking guard must pass on both built artifacts."""
    result = subprocess.run(
        [sys.executable, str(GUARD), *[str(a) for a in built_artifacts]],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"package hygiene guard failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "CLEAN" in result.stdout


def test_sdist_does_not_ship_internal_docs(built_artifacts):
    """Internal counsel/design packets must not be distributed."""
    import tarfile

    sdists = [a for a in built_artifacts if a.name.endswith(".tar.gz")]
    assert sdists, "no sdist built"
    with tarfile.open(sdists[0], "r:gz") as tf:
        names = tf.getnames()
    assert not any("docs/design/" in n or "docs/releases/" in n for n in names), (
        "sdist contains internal docs/design or docs/releases packets"
    )
    assert any("docs/release-notes-v0.6.0.md" in n for n in names), "sdist missing public release notes"
    assert any("docs/release-notes-v0.7.0.md" in n for n in names), "sdist missing v0.7 public release notes"
    assert any("docs/retention-policy.md" in n for n in names), "sdist missing public retention policy"
    assert any("docs/mcp-compatibility.md" in n for n in names), "sdist missing public MCP compatibility manifest"


def test_wheel_contains_public_docs_and_all_py_modules(built_artifacts):
    """Proof 10: wheel ships v0.7 docs and every declared top-level module."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    import zipfile

    wheels = [a for a in built_artifacts if a.name.endswith(".whl")]
    assert wheels, "no wheel built"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    for suffix in (
        "share/hermes-gpt/docs/mcp-compatibility.md",
        "share/hermes-gpt/docs/release-notes-v0.7.0.md",
    ):
        assert any(n.endswith(suffix) for n in names), f"wheel missing data file: {suffix}"
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        pyproject = tomllib.load(fh)
    modules = pyproject["tool"]["setuptools"]["py-modules"]
    assert modules, "pyproject.toml declares no py-modules"
    for module in modules:
        top_level = f"{module}.py"
        assert top_level in names, f"wheel missing py-module: {top_level}"
