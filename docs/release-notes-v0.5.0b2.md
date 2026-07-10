# Hermes GPT v0.5.0b2

This maintenance beta adds safe, easy Hermes GPT updates and aligns the project documentation.

- `hermes-gpt update` checks for updates without changing anything.
- `hermes-gpt update --apply` fast-forwards a clean default-branch checkout or upgrades an installed package only when a newer version exists.
- The updater refuses tracked local changes, non-default branches, divergent Git history, and package downgrades.
- Documentation now links the Codex, Operator Mode, and update workflows consistently.

See [updating.md](updating.md) for details.
