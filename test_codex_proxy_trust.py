"""Proxy-trust posture tests for the Codex HTTP runner (security t_f9925699).

The Codex ``mcp --http`` runner historically passed ``forwarded_allow_ips="*"``
to uvicorn, trusting any client-supplied X-Forwarded-For. The main server
derives proxy trust from authenticated posture only (loopback proxy allowlist
via HERMES_GPT_TRUSTED_PROXY_IPS). The Codex runner must not be the wildcard
outlier: omit the override so uvicorn's default (env FORWARDED_ALLOW_IPS or
loopback-only) applies.
"""

from __future__ import annotations

import server


def test_codex_http_runner_does_not_trust_wildcard_proxies(monkeypatch):
    captured: dict[str, object] = {}

    class StubApp:
        def streamable_http_app(self):
            return object()

    monkeypatch.setattr(server, "build_codex_mcp_server", lambda **kw: StubApp())

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))

    import os

    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    server._run_codex_mcp(["--http", "--host", "127.0.0.1", "--port", "7878"])

    assert captured.get("forwarded_allow_ips") != "*", (
        "codex HTTP runner must not trust wildcard proxies "
        f"(kwargs: {captured})"
    )
