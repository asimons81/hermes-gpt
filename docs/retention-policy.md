# Hermes GPT data retention and cleanup policy

Status: v0.6.0 release policy
Owner: Hermes GPT maintainer

## Scope and exclusion

Mission Control never returns request dumps, Codex CLI transcripts, raw prompts, messages, memory bodies, or profile secrets. This policy governs the diagnostic artifacts that remain local despite that exclusion.

## Retention windows

- Request dumps (`request_dump_*.json`): retain at most 7 days from file modification time. They are diagnostic-only and may contain raw request bodies.
- Codex transcripts and job artifacts: retain at most 14 days after terminal job completion. Keep only bounded job metadata required for operational status.
- M2 swarm worktrees: retain at most 7 days after a workflow reaches a terminal state and its declared artifacts are copied to the owning repository or attached to its Kanban card.
- M2 verdict JSON/workflow records: retain at most 30 days after terminal state. Records must remain bounded and redacted; no transcript or prompt body is permitted.
- Failed/abandoned workflow records: apply the same windows from the last state transition; do not preserve them indefinitely for debugging.

## Cleanup procedure

1. Run cleanup locally under the authenticated maintainer account; do not send artifact contents to a client or log them in a summary.
2. Identify candidates by mtime/terminal timestamp, confirm they are within the directories above, and delete only the eligible files/worktrees.
3. Remove request dumps and transcripts without opening or copying their content. Remove worktrees through the repository's normal worktree cleanup path only after confirming the workflow is terminal.
4. Record only aggregate counts, paths or IDs, and timestamps in the operator audit/maintenance receipt. Never record raw text.
5. If an artifact is subject to an investigation, legal hold, or active incident response, suspend deletion for that item and document the exception with the responsible owner.

## Enforcement and release posture

This is an operational policy and release gate, not a claim of legal compliance. v0.6.0 does not install an automatic deletion daemon; the maintainer must perform and audit the bounded cleanup procedure before artifacts exceed the stated window. Any automation added later must be dry-run-first, path-allowlisted, and independently tested.
