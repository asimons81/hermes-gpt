# Hermes GPT data retention and cleanup policy

**Status:** current v0.6.0 operational policy  
**Owner:** Hermes GPT maintainer

For documentation authority rules, see [docs/README.md](README.md).

## Scope

This policy covers local diagnostic and orchestration artifacts that can remain on the host even though Mission Control does not return their raw contents.

Mission Control does not return raw request dumps, Codex CLI transcripts, prompts, messages, memory bodies, credentials, or profile-secret bodies.

## Retention windows

| Artifact class | Maximum window | Enforcement in v0.6.0 |
| --- | --- | --- |
| `request_dump_*.json` | 7 days from file mtime | maintainer cleanup |
| Codex job metadata/transcript artifacts | 30 days after terminal completion | age-based cleanup during `operator_codex` reconciliation |
| terminal Swarm worktrees | 7 days after terminal state and declared artifact preservation | maintainer cleanup through normal Git worktree handling |
| terminal Swarm workflow/verdict records | 30 days after terminal state | maintainer cleanup |
| failed/abandoned workflow records | same windows, measured from last state transition | maintainer cleanup |

### Codex-specific automatic cleanup

`operator_codex` defines `RETENTION_DAYS=30` and performs age-based cleanup as part of job reconciliation.

This is **not** a general background deletion daemon. If no Codex reconciliation occurs, do not describe the policy as a continuously scheduled purge.

### Other artifact classes

v0.6.0 does not install a global cleanup service for request dumps, Swarm worktrees, or Swarm workflow/verdict records. The maintainer remains responsible for keeping those classes within the stated windows unless later code explicitly adds and tests automation.

## Cleanup procedure

1. Run cleanup locally under the authenticated maintainer account.
2. Identify candidates only inside the expected artifact directories and use mtime / terminal timestamps rather than opening sensitive content.
3. Confirm the item is outside its retention window and is no longer required by an active workflow.
4. For Swarm worktrees, first confirm the workflow is terminal and declared artifacts have been copied to the owning repository or attached to the intended work record.
5. Remove worktrees through the repository's normal worktree cleanup path.
6. Record only bounded operational metadata in the maintenance/audit receipt, such as aggregate counts, safe IDs/paths, and timestamps.
7. Never copy raw prompt, transcript, request, credential, or secret content into the audit record or client response.

## Exceptions

If an artifact is subject to an investigation, legal hold, or active incident response:

- suspend deletion for that specific item;
- record the exception and responsible owner using bounded metadata;
- do not broaden the exception to unrelated artifacts.

## Release and compliance posture

This is an operational retention policy, not a claim of legal or regulatory compliance.

Any future cleanup automation must remain:

- path-allowlisted;
- bounded;
- independently tested;
- auditable;
- conservative around active workflows and legal-hold exceptions.

If documentation and implementation disagree about cleanup behavior, verify `operator_codex.py`, Swarm storage code, and the relevant tests before updating the claim.
