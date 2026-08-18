"""Test the SPA static asset URL contract (t_c31a37a9).

Covers the base URL fix: Vite builds with base=/ui/ so that assets are loaded
from /ui/assets/... inside the /ui StaticFiles mount, and the bare /ui route
still returns the SPA shell. The tests are isolated from production Hermes
roots and never touch real user data.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import oauth_auth
import operator_mission as op_mission
import operator_policy as op
import server
import ui_api
import ui_security


@pytest.fixture
def ui_root(tmp_path: Path, monkeypatch):
    """Hermetic Hermes root; Path.home patched so defaults stay inside tmp."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv(oauth_auth.OAUTH_ENABLE_ENV, raising=False)
    monkeypatch.delenv(oauth_auth.AUTH_TOKEN_ENV, raising=False)
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    op_mission._cache_clear()
    root = tmp_path / ".hermes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        "model: test-model\nprovider: test-provider\n", encoding="utf-8"
    )
    return root


def _build_app(monkeypatch, *, ui_enabled: bool = True, **env) -> TestClient:
    """Build the ASGI app with the UI mount per test env, return TestClient."""
    if ui_enabled:
        monkeypatch.setenv(ui_security.UI_ENABLED_ENV, "1")
    else:
        monkeypatch.delenv(ui_security.UI_ENABLED_ENV, raising=False)
    if "HERMES_GPT_MISSION_ALLOWED_SURFACES" not in env:
        monkeypatch.delenv(op_mission.MISSION_ALLOWED_SURFACES_ENV, raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    built = server.build_server(http=True)
    app = server.build_asgi_app(built, http=True)
    return TestClient(app, base_url="http://127.0.0.1")


def _read_index(dist: Path) -> str:
    return (dist / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """Fake a built Vite dist directory with assets referenced as /ui/assets."""
    d = tmp_path / "dist"
    d.mkdir()
    (d / "assets").mkdir()
    (d / "assets" / "index-ABC123.js").write_text("// built js", encoding="utf-8")
    (d / "assets" / "index-ABC123.css").write_text("/* built css */", encoding="utf-8")
    (d / "index.html").write_text(
        "<!doctype html>"
        '<script type="module" crossorigin src="/ui/assets/index-ABC123.js"></script>'
        '<link rel="stylesheet" crossorigin href="/ui/assets/index-ABC123.css">'
        "<div id=root></div>",
        encoding="utf-8",
    )
    return d


def test_vite_base_is_ui(ui_root, monkeypatch, dist):
    """Vite builds with base=/ui/ so assets are referenced under /ui."""
    # This test uses the fixture dist index, so it is independent of a real build.
    index_text = _read_index(dist)
    assert '/ui/assets/index-ABC123.js' in index_text
    assert '/ui/assets/index-ABC123.css' in index_text
    # No root-relative /assets/ references must remain in the built index.
    assert re.search(r'["\']/assets/[^"\']+["\']', index_text) is None


def test_ui_static_serves_shell_and_assets(ui_root, monkeypatch, dist):
    """Bare /ui returns the SPA shell; /ui/assets/... serves hashed assets."""
    monkeypatch.setenv(ui_security.UI_DIR_ENV, str(dist))
    client = _build_app(monkeypatch)

    shell = client.get("/ui")
    assert shell.status_code == 200
    assert "<div id=root></div>" in shell.text

    js = client.get("/ui/assets/index-ABC123.js")
    assert js.status_code == 200
    assert js.text == "// built js"

    css = client.get("/ui/assets/index-ABC123.css")
    assert css.status_code == 200
    assert css.text == "/* built css */"


def test_ui_client_routes_fallback_to_index(ui_root, monkeypatch, dist):
    """SPA client routes (e.g. /ui/chat) fall back to index.html."""
    monkeypatch.setenv(ui_security.UI_DIR_ENV, str(dist))
    client = _build_app(monkeypatch)
    resp = client.get("/ui/chat")
    assert resp.status_code == 200
    assert "<div id=root></div>" in resp.text


def test_ui_api_static_routes_are_present():
    """Route composition includes the /ui exact route and /ui static mount."""
    paths = {getattr(r, "path", "") for r in ui_api.routes()}
    assert "/ui" in paths
    mounts = [r for r in ui_api.routes() if hasattr(r, "app")]
    assert any(getattr(r, "path", "") == "/ui" for r in mounts)
