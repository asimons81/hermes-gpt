"""Keep unit tests isolated from the operator posture of the invoking shell."""

import pytest


@pytest.fixture(autouse=True)
def isolate_operator_environment(monkeypatch):
    for name in (
        "HERMES_GPT_OPERATOR_ENABLED", "HERMES_GPT_OPERATOR_LEVEL",
        "HERMES_GPT_OPERATOR_APPLY_MODE", "HERMES_GPT_OPERATOR_ALLOWED_PATHS",
        "HERMES_GPT_OPERATOR_ALLOWED_PROFILES", "HERMES_GPT_OWNER_ACK",
        "HERMES_GPT_ENABLE_CODEX_RUNNER", "HERMES_GPT_ALLOW_CODEX_WRITE",
        "HERMES_GPT_CODEX_TOOLSET",
    ):
        monkeypatch.delenv(name, raising=False)
