# Updating Hermes GPT

`hermes-gpt update` is deliberately check-first. It reports whether an update is available without changing files, packages, environments, or Git history.

For documentation authority rules, see [docs/README.md](README.md).

## Check first

```powershell
hermes-gpt update
```

Apply only after reviewing the result:

```powershell
hermes-gpt update --apply
```

## Identify the installation type first

Hermes GPT has two update paths:

- a Git checkout checks the repository remote and default branch;
- an installed package checks PyPI through pip.

GitHub releases and PyPI are separate distribution channels and can temporarily report different current versions. A newer GitHub release does not mean an installed-package update will be available before that version is published to PyPI.

Agents should determine whether they are operating a checkout or an installed package before describing update availability.

## Git checkout updates

For a Git checkout, the updater:

1. refuses to apply over tracked local changes;
2. refuses to apply from a feature branch;
3. checks the remote default branch without changing the working tree;
4. on `--apply`, fetches the default branch and uses fast-forward-only merge behavior.

It does not create merge commits, rebase, stash tracked edits, force-reset the checkout, downgrade, or delete untracked files.

If local history has diverged, the updater stops for manual resolution.

```powershell
hermes-gpt update
hermes-gpt update --apply
```

Restart a running Hermes GPT server after a successful checkout update.

## Installed-package updates

For an installed package, the updater asks pip for the latest available `hermes-gpt` version.

`--apply` upgrades only when the available package version is newer.

```powershell
hermes-gpt update
hermes-gpt update --apply
```

It does not intentionally downgrade the package.

To include prerelease packages in the pip check:

```powershell
hermes-gpt update --pre
hermes-gpt update --pre --apply
```

Restart any running Hermes GPT server or Codex MCP process after a successful package update.

## What the updater does not synchronize

The updater does not automatically synchronize:

- GitHub release publication with PyPI publication;
- feature branches with the default branch;
- tracked local edits with upstream changes;
- cached MCP schemas in external clients;
- already-running server processes after files or packages change.

After an update that changes the MCP surface, restart Hermes GPT and reconnect or recreate clients that cached the previous tool schema.

## Troubleshooting

| Code or symptom | Meaning | Action |
| --- | --- | --- |
| `WORKTREE_DIRTY` | tracked files have local changes | resolve the tracked changes before applying |
| `NOT_ON_DEFAULT_BRANCH` | checkout is on a feature branch | switch to the remote default branch before applying |
| `FAST_FORWARD_REQUIRED` | local and remote history diverged | resolve history manually; updater will not merge or rebase it |
| `UPDATE_CHECK_FAILED` | remote or PyPI check failed | verify network and channel access; no update was applied |
| GitHub has a newer release but package updater reports no update | the newer version is not available from PyPI yet | use the intended source/GitHub distribution or wait for the package publication |
| client still shows old tools after updating | the running process or client schema is stale | restart Hermes GPT, then refresh the client connector |

## Safety invariant

A failed or refused update should leave the existing checkout or package usable. Keep the clean-tree, default-branch, and fast-forward requirements intact.
