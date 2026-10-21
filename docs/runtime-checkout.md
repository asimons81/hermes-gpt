# Runtime deployment provenance

A source checkout, a published package, and a running service are separate states. This guide does not identify a particular live host or assert that a service has been updated. Keep host-specific commands, credentials and acceptance receipts in private deployment records.

## Verify before changing a service

1. Identify the intended service, effective executable, entry script and working directory through the host service manager. Inspect only these fields; do not dump environment maps, process environments or credential files.
2. Resolve the interpreter and package location. Use that interpreter's package metadata, not whichever `hermes-gpt` happens to be first on the interactive shell's PATH.
3. For a source install, record the exact commit and any tracked changes. For a wheel install, retain its source commit, artifact hash and installation receipt. Check that imports resolve to the intended package rather than a stale checkout on `PYTHONPATH`.
4. Verify the authenticated MCP handshake, current tool inventory and required feature gates. `serverInfo.version` identifies the package version; it does not distinguish unreleased commits that share that version or prove that optional tools are enabled.
5. Record acceptance through the intended client/transport. A successful standalone adapter test, package install or health response does not establish that a new Hermes tool works through that service.

Safe metadata commands for an installed environment:

```sh
/absolute/path/to/venv/bin/python -m pip show hermes-gpt mcp
/absolute/path/to/venv/bin/python -c "import sys, server; print(sys.executable); print(server.__file__)"
```

These commands inspect local installation metadata; they do not test a running service or start a listener. Run them from a neutral directory so an adjacent `server.py` does not shadow the installed package.

## Update and rollback scope

Before updating, preserve the intended service's launcher/configuration and record its previous artifact or source revision. Review shared interpreters and dependencies: if multiple services use one environment, upgrading it changes all of them. Use a separate environment when an update must affect only one service, while preserving existing authentication, Agent dependency selection and Python compatibility.

Install the reviewed artifact, restart only the intended service, verify authenticated discovery and bounded acceptance, then refresh the client's cached tool list. Restore the previous launcher/artifact if acceptance fails. Never change network exposure, OAuth grants or authority gates as an incidental update step. See [updating](updating.md), [authentication](oauth.md) and the [Jev setup checklist](jev.md).

Preserve unrelated source changes and untracked operational files. Do not reset a historical runtime checkout merely because a newer service uses a package. Earlier deployment snapshots are historical evidence, not a current runtime pin.
