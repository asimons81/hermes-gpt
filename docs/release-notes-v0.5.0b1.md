# Hermes GPT v0.5.0b1

The first v0.5.0 beta batch adds Codex integration through a local, curated MCP server.

- `hermes-gpt mcp` and `hermes-gpt codex mcp` expose planning, vision, web, cron planning, skill drafting, and gateway diagnostics to Codex.
- `hermes-gpt codex install`, `uninstall`, `doctor`, and `print-config` manage the local MCP entry safely.
- The Codex facade adds capability gates, strict write gates, recursive secret redaction, local image path validation, and public URL SSRF protections.
- New setup and safety documentation is available in [codex.md](codex.md).

This is a beta batch, not the complete v0.5.0 release.
