"""Suite-determinism guards (rm-101).

Two invariants this repo has silently relied on:

1. Environment-dependent ``skipif`` markers are the ONLY reason a test result
   may differ between machines. They are few and deliberate; this guard makes
   the inventory explicit so a new skip cannot appear silently. If you add a
   skipif, register it here (and make sure its ``reason`` says what outside
   condition it depends on).

2. ``operator_policy``'s audit-log override is a process global. The
   autouse ``_reset_audit_log_override`` fixture in the root ``conftest.py``
   must guarantee it never leaks from one test into the next — proven here by
   a two-test sequence: the first leaks on purpose, the second asserts the
   pre-test state is clean.
"""

from __future__ import annotations

import re
from pathlib import Path

import operator_policy as op

_REPO_ROOT = Path(__file__).resolve().parent

# file name -> expected number of `skipif` occurrences (owner notes live in
# each marker's reason= string; keep counts and reasons accurate when adding
# or removing environment-dependent skips).
_EXPECTED_SKIPIF_COUNTS = {
    "test_runner_confinement.py": 6,
    "test_token_store.py": 1,
    "test_server.py": 1,
    "test_runner_confinement_env_shebang.py": 1,
    "test_operator_workspace.py": 1,
    "test_codex_mcp.py": 1,
    # v0.14.0 adoption: Linux-only /proc authority proof at module level
    # (pytestmark skipif not Path("/proc/self/environ").exists()); portable
    # Autopilot acceptance tests must stay runnable on every OS.
    "test_operator_autopilot_acceptance.py": 1,
    # TEST-01 editable-install freshness: the PEP-660 finder resolution
    # check only applies when this package is installed editable in the
    # running interpreter (dev/CI venvs: yes; bare wheel installs: no).
    "test_editable_install_freshness.py": 1,
}

_SKIP_PATTERN = re.compile(r"\bskipif\b")


def test_environment_dependent_skipif_inventory_is_registered():
    found: dict[str, int] = {}
    for path in sorted(_REPO_ROOT.glob("test_*.py")):
        if path.name == "test_suite_determinism.py":
            # This guard file: its own docstring/regex mention "skipif".
            continue
        count = len(_SKIP_PATTERN.findall(path.read_text(encoding="utf-8")))
        if count:
            found[path.name] = count
    expected = dict(_EXPECTED_SKIPIF_COUNTS)
    if found != expected:
        added = {k: v for k, v in found.items() if expected.get(k) != v}
        missing = {k: v for k, v in expected.items() if found.get(k) != v}
        raise AssertionError(
            "environment-dependent skipif inventory drifted. "
            f"added/changed: {added} removed/changed: {missing}. "
            "Every skipif must be deliberate: register it in "
            "_EXPECTED_SKIPIF_COUNTS here and give its reason= string an "
            "owner note naming the outside condition (platform, tool, env)."
        )


def test_audit_override_leaks_on_purpose():
    """Leak the process-global override with no teardown of our own."""
    op.set_audit_log_override(Path("/tmp/rm-101-leak-sentinel.jsonl"))
    # Deliberately no reset: the autouse conftest fixture must clean up.


def test_audit_override_did_not_leak_from_previous_test():
    """Runs after the leaker in collection order; state must be clean."""
    assert op._audit_log_override is None
