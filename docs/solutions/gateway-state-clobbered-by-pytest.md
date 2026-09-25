---
title: "gateway_state.json clobbered by a pytest run (dead PID, false 'overall: fail')"
category: gateway-health
date_discovered: 2026-08-31
date_resolved: 2026-08-31
status: resolved
related_commits: []
---

# `gateway_state.json` clobbered by a pytest run

## Symptom

Health checks report **`overall: fail`** even though the gateway is alive and
healthy. The cause: `gateway_state.json` (under the active profile home, e.g.
`~/.hermes/gateway_state.json`) contains a **dead PID** — a pytest run wrote
its own process context into the live gateway state path, and health reporting
then treats the stale PID as the gateway's.

## Root cause

A test execution wrote to the **live** gateway state path instead of a
sandboxed/profile-isolated one, clobbering the real gateway's PID entry.

## Detection

Compare the PID recorded in `gateway_state.json` against the actual gateway
process (`ps`). A mismatched or dead PID in the file while the gateway is
running means the file was clobbered.

## Fix procedure

Repair the file directly: write the **real gateway PID** (found via `ps`) back
into `gateway_state.json`.

**Do not restart the gateway for this.** The gateway is fine; only the state
file is wrong.

## Prevention

Tests must never write the live gateway state path. Test runs must target a
sandboxed or isolated profile home, not the profile home of the running
gateway.
