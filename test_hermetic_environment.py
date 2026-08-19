"""Hermeticity regression tests (audit t_9d200636, Class A).

The suite historically read the developer's real ``~/.hermes/config.yaml``
during fleet/swarm/contract tests (via ``operator_fleet._load_hermes_config``
-> ``hermes_cli`` config resolution) and ``test_ui_chat`` poisoned ``sys.path``
for every later module by inserting the real Hermes agent source root at
collection time. These tests pin both isolation properties:

1. under the default test environment the fleet registry must see NO peers
   (the hermetic sandbox has no ``a2a_agents`` config);
2. importing ``test_ui_chat`` in a clean interpreter must not add any path
   outside the repository to ``sys.path`` (and must not make ``hermes_cli``
   importable).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import operator_fleet

REPO = Path(__file__).resolve().parent


def test_fleet_registry_reads_no_real_machine_peers_under_default_test_env():
    """No test may observe the invoking machine's real a2a_agents config."""
    peers = operator_fleet._a2a_peers()
    assert peers == {}, (
        "operator_fleet read a real config.yaml during tests "
        f"(HERMES_HOME={os.environ.get('HERMES_HOME')!r}, peers={sorted(peers)})"
    )


def test_hermes_home_is_redirected_to_a_sandbox_without_real_config():
    home = os.environ.get("HERMES_HOME")
    assert home, "conftest must redirect HERMES_HOME for the whole suite"
    sandbox = Path(home)
    assert sandbox != Path.home() / ".hermes", (
        "HERMES_HOME still points at the invoking user's real data root"
    )
    config = sandbox / "config.yaml"
    if config.exists():
        raw = config.read_text(encoding="utf-8", errors="replace")
        assert "a2a_agents" not in raw, "sandbox config leaks real A2A peers"
    profile = os.environ.get("HERMES_PROFILE")
    assert profile in (None, ""), f"HERMES_PROFILE leaks the invoking shell: {profile!r}"


def test_importing_test_ui_chat_does_not_inject_outside_paths():
    """test_ui_chat collection must not mutate global sys.path beyond the repo."""
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "import test_ui_chat\n"
        "print(json.dumps(sys.path))\n"
        "print(json.dumps(sorted(m for m in sys.modules if m == 'hermes_cli')))\n"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("HERMES_HOME", None)
    env.pop("HERMES_PROFILE", None)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    imported_sys_path = json.loads(lines[0])
    hermes_cli_modules = json.loads(lines[1])
    outside = [
        p
        for p in imported_sys_path
        if ".hermes" in p and not p.startswith(str(REPO))
    ]
    assert not outside, f"test_ui_chat import injected non-repo paths: {outside}"
    assert hermes_cli_modules == [], "test_ui_chat import made hermes_cli importable"
