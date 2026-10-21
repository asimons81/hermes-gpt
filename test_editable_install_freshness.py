"""Editable-install freshness contract for every py-module (TEST-01 tier).

Regression for the 2026-10-20T23:18Z outage class: PR #57 added
``operator_hermes_agent_runner.py`` and declared it in pyproject
py-modules, but the deployed checkout sync (``deploy-checkout-sync.sh``)
fast-forwards files WITHOUT refreshing the PEP-660 editable install —
so the ``__editable___hermes_gpt_*_finder.py`` module map in the
production venv never learned the new module and BOTH always-on units
(hermes-gpt-server MCP :4751, hermes-gpt-sidecar :4750) crash-looped
``ModuleNotFoundError`` for 10 minutes until a human-shaped repair
reinstalled the package.

Property: for every module declared in ``[tool.setuptools] py-modules``,
the on-disk file exists AND, when an editable finder for this package is
active in the running interpreter, the finder's MAPPING resolves it.
Source-of-truth for the declaration is pyproject.toml, parsed with
tomllib — never a hardcoded list (a stale hardcoded list is exactly the
defect being guarded).
"""
from __future__ import annotations

import pathlib
import sys
try:
    import tomllib  # Python >= 3.11
except ImportError:  # pragma: no cover - exercised on the 3.10 floor legs
    import tomli as tomllib  # declared runtime shim: tomli; python_version < '3.11'

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent


def _declared_py_modules() -> list[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    modules = data["tool"]["setuptools"]["py-modules"]
    assert isinstance(modules, list) and modules, "py-modules must be a non-empty list"
    return modules


def _editable_finder_mapping() -> dict[str, str] | None:
    """Return the active PEP-660 editable MAPPING for hermes-gpt, if any."""
    for name in list(sys.modules):
        if "__editable___hermes_gpt_" in name and name.endswith("_finder"):
            return dict(sys.modules[name].MAPPING)
    return None


def test_every_declared_py_module_exists_on_disk():
    missing = [m for m in _declared_py_modules()
               if not (REPO_ROOT / f"{m}.py").is_file()]
    assert not missing, f"py-modules declared but not on disk: {missing}"


def test_no_silent_py_module_on_disk():
    """Every top-level operator_*/runner module file must be declared.

    Catches the inverse direction: a new module file that was never
    added to py-modules (the packaging half of the outage class).
    """
    declared = set(_declared_py_modules())
    on_disk = {
        p.stem for p in REPO_ROOT.glob("operator_*.py")
    } | {
        p.stem for p in REPO_ROOT.glob("runner_*.py")
    } | {"server", "seams", "token_store", "updater", "versioning", "oauth_auth",
         "hermes_state", "mcp_compat", "fabric_artifacts", "fabric_write_guard",
         "finance_worker", "codex_config", "codex_core", "codex_mcp"}
    undeclared = sorted(on_disk - declared)
    assert not undeclared, (
        f"module files present but missing from py-modules: {undeclared} — "
        "editable installs will raise ModuleNotFoundError for them"
    )


@pytest.mark.skipif(
    _editable_finder_mapping() is None,
    reason="no hermes-gpt editable finder active in this interpreter",
)
def test_editable_finder_resolves_every_declared_module():
    mapping = _editable_finder_mapping()
    assert mapping is not None
    unresolvable = [m for m in _declared_py_modules() if m not in mapping]
    assert not unresolvable, (
        f"declared py-modules missing from the active editable finder MAPPING: "
        f"{unresolvable} — reinstall the editable package in this venv"
    )
