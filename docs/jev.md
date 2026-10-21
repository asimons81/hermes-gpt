# Jev structured decisions

Status: merged, unreleased source after v0.14.0; default off. Installing the published v0.14.0 release does not establish that this bridge is present. Verify the installed artifact/source revision and authenticated tool inventory. Deployment and live acceptance are specific to each installation; this guide is not a deployment receipt.

The optional bridge exposes TypeSafe Jev decisions through the main Hermes GPT MCP server. It reuses the separate community [jev-mcp adapter](https://github.com/asimons81/jev-mcp), pinned to `fc4d4bef26af92d1d5a3b9e05bffb46d90a37eae`; it does not implement another TypeSafe API client or change the Hermes Agent gateway.

| Hermes GPT tool | Adapter tool | Result |
|---|---|---|
| `hermes_jev_yes_no` | `jev_yes_no` | Probability of yes, 0–1 |
| `hermes_jev_choose` | `jev_choose` | Label, distribution and confidence |
| `hermes_jev_score` | `jev_score` | Weighted position on ordered levels, distribution, legend and confidence |

Every tool accepts `state` and `instructions` as text. Choice requires a `criteria` mapping of 2–32 labels to descriptions; Score requires 2–32 ordered descriptions. Yes/no optionally accepts `criteria` with `true` and `false` descriptions. See the adapter's [safe examples](https://github.com/asimons81/jev-mcp/blob/fc4d4bef26af92d1d5a3b9e05bffb46d90a37eae/examples/decisions.json).

Jev judges the supplied evidence; it does not search, chat, execute actions, approve work, or grant authority. Calls send the supplied text to TypeSafe and may incur API charges. Do not send credentials or sensitive operational records. The bridge never gathers session history, memory, files, or gateway state automatically.

## Owner setup prerequisites

These are operator setup steps; the bridge does not install or configure itself. Do not enable the gate or restart a deployment until the owner is ready.

1. Verify the actual Hermes GPT runtime's executable, package/check-out provenance and authenticated MCP version. An unchanged `0.14.0` version string or a source checkout does not establish the running deployment; see [runtime provenance](runtime-checkout.md).
2. Install Node.js 22+ on that runtime host. In a dedicated operator-managed directory, check out the exact adapter revision and install its locked dependencies:

   ```sh
   git clone --config core.autocrlf=false https://github.com/asimons81/jev-mcp.git
   cd jev-mcp
   git checkout --detach fc4d4bef26af92d1d5a3b9e05bffb46d90a37eae
   npm ci --ignore-scripts
   npm test
   ```

   Preserve the exact Git source bytes: automatic LF-to-CRLF checkout conversion changes the pinned hashes, even when JavaScript behavior is equivalent. The clone setting above is scoped to this new adapter checkout; it does not change global Git settings. For an existing managed checkout, restore the pinned index bytes before installation with `git -c core.autocrlf=false -c core.eol=lf checkout-index --all --force` only after preserving any local changes. Do not relax the hash verification to accommodate converted or modified files.

3. Configure the following in the service's private environment/credential launcher, with absolute paths. Never place a key in MCP arguments, source control, logs, a client header, or a command-line argument:

   ```text
   HERMES_GPT_JEV_ADAPTER_ROOT=/absolute/path/to/jev-mcp
   HERMES_GPT_JEV_NODE=/absolute/path/to/node
   TYPESAFE_API_KEY=<supplied privately by the owner>
   TYPESAFE_MODEL=jev-latest
   HERMES_GPT_ENABLE_JEV=1
   ```

   `TYPESAFE_MODEL` is optional. All other settings are required for successful calls. The gate is enabled only by the exact value `1`. Paths are administrator configuration, never tool arguments. The bridge checks the pinned hashes of all adapter runtime sources, `package.json` and `package-lock.json`, plus direct dependency versions, before spawning. Keep the adapter, its installed dependency tree and the Node executable protected from untrusted writes. Changing the pin requires a reviewed code update; there is no automatic update/download path.

4. If other services share the Python environment, use an isolated installation for a service-specific update; preserve the existing launcher/auth configuration for rollback. Install the reviewed Hermes GPT build, restart only the intended Hermes GPT process after owner approval, then refresh the client's tool list. The separate curated Codex toolset is unchanged; Codex may continue using `jev-mcp` directly.
5. Verify the three additional tools and make synthetic calls such as `{"state":"The parcel arrived.","instructions":"Did the parcel arrive?"}` to `hermes_jev_yes_no`. A successful call returns bounded `model`, `answer` and `usage` data. Check missing-key, invalid-key and disabled-gate failures without printing a key. A standalone `jev-mcp` smoke test proves only that adapter/key path. Claim Hermes GPT live acceptance only after authenticated discovery and all three wrapper calls succeed through the intended service and transport.

## Authentication and authority

The outer connection keeps the existing [Hermes GPT transport and authentication](mcp-compatibility.md): local stdio, or authenticated HTTP/private tunnel as configured by the operator. New tools advertise `server.tool_meta()` exactly like existing tools, including the configured OAuth scope or bearer scheme. No additional HTTP listener, credential authority, OAuth grant, Host allowlist entry, or network permission is introduced.

The inner hop is local MCP stdio with an absolute Node executable and the fixed pinned `src/cli.js` entry point. Each request owns a short-lived child. Only the TypeSafe key/model and a PATH limited to the Node directory are supplied; Windows also receives SystemRoot when needed. Parent Hermes/OAuth/cloud credentials, HOME, NODE_OPTIONS and NODE_PATH are not forwarded. Some operating systems may inject their own harmless runtime variables.

No Operator, direct-mode, confirmation or Owner gate is enabled or bypassed. Tool annotations are descriptive hints, not authorization. Jev answers must never be treated as approval, Work Contract acceptance evidence, or a reason to relax existing permissions.

## Bounds and failure behavior

The bridge admits only the three fixed adapter tool names. State is limited to 16,000 characters, instructions and each criterion to 4,000, and the JSON input to 128 KiB. At most four requests per process may own children. The entire initialization/call has a 25-second deadline; the adapter has its own 20-second upstream deadline. Cancellation and errors close stdin, terminate/kill if necessary, and reap only the owned child. Cleanup adds a short bounded grace interval under normal OS process termination.

The inner wire has a 512 KiB aggregate byte ceiling, including duplicate text/structured results; the returned decision is limited to 256 KiB. The pinned adapter owns semantic validation of Jev answers. Child stderr and arbitrary exception details are discarded. Recognized adapter error codes are re-emitted with fixed bridge messages; unknown errors become `UNAVAILABLE`. No raw request/result/credential content is persisted by this bridge. There are no automatic retries, daemons, or services.

Removing or clearing `HERMES_GPT_ENABLE_JEV` hides the tools after rebuilding/restarting the server; already-registered callbacks also recheck the gate before spawning. This does not cancel an already-dispatched provider request. Stop the intended Hermes GPT process if an active request must be interrupted.

## Offline verification

`python -m pytest test_jev_bridge.py` covers gates, allowlists, environment confinement, adapter pin rejection, safe errors, subprocess cleanup and existing bearer/OAuth boundaries. Real Python-to-Node interoperability runs with a mocked TypeSafe `fetch`, no real key and no upstream network request. Set `HERMES_GPT_TEST_JEV_ADAPTER_ROOT` to the pinned checkout with installed dependencies; this variable is used only by tests. Otherwise tests discover an adjacent `jev-mcp` checkout or explicitly skip the optional Node checks. CI installs the exact pin to exercise them with both supported Python MCP SDK families.
