# Hermes GPT v0.16.2 — session compatibility and reliability

Status: prepared in source; verify GitHub and PyPI independently before claiming publication.

- Session control uses the native finite `chat -q --format stream-json` interface, without unattended autoapproval. Matching terminal results determine completion; stdout progress never substitutes for the answer.
- Durable reservations stop duplicate submissions after unknown outcomes. Bounded parsing and redacted final-only persistence keep tool bodies out of session-job results. Existing session/profile gates remain in force; there is no automatic retry or unlock.
- Autopilot writes its timeout/crash outcome before terminalizing its job, closing the racing status-read window from #101.
- Tag publication consumes the wheel/sdist already built, tested and hygiene-checked by CI. A mismatched tag/version is refused.

See [session control](session-control.md) for protocol limits and uncertain-outcome handling. Live Desktop acceptance remains unverified; this release does not claim end-to-end Windows session acceptance or close #103 on that basis. The new transport is covered offline.

The neutral tool baseline is 139; enabling Finance makes it 140. No new MCP tool or session-call argument is introduced. Follow [the release checklist](../RELEASE_CHECKLIST.md), including both SDK families and installed-wheel/client verification. Publication does not update running services or cached connector schemas.
