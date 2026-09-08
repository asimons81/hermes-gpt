"""Keep unit tests isolated from the operator posture of the invoking shell.

Also sandboxes the Hermes data root (audit t_9d200636 Class A): fleet/swarm/
contract tests resolve peers via ``operator_fleet._load_hermes_config()`` ->
``hermes_cli`` config resolution, which otherwise reads the invoking user's
real ``~/.hermes/config.yaml`` (including its ``a2a_agents``). The sandbox
provides an empty config so injected runners stay authoritative.
"""

import os
import tempfile
from pathlib import Path

import pytest


_ISOLATED_ENV_VARS = (
    "HERMES_GPT_OPERATOR_ENABLED",
    "HERMES_GPT_OPERATOR_LEVEL",
    "HERMES_GPT_OPERATOR_APPLY_MODE",
    "HERMES_GPT_OPERATOR_ALLOWED_PATHS",
    "HERMES_GPT_OPERATOR_ALLOWED_PROFILES",
    "HERMES_GPT_OWNER_ACK",
    "HERMES_GPT_OWNER_ACTIVE",
    "HERMES_GPT_ENABLE_CODEX_RUNNER",
    "HERMES_GPT_ALLOW_CODEX_WRITE",
    "HERMES_GPT_CODEX_TOOLSET",
    "HERMES_GPT_CODEX_EXE",
    "HERMES_GPT_OAUTH_ENABLE",
    "HERMES_GPT_OAUTH_ISSUER",
    "HERMES_GPT_OAUTH_CLIENT_ID",
    "HERMES_GPT_OAUTH_CLIENT_SECRET",
    "HERMES_GPT_OAUTH_REDIRECT_URI",
    "HERMES_GPT_OAUTH_SCOPE",
    "HERMES_GPT_BEARER_TOKEN",
    # Hermes-side identity env: never inherit the invoking shell's profile or
    # data root during tests. Cleared at import time so collection-time module
    # imports resolve against the sandbox, not the real machine.
    "HERMES_PROFILE",
)

# Session-scoped hermetic Hermes data root. Lives for the pytest process only.
_HERMES_SANDBOX: Path | None = None


def _hermes_sandbox() -> Path:
    global _HERMES_SANDBOX
    if _HERMES_SANDBOX is None:
        _HERMES_SANDBOX = Path(tempfile.mkdtemp(prefix="hgpt-test-hermes-"))
        (_HERMES_SANDBOX / "config.yaml").write_text(
            "# hermetic test sandbox: no A2A peers configured\n",
            encoding="utf-8",
        )
    return _HERMES_SANDBOX


# conftest.py is imported before test modules are collected. Clear live auth
# posture here as well as in the fixture so top-level imports remain hermetic.
for _name in _ISOLATED_ENV_VARS:
    os.environ.pop(_name, None)

# Redirect the Hermes data root before any test module imports (fleet/swarm/
# contract modules resolve config lazily, but ui modules may touch the root at
# import time). An explicit sandbox config.yaml with no a2a_agents keeps the
# official A2A registry empty so injected test runners stay authoritative.
os.environ["HERMES_HOME"] = str(_hermes_sandbox())

# Deterministic MIME type database. The minimal Arch/Python mimetypes DB does
# not map common office MIME types (e.g. .xlsx), which makes
# test_operator_export.py::test_export_returns_mcp_embedded_blob_without_local_path
# environment-dependent (it asserted the canonical spreadsheet type while the
# module fell back to application/octet-stream). CI images ship the full DB.
# Seed the standard office types so the local suite behaves like a standard
# desktop/CI host; this only ADDS mappings and never weakens an assertion.
import mimetypes as _mimetypes

_OFFICE_MIME = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
}
for _ext, _mime in _OFFICE_MIME.items():
    if _mimetypes.guess_type(f"report{_ext}")[0] != _mime:
        _mimetypes.add_type(_mime, _ext)


@pytest.fixture(autouse=True)
def isolate_operator_environment(monkeypatch):
    for name in _ISOLATED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(_hermes_sandbox()))
