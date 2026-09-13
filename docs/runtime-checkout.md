# Live runtime checkout pin (host deployment provenance)

This note records **which checkout the live `hermes-gpt-server.service` actually
serves**, so agents do not assume the live surface equals the branch they happened
to inspect. It is host-deployment state, not a product contract. Verify it fresh
before trusting a "we're updated" claim; it can change without a code change.

## The live service

- Unit: `hermes-gpt-server.service` (user service; override.conf in
  `~/.config/systemd/user/hermes-gpt-server.service.d/`)
- Status: `active`
- Effective `ExecStart`:
  `/home/tony/hermes-gpt/.venv/bin/python /home/tony/hermes-gpt-runtime/server.py --http --host 127.0.0.1 --port 4750 --profile remote`
- Effective `WorkingDirectory`: `/home/tony/hermes-gpt-runtime`
- `ExecMainStartTimestamp` (last verified): `Sat 2026-09-05 16:56:05 CDT`

The Python interpreter comes from the **main repo's** `.venv` (`/home/tony/hermes-gpt/.venv`,
which carries the FastMCP-compatible `mcp[cli]>=1.0,<2`); the **code** it runs comes
from the runtime worktree because `server.py` is invoked by that absolute path and
imports resolve from its own directory. `hermes_cli`/tools arrive via `PYTHONPATH`
pointing at the Hermes Agent source tree.

## The live checkout

- Path: `/home/tony/hermes-gpt-runtime`
- It is a **git worktree** of `/home/tony/hermes-gpt` (`git rev-parse --git-common-dir`
  resolves to `/home/tony/hermes-gpt/.git`). It and the main repo share one `.git`.
- Worktree branch: `connector-acceptance-runtime`
- HEAD: `6b641efacb53f551c696d3f070d5f4f2e13e091d`
  `fix(auth): accept clustered HMAC-signed OAuth access tokens`
  (commit date `2026-08-25 19:34:26 -0500`)
- HEAD is identical to the main repo's `fix/v09-release-connector-acceptance` HEAD.

## Pin verification

Compare the service start time against the checkout HEAD commit time:

- service `ExecMainStartTimestamp`: `Sat 2026-09-05 16:56:05 CDT`
- runtime HEAD `%ci`: `2026-08-25 19:34:26 -0500`

The service started **after** the HEAD commit date, so the running server serves the
runtime checkout at HEAD `6b641ef`. Because the service reads `server.py` plus the
imported `operator_*.py` modules from the worktree directory at runtime, it is also
serving the worktree's **uncommitted** working-tree state, which includes local
modifications to `operator_fleet.py` and `test_operator_fleet.py`.

## Uncommitted state in the live worktree (never reset/discard)

- Modified tracked files:
  - `operator_fleet.py` (+421 lines)
  - `test_operator_fleet.py` (409 lines changed; 708 insertions / 122 deletions total)
- Untracked acceptance/test files and backups in the worktree (28 untracked entries),
  including `live_peer_transport_acceptance.py`,
  `live_peer_work_order_acceptance.py`, `operator_fleet.py.bak.*`,
  `test_operator_fleet.py.bak.*`, and non-source artifacts.

These are **local working-tree changes**, not local-only commits. They are not part of
any published branch and must not be reset, discarded, or checked out over.

## Drift from the review premise

The host card that created this note described the live checkout as "branch
`v09-pr56-final` carrying local-only commits." The verified reality is:

- the active worktree branch is `connector-acceptance-runtime` (not `v09-pr56-final`);
- the "local-only" content is **uncommitted working-tree modifications**, not commits.

`remotes/origin/v09-pr56-final` exists as a remote branch, but it is **not** the branch
the live worktree is checked out on. Record the verified facts above before relying on
any stale planning premise.

## Re-verify command

```bash
systemctl --user is-active hermes-gpt-server.service
systemctl --user show hermes-gpt-server.service -p ExecMainStartTimestamp -p WorkingDirectory -p ExecStart
git -C /home/tony/hermes-gpt-runtime rev-parse --git-common-dir
git -C /home/tony/hermes-gpt-runtime branch --show-current
git -C /home/tony/hermes-gpt-runtime log -1 --format='%H %ci %s'
git -C /home/tony/hermes-gpt-runtime status --short
```
