"""The browser observes the exact runtime selection through read gates only."""
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import operator_policy as op
import ui_ops


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.delenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", raising=False)
    monkeypatch.setattr(ui_ops, "_agent_root_provider", lambda: tmp_path / "exact-runtime-selection")
    return TestClient(Starlette(routes=ui_ops.ui_ops_routes()))


def test_get_uses_exact_running_server_selection_and_does_not_write(client, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ui_ops.op_agent_readiness, "readiness", lambda root: calls.append(root) or {"read_only": True, "live_acceptance": "not_verified"})
    response = client.get("/api/ops/agent-readiness")
    assert response.status_code == 200
    assert calls == [tmp_path / "exact-runtime-selection"]
    assert response.json()["data"]["live_acceptance"] == "not_verified"
    assert client.post("/api/ops/agent-readiness", json={}).status_code == 405
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("env,value", [
    ("HERMES_GPT_MISSION_ALLOWED_SURFACES", ""),
    ("HERMES_GPT_MISSION_ALLOWED_SURFACES", "approvals,delegations"),
    (op.OPERATOR_ENABLED_ENV, "0"),
])
def test_denied_reads_cannot_probe(client, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    monkeypatch.setattr(ui_ops.op_agent_readiness, "readiness", lambda *_: pytest.fail("denied read must not probe"))
    assert client.get("/api/ops/agent-readiness").status_code == 403


def test_configured_health_allowlist_allows_read(client, monkeypatch):
    monkeypatch.setenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", "health")
    monkeypatch.setattr(ui_ops.op_agent_readiness, "readiness", lambda *_: {"read_only": True})
    assert client.get("/api/ops/agent-readiness").status_code == 200


def test_failures_return_closed_error_without_source_path(client, monkeypatch):
    def broken(_):
        raise OSError("private /source/root token=private-value")
    monkeypatch.setattr(ui_ops.op_agent_readiness, "readiness", broken)
    response = client.get("/api/ops/agent-readiness")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "READINESS_UNAVAILABLE"
    assert "private" not in response.text


def test_http_mount_binds_server_selection_without_reimporting_or_rediscovering(monkeypatch, tmp_path):
    import oauth_auth
    import server

    selected = tmp_path / "runtime-agent"
    monkeypatch.setattr(server, "HERMES_ROOT", selected)
    monkeypatch.setattr(ui_ops, "_agent_root_provider", lambda: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GPT_UI_ENABLED", "1")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.delenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", raising=False)
    monkeypatch.delenv(oauth_auth.OAUTH_ENABLE_ENV, raising=False)
    monkeypatch.delenv(oauth_auth.AUTH_TOKEN_ENV, raising=False)
    calls = []
    monkeypatch.setattr(ui_ops.op_agent_readiness, "readiness", lambda root: calls.append(root) or {"read_only": True})
    monkeypatch.setattr(server, "find_hermes_root", lambda: pytest.fail("runtime root must not be rediscovered"))
    app = server.build_asgi_app(server.build_server(http=True), http=True)
    with TestClient(app, base_url="http://127.0.0.1") as mounted:
        assert mounted.get("/api/ops/agent-readiness").status_code == 200
    assert calls == [selected]
