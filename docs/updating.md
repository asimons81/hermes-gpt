# Updating Hermes GPT

`hermes-gpt update` is deliberately check-first. It reports whether an update is available without changing files, packages, environments, or Git history.

For documentation authority rules, see [docs/README.md](README.md).

## Upgrade a package-installed service safely

Current behavior: `hermes-gpt update --apply --version X.Y.Z` upgrades the `hermes-gpt` package inside one existing virtual environment, verifies the import from a neutral directory, optionally restarts one named service, and writes a rollback snapshot. The package path runs only when `--version` is given with `--apply`. Without it, `hermes-gpt update` and `hermes-gpt update --apply` keep their check-first behavior for Git checkouts and pip installs.

```sh
hermes-gpt update --apply --version 0.16.0 \
  --venv /absolute/path/to/venv \
  --rollback-dir /absolute/path/to/new-empty-rollback-dir \
  [--service NAME] [--wheel /absolute/path/to/hermes_gpt-0.16.0-py3-none-any.whl] \
  [--allow-dependency-changes] [--allow-prerelease]
```

- `--version` must be a canonical release (for example `0.16.0`). Prereleases need `--allow-prerelease`.
- `--venv` must be absolute and contain `bin/python` (or `Scripts/python.exe` on Windows).
- `--rollback-dir` must be absolute. It must not exist or must be empty; the updater never overwrites a previous snapshot.
- `--service` is validated against `[A-Za-z0-9_.@-]{1,128}`. On macOS it runs `launchctl kickstart -k gui/<uid>/<service>`; on Linux it runs `systemctl --user restart <service>`. No other service is restarted, and other platforms are refused before any change.
- `--wheel` installs a local `py3-none-any` wheel instead of downloading one from PyPI.

Sequence: validate inputs; read the installed version and `direct_url.json` through the venv's own Python; refuse if the installed version is the target or newer; obtain the target wheel and verify its sha256 against PyPI's digest; compare `Requires-Dist` with the installed metadata; snapshot `pip freeze` and the current wheel; install with `pip install --no-deps --no-cache-dir <wheel>`; run `pip check`; confirm the other packages are unchanged in the freeze; run `python -I -c "import versioning, server; print(versioning.VERSION)"` from a neutral directory; then restart the named service if one was given. Any failure after the install reinstalls the snapshot wheel with `--no-deps --force-reinstall` and reports `rolled_back`.

Refusal codes (nothing is installed): `INVALID_VERSION`, `PRERELEASE_REFUSED`, `ALREADY_AT_TARGET`, `DOWNGRADE_REFUSED`, `VENV_NOT_ABSOLUTE`, `VENV_INVALID`, `ROLLBACK_DIR_NOT_ABSOLUTE`, `ROLLBACK_DIR_NOT_EMPTY`, `SERVICE_NAME_INVALID`, `UNSUPPORTED_PLATFORM`, `HASH_MISMATCH`, `WHEEL_MISMATCH`, `DEPENDENCIES_CHANGED` (the result lists the differences; re-run with `--allow-dependency-changes` only after reviewing them), and `ROLLBACK_ARTIFACT_UNAVAILABLE` (for example a directory or VCS install, where no exact previous wheel exists). Failures after the install report `INSTALL_FAILED`, `PIP_CHECK_FAILED`, `FREEZE_CHANGED`, `IMPORT_VERSION_MISMATCH`, or `RESTART_FAILED`. The command exits non-zero on any refusal or rollback.

What it never touches: launchers, launchd plists, systemd units, environment files, credential or token stores, OAuth grants, network exposure, or any service other than `--service`. Dependency changes are installed only with `--allow-dependency-changes`. A venv shared by several services changes for all of them; use a separate venv when only one service should move. A pre-existing `pip check` failure in the venv also triggers rollback, so fix that first.

Verify afterwards: the updater cannot authenticate to the service, so the result's `next_step` is a manual check. Read `serverInfo.version` through your MCP client after the restart, confirm the expected tool inventory, and refresh the client's cached tool list. The local import check only proves the venv resolves the target version.

Outputs: the JSON result contains `steps`, `applied`, `rolled_back`, and the paths below. Inside `--rollback-dir` the updater writes `RECEIPT.json` (versions, wheel names and sha256 values, steps, timestamps; no environment values or credentials), `freeze-before.txt`, the previous wheel, and `ROLLBACK.md` with the exact rollback commands.

Rollback by hand (after a successful upgrade): run the reinstall and import commands in `ROLLBACK.md`, restart the service, then verify `serverInfo.version` again. If the upgrade changed dependencies, `ROLLBACK.md` also restores the recorded set from `freeze-before.txt`.

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

Agents should determine whether they are operating a checkout or an installed package before describing update availability. Unreleased commits may retain the published version number: the package updater cannot distinguish two source revisions both labeled `0.14.0`. Retain the artifact hash/source commit and verify the running tool inventory; see [runtime provenance](runtime-checkout.md). A manual source/wheel install and service restart are separate operator actions. If multiple services share one environment, review the shared impact or use an isolated environment before upgrading it.

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
