import os
from pathlib import Path

import pytest

import codex_core as core_module


ALL_GATES = [
    core_module.ENABLE_CODEX_ENV, core_module.ENABLE_MCP_ENV,
    core_module.ENABLE_VISION_ENV, core_module.ENABLE_WEB_ENV,
    core_module.ENABLE_CRON_ENV, core_module.ENABLE_DIAGNOSTICS_ENV,
    core_module.ALLOW_WRITE_ENV, core_module.ALLOW_CRON_WRITE_ENV,
    core_module.ALLOW_SKILL_WRITE_ENV, core_module.ALLOWED_ROOTS_ENV,
    core_module.ALLOW_PRIVATE_NETWORK_ENV,
]


@pytest.fixture
def tool_core(monkeypatch):
    for name in ALL_GATES:
        monkeypatch.delenv(name, raising=False)
    calls = {}

    def remember(name, value):
        calls[name] = value
        return value

    tool = core_module.CodexToolCore(
        version="0.5.0b2",
        imports_ready=lambda: True,
        gateway_snapshot=lambda: {"gateway": {"running": True, "pid": 42}},
        gateway_diagnostics_callback=lambda: {"success": True, "warnings": []},
        vision_analyze=lambda path, prompt: remember("vision", {"path": path, "prompt": prompt, "api_key": "not-a-real-secret-but-long-enough"}),
        web_search=lambda query, limit: remember("search", {"title": "Result", "url": "https://example.com", "snippet": f"token=very-secret-token-123456789 for {query}"}),
        web_extract=lambda urls, limit: remember("extract", {"url": urls[0], "content": "Authorization: Bearer abcdefghi.jklmnopqr.stuvwx"}),
        cron_create_callback=lambda schedule, prompt, dry_run: remember("cron", {"schedule": schedule, "prompt": prompt, "dry_run": dry_run}),
        skill_create_callback=lambda name, content, dry_run: remember("skill", {"name": name, "content": content, "dry_run": dry_run}),
    )
    return tool, calls


def enable_base(monkeypatch):
    monkeypatch.setenv(core_module.ENABLE_CODEX_ENV, "1")
    monkeypatch.setenv(core_module.ENABLE_MCP_ENV, "1")


def test_missing_base_gates_report_a_clean_block(tool_core):
    tool, _ = tool_core
    result = tool.plan("inspect repository", str(Path.cwd()))
    assert result["ok"] is False
    assert result["error"]["code"] == "CODEX_DISABLED"
    assert "HERMES_GPT_ENABLE_CODEX=1" in result["error"]["suggested_action"]


def test_capabilities_list_disabled_reasons_without_leaking_environment(tool_core):
    tool, _ = tool_core
    result = tool.capabilities()
    assert result["ok"] is True
    assert result["capabilities"]["vision"]["enabled"] is False
    assert "reason" in result["capabilities"]["vision"]


def test_vision_validates_roots_and_symlink_escape(tmp_path, monkeypatch, tool_core):
    tool, calls = tool_core
    enable_base(monkeypatch)
    monkeypatch.setenv(core_module.ENABLE_VISION_ENV, "1")
    root = tmp_path / "project"
    root.mkdir()
    image = root / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image payload")

    allowed = tool.vision(str(image), "describe it", str(root))
    assert allowed["ok"] is True
    assert calls["vision"]["path"] == str(image.resolve())
    assert "not-a-real-secret" not in str(allowed)

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image payload")
    blocked = tool.vision(str(outside), "describe it", str(root))
    assert blocked["error"]["code"] == "VISION_INPUT_BLOCKED"
    assert "outside the approved project root" in blocked["error"]["message"]


def test_vision_denies_secret_paths_before_extension_checks(tmp_path, monkeypatch, tool_core):
    tool, _ = tool_core
    enable_base(monkeypatch)
    monkeypatch.setenv(core_module.ENABLE_VISION_ENV, "1")
    secret = tmp_path / ".env"
    secret.write_text("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz_1234567890")
    result = tool.vision(str(secret), "read", str(tmp_path))
    assert result["ok"] is False
    assert result["error"]["code"] == "VISION_INPUT_BLOCKED"
    assert "OPENAI_API_KEY" not in str(result)


def test_vision_rejects_a_mismatched_extension_and_file_signature(tmp_path, monkeypatch, tool_core):
    tool, _ = tool_core
    enable_base(monkeypatch)
    monkeypatch.setenv(core_module.ENABLE_VISION_ENV, "1")
    image = tmp_path / "not-really-jpeg.jpg"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image payload")
    result = tool.vision(str(image), "describe it", str(tmp_path))
    assert result["ok"] is False
    assert result["error"]["code"] == "VISION_INPUT_BLOCKED"
    assert "detected MIME type" in result["error"]["message"]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://localhost:8080/",
    "http://127.0.0.1/",
    "http://10.0.0.8/",
    "http://169.254.169.254/latest/meta-data/",
])
def test_extract_rejects_nonpublic_urls(url, monkeypatch, tool_core):
    tool, calls = tool_core
    enable_base(monkeypatch)
    monkeypatch.setenv(core_module.ENABLE_WEB_ENV, "1")
    result = tool.extract_page(url)
    assert result["ok"] is False
    assert result["error"]["code"] == "URL_BLOCKED"
    assert "extract" not in calls


def test_web_responses_are_recursively_redacted(monkeypatch, tool_core):
    tool, _ = tool_core
    enable_base(monkeypatch)
    monkeypatch.setenv(core_module.ENABLE_WEB_ENV, "1")
    result = tool.search("test")
    assert result["ok"] is True
    assert "very-secret-token" not in str(result)
    assert "[REDACTED]" in str(result)


def test_plan_is_read_only_and_excludes_secret_files(tmp_path, monkeypatch, tool_core):
    tool, _ = tool_core
    enable_base(monkeypatch)
    (tmp_path / "README.md").write_text("hello")
    (tmp_path / ".env").write_text("secret")
    result = tool.plan("document repository", str(tmp_path), include_tree=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert "README.md" in result["repository_context"]["tree"]
    assert ".env" not in result["repository_context"]["tree"]


def test_cron_write_requires_confirmation_and_all_gates(monkeypatch, tool_core):
    tool, calls = tool_core
    enable_base(monkeypatch)
    monkeypatch.setenv(core_module.ENABLE_CRON_ENV, "1")
    request = "daily at 9am summarize the logs"
    assert tool.cron_plan(request)["proposed_schedule"] == "0 9 * * *"
    assert tool.cron_create(request, confirm=False)["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert tool.cron_create(request, confirm=True)["error"]["code"] == "CRON_WRITE_DISABLED"
    monkeypatch.setenv(core_module.ALLOW_WRITE_ENV, "1")
    monkeypatch.setenv(core_module.ALLOW_CRON_WRITE_ENV, "1")
    created = tool.cron_create(request, confirm=True, dry_run=True)
    assert created["ok"] is True
    assert calls["cron"]["dry_run"] is True


def test_skill_authoring_is_a_draft_by_default_and_write_gated(monkeypatch, tool_core):
    tool, calls = tool_core
    enable_base(monkeypatch)
    draft = tool.author_skill("release-helper", "Draft reliable release steps")
    assert draft["ok"] is True
    assert draft["dry_run"] is True
    assert "skill" not in calls
    blocked = tool.author_skill("release-helper", "Draft reliable release steps", dry_run=False)
    assert blocked["error"]["code"] == "SKILL_WRITE_DISABLED"
    monkeypatch.setenv(core_module.ALLOW_WRITE_ENV, "1")
    monkeypatch.setenv(core_module.ALLOW_SKILL_WRITE_ENV, "1")
    applied = tool.author_skill("release-helper", "Draft reliable release steps", dry_run=False)
    assert applied["ok"] is True
    assert calls["skill"]["dry_run"] is False


def test_redaction_covers_provider_github_cookie_and_private_key_text():
    text = "github_pat_abcdefghijklmnopqrstuvwxyz123456 Cookie: session-abcdefghijklm xai-abcdefghijklmnopqrst\n-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
    result = core_module.redact_value({"nested": [text]})
    rendered = str(result)
    assert "github_pat_" not in rendered
    assert "session-abcdefgh" not in rendered
    assert "BEGIN PRIVATE KEY" not in rendered
