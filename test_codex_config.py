from pathlib import Path

import codex_config


def test_direct_project_install_is_idempotent_and_preserves_other_servers(tmp_path):
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    config = config_dir / "config.toml"
    config.write_text(
        "# existing user comment\n[mcp_servers.other]\ncommand = \"other.exe\"\nargs = []\n",
        encoding="utf-8",
    )
    first = codex_config.install(project=True, cwd=tmp_path, server_path=tmp_path / "server.py", prefer_cli=False)
    assert first["ok"] is True
    assert first["changed"] is True
    assert first["backup"]
    text = config.read_text(encoding="utf-8")
    assert "# existing user comment" in text
    assert "[mcp_servers.other]" in text
    assert codex_config.get_server_entry(config)["args"][-1] == "mcp"

    second = codex_config.install(project=True, cwd=tmp_path, server_path=tmp_path / "server.py", prefer_cli=False)
    assert second["ok"] is True
    assert second["changed"] is False

    removed = codex_config.uninstall(project=True, cwd=tmp_path)
    assert removed["ok"] is True
    assert removed["changed"] is True
    after = config.read_text(encoding="utf-8")
    assert "[mcp_servers.other]" in after
    assert "hermes-gpt" not in after


def test_install_refuses_conflicting_server_name(tmp_path):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("[mcp_servers.\"hermes-gpt\"]\ncommand = \"not-hermes.exe\"\nargs = [\"x\"]\n", encoding="utf-8")
    result = codex_config.install(project=True, cwd=tmp_path, prefer_cli=False)
    assert result["ok"] is False
    assert result["code"] == "NAME_CONFLICT"
    assert "not-hermes.exe" in config.read_text(encoding="utf-8")


def test_malformed_config_is_never_changed(tmp_path):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    original = "[mcp_servers\nthis is invalid"
    config.write_text(original, encoding="utf-8")
    result = codex_config.install(project=True, cwd=tmp_path, prefer_cli=False)
    assert result["ok"] is False
    assert result["code"] == "MALFORMED_CONFIG"
    assert config.read_text(encoding="utf-8") == original


def test_doctor_reports_registry_and_redaction_checks(tmp_path):
    result = codex_config.doctor(
        project=True,
        cwd=tmp_path,
        list_tools=lambda: ["hermes_status", "hermes_capabilities", "hermes_plan", "hermes_gateway_diagnostics"],
        status=lambda: {"ok": True, "gateway": "running"},
    )
    assert result["checks"]["mcp_tool_registry"]["status"] == "PASS"
    assert result["checks"]["redaction_smoke"]["status"] == "PASS"
    assert result["checks"]["gateway"]["status"] == "PASS"
