"""Keep unit tests isolated from the operator posture of the invoking shell."""

import os

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
)

# conftest.py is imported before test modules are collected. Clear live auth
# posture here as well as in the fixture so top-level imports remain hermetic.
for _name in _ISOLATED_ENV_VARS:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def isolate_operator_environment(monkeypatch):
    for name in _ISOLATED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
