# X Post Draft — Hermes GPT v0.4.0

## Option A: Short & Punchy (best for reach)

hermes-gpt v0.4.0 is out.

New this release:
- hermes_vision_analyze — ChatGPT can now see through your Hermes
- hermes_web_search + hermes_web_extract — browsing through your local agent
- hermes_cron_create — schedule jobs directly from MCP
- macOS gateway PID fix (thanks @NexaraCore)
- Vercel site now deploys (hermesgpt.vercel.app)

pip install hermes-gpt

Or grab it on GitHub: https://github.com/asimons81/hermes-gpt

See ya in the next one! 🤘

---

## Option B: Slightly Deeper (for the builders)

Hermes GPT is the MCP sidecar that exposes your local Hermes Agent to ChatGPT and any MCP client.

v0.4.0 adds:
- Vision analysis (images through your local agent)
- Web search & page extraction
- Cron job creation from MCP clients
- Gateway PID diagnostics (macOS fix from a contributor)

All env-gated, all dry-run by default, all audited. No secrets leaked. No shell=True.

https://github.com/asimons81/hermes-gpt

See ya in the next one! 🤘

---

## Option C: Thread format

1/ Heres something I've been cooking 🤘

hermes-gpt lets ChatGPT reach into your local Hermes Agent — files, search, skills, cron, config — through the MCP protocol.

v0.4.0 just dropped with some big additions.

2/ New tools:
- hermes_vision_analyze — ChatGPT sees your desktop
- hermes_web_search + hermes_web_extract — browse through Hermes
- hermes_cron_create — schedule jobs from any MCP client

All behind env gates. All dry-run safe. All audited.

3/ Also shipped the first external contribution — macOS gateway PID fix from @NexaraCore. Gateway diagnostics now expose state, kind, exit reason, active agents, and a clear PID source.

4/ The static site is live too: hermesgpt.vercel.app

pip install hermes-gpt
Or clone: https://github.com/asimons81/hermes-gpt

See ya in the next one! 🤘
