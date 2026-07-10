# Updating Hermes GPT

`hermes-gpt update` is deliberately check-first. It reports the current and available revision or package version without changing files, environments, or Git history.

```powershell
hermes-gpt update
```

When the report shows an update is available, apply it explicitly:

```powershell
hermes-gpt update --apply
```

## Git checkout updates

When Hermes GPT runs from a Git checkout, the updater:

1. Refuses to act if tracked files have local changes.
2. Refuses to update from a feature branch; it operates only on the remote default branch.
3. Checks the remote branch without modifying the checkout.
4. On `--apply`, fetches the default branch and runs `git merge --ff-only`.

It never creates a merge commit, rebases, stashes changes, force-resets, or deletes untracked files. Untracked files are left alone. If a checkout has diverged, the command stops and tells you to resolve it manually.

## Installed-package updates

When Hermes GPT is installed from PyPI rather than a Git checkout, the updater checks the latest package version through pip. `--apply` runs the current Python environment's pip with `install --upgrade hermes-gpt` only if the available version is newer.

It never downgrades a package. Add `--pre` only when you want pip to consider prerelease packages:

```powershell
hermes-gpt update --pre
hermes-gpt update --pre --apply
```

Restart any running Hermes GPT server or Codex MCP process after a successful installed-package update.

## Troubleshooting

- `WORKTREE_DIRTY`: commit, stash, or otherwise resolve tracked changes first. The updater intentionally ignores untracked files but will not update over tracked edits.
- `NOT_ON_DEFAULT_BRANCH`: switch to the default branch before applying an update.
- `FAST_FORWARD_REQUIRED`: the checkout has diverged. Resolve the history manually; the updater will not merge or rebase it.
- `UPDATE_CHECK_FAILED`: verify network/PyPI access and retry. No update was applied.
