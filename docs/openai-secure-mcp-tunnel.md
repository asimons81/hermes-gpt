# OpenAI Secure MCP Tunnel

Status: current operational guide.

OpenAI Secure MCP Tunnel is the preferred private connection path for using a loopback-bound Hermes GPT server from supported OpenAI products when Secure MCP Tunnel is available for the target account or workspace.

It keeps Hermes GPT off the public internet. `tunnel-client` runs on the same trusted machine or network as Hermes GPT, opens an outbound HTTPS connection to OpenAI, forwards queued MCP requests to the local Streamable HTTP endpoint, and returns responses through the same tunnel.

Official OpenAI documentation:

- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- https://github.com/openai/tunnel-client

## Architecture

```text
ChatGPT / Codex / supported OpenAI surface
                  |
                  v
       OpenAI-hosted tunnel endpoint
                  ^
                  | outbound HTTPS
                  |
            tunnel-client
                  |
                  | local Streamable HTTP
                  v
         Hermes GPT on loopback
       http://127.0.0.1:4750/mcp
```

Hermes GPT remains bound to `127.0.0.1`. You do not need to publish a public hostname, open an inbound firewall port, or add a public hostname to `HERMES_GPT_ALLOWED_HOSTS` for this path.

Secure MCP Tunnel is a transport path. It does not change Hermes GPT Operator or Owner authorization. Tool visibility, mutation gates, allowed paths, direct mode, confirmation, and Owner Mode remain independent.

Secure MCP Tunnel is for private connections and developer-mode testing. It is not a public plugin distribution endpoint.

## Prerequisites

You need:

- Hermes GPT installed or checked out locally;
- Streamable HTTP available on loopback;
- a tunnel created in OpenAI Platform tunnel settings;
- a tunnel runtime API key for `tunnel-client`;
- Tunnels Read + Use permission to run the client or select the tunnel;
- Tunnels Read + Manage permission only when creating or editing tunnel metadata;
- the target ChatGPT workspace or Platform organization associated with the tunnel when that OpenAI surface needs to discover it;
- ChatGPT developer-mode access when connecting the tunnel as a ChatGPT developer-mode app.

OpenAI tunnel permissions and ChatGPT developer-mode permissions are separate. Do not assume access to one grants access to the other.

## 1. Keep Hermes GPT on loopback

Start Hermes GPT with Streamable HTTP on a loopback address:

```powershell
python server.py --http --host 127.0.0.1 --port 4750
```

The local MCP endpoint is:

```text
http://127.0.0.1:4750/mcp
```

Do not switch Hermes GPT to a wildcard or public bind merely to use Secure MCP Tunnel.

If Operator Mode is enabled, keep its authority as narrow as the deployment requires. In particular, leave Owner Mode off for an always-on connector unless there is a deliberate break-glass reason to activate it.

## 2. Create the tunnel and associate the right contexts

Create or manage the tunnel in OpenAI Platform tunnel settings. Record the `tunnel_id` without committing it into this repository.

Associate every OpenAI context that should be able to find or use the tunnel:

- the Platform organization that owns or manages it;
- the ChatGPT workspace that should list it when creating an app;
- any additional Platform organization that will use it from Codex, the Responses API, or another supported surface.

A tunnel associated only with one Platform organization does not automatically appear in an unrelated ChatGPT workspace.

## 3. Install tunnel-client

Use the download supplied by OpenAI Platform tunnel settings or the latest public release from the official `openai/tunnel-client` repository.

Do not pin documentation or automation to a historical release URL. Start by inspecting the installed binary:

```powershell
tunnel-client.exe help quickstart
```

## 4. Configure the runtime API key outside the repository

`tunnel-client` uses a tunnel runtime API key for its long-lived connection to the OpenAI tunnel control plane. This is separate from an OpenAI admin key and separate from Hermes GPT credentials.

For an interactive PowerShell session:

```powershell
$env:CONTROL_PLANE_API_KEY = '<runtime-api-key>'
```

For an always-on deployment, load the value from the service or scheduled-task secret environment. Do not place it in Git, a launcher script, a command-line argument, a prompt, or logs.

Use an OpenAI admin key only for administrative tunnel CRUD workflows that explicitly require one. Do not use an admin key as the long-lived tunnel daemon credential.

## 5. Create a named Hermes GPT profile

Discover the built-in samples before creating a profile:

```powershell
tunnel-client.exe profiles samples list
```

For the normal loopback/no-auth local hop, create a named HTTP profile with the current no-auth HTTP sample:

```powershell
tunnel-client.exe init `
  --sample sample_mcp_remote_no_auth `
  --profile hermes-gpt `
  --tunnel-id tunnel_0123456789abcdef0123456789abcdef `
  --mcp-server-url http://127.0.0.1:4750/mcp
```

Use your real tunnel ID. The example value is intentionally fake.

Named profiles keep runtime configuration out of repeated command lines and can be inspected or edited with `tunnel-client` profile commands. Keep secret values as environment or file references instead of literal YAML values.

## 6. Validate before starting the long-lived client

With Hermes GPT already listening on `127.0.0.1:4750`, run:

```powershell
tunnel-client.exe doctor --profile hermes-gpt --explain
```

Do not continue until `doctor` succeeds. It is the canonical first diagnostic for profile, control-plane, and local MCP reachability problems.

Then start the long-lived tunnel client:

```powershell
tunnel-client.exe run --profile hermes-gpt
```

Keep this process running. App discovery and MCP tool calls depend on the client being connected and polling.

`tunnel-client` exposes local health and operator surfaces including `/healthz`, `/readyz`, `/metrics`, and `/ui`. The admin UI is loopback-only by default and should stay that way unless an operator network is intentionally allowed to reach it.

## 7. Connect from ChatGPT

In ChatGPT developer mode, create a developer-mode app and choose **Tunnel** as the connection type. Select the associated tunnel when it appears, or provide the valid tunnel ID where the product surface allows it.

If the tunnel does not appear:

1. verify the tunnel is associated with the target ChatGPT workspace;
2. verify the app creator has Tunnels Read + Use;
3. verify ChatGPT developer-mode access is enabled for that workspace/user;
4. confirm `tunnel-client run --profile hermes-gpt` is still running;
5. rerun `tunnel-client doctor --profile hermes-gpt --explain`.

## Optional local-hop bearer defense in depth

For many private developer deployments, the OpenAI tunnel identity plus Hermes GPT's own Operator policy is the intended baseline and Hermes GPT can remain no-auth on the loopback hop.

If you want a second credential on the local MCP hop, Hermes GPT already supports a static bearer token. Configure Hermes GPT with a strong secret:

```powershell
$env:HERMES_GPT_BEARER_TOKEN = '<strong-random-token>'
```

Then derive an Authorization header value for the tunnel process without writing the token into the profile:

```powershell
$env:HERMES_GPT_TUNNEL_AUTHORIZATION = "Bearer $env:HERMES_GPT_BEARER_TOKEN"
```

Edit the named tunnel profile and add an MCP runtime header that references the environment variable:

```yaml
mcp:
  extra_headers:
    Authorization: env:HERMES_GPT_TUNNEL_AUTHORIZATION
```

`tunnel-client` supports `env:` and `file:` references for secret-bearing MCP header values. Prefer those references over literal secrets in YAML.

The bearer token authenticates the local MCP request. It does not activate Hermes GPT mutation, direct mode, or Owner Mode.

## OAuth caveat

Hermes GPT also ships a built-in confidential-client OAuth authorization-code flow. Do not make that the default Secure MCP Tunnel recipe.

OpenAI Secure MCP Tunnel can carry OAuth discovery metadata for the MCP server, but the browser-facing authorization server itself is not automatically tunneled. Hermes GPT's OAuth issuer and authorization/token endpoints therefore still need to be reachable by the parties participating in the OAuth flow.

Use Hermes GPT OAuth with Secure MCP Tunnel only when you deliberately provide that authorization-server reachability and understand the additional trust boundary. For a tunnel-only private deployment, use the baseline loopback path or the optional static bearer defense in depth described above.

See [OAuth and bearer authentication](oauth.md) for the Hermes GPT OAuth contract.

## Windows supervised launcher

The repository ships [`../examples/start-openai-secure-mcp-tunnel.example.ps1`](../examples/start-openai-secure-mcp-tunnel.example.ps1).

The example:

- starts only the configured Hermes GPT process;
- verifies that the new process owns the expected loopback listener;
- runs `tunnel-client doctor --profile ... --explain` before the daemon;
- starts `tunnel-client run --profile ...`;
- watches both child processes;
- stops only the child process it owns if the other side exits;
- never searches for or kills arbitrary `python.exe` processes;
- never embeds tunnel or Hermes credentials.

Copy the example to a private local path before customizing machine-specific executable paths.

For Task Scheduler, point the task at your private copy of the supervised launcher. Store credentials in the scheduled task's service environment or another appropriate secret source, not in the checked-in example.

## Cloudflare Tunnel vs OpenAI Secure MCP Tunnel

Use OpenAI Secure MCP Tunnel when a supported OpenAI product needs private access and you want Hermes GPT to remain strictly loopback-bound with no public inbound hostname.

Use the existing [Cloudflare Tunnel guide](cloudflare-tunnel.md) when you intentionally need a public HTTPS proxy hostname in front of Hermes GPT. That path has a different Host-header and authentication boundary and may require `HERMES_GPT_ALLOWED_HOSTS`.

Do not combine the two merely because both are called tunnels. Pick the boundary that matches the client and deployment.

## Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `doctor` cannot reach Hermes GPT | Hermes GPT is not listening on the configured local URL | Verify the `127.0.0.1:4750` listener and the profile MCP URL. |
| Tunnel is not visible in ChatGPT | Missing workspace association or permission | Check the target ChatGPT workspace association and Tunnels Read + Use. |
| Tool discovery worked, then calls fail | `tunnel-client` stopped or lost readiness | Keep `run` alive, inspect `/readyz` or `/ui`, and rerun `doctor --explain`. |
| Control-plane requests fail | Runtime key, egress, proxy, CA, or mTLS problem | Verify `CONTROL_PLANE_API_KEY`, outbound HTTPS, and any enterprise proxy/CA/mTLS configuration. |
| Local MCP returns unauthorized | Optional bearer header is missing/mismatched | Verify Hermes GPT's bearer token and the profile's secret-backed `Authorization` header. |
| OAuth discovery works but browser auth fails | Authorization server is not reachable | Provide deliberate authorization-server reachability or use the tunnel baseline/static bearer path instead. |
| Public hostname returns `421` | You are using the public-proxy path, not the private tunnel path | Follow [Cloudflare Tunnel deployment](cloudflare-tunnel.md) and configure the public Host boundary there. |

## Security checklist

- Keep Hermes GPT on `127.0.0.1`.
- Do not add a public Host allowlist entry for Secure MCP Tunnel alone.
- Keep the tunnel runtime API key out of Git, argv, prompts, and logs.
- Keep tunnel profile secrets as `env:` or `file:` references.
- Keep Operator Mode authority narrow.
- Keep Owner Mode off for normal always-on access.
- Remember that tunnel transport authentication and Hermes authorization are separate layers.
- Keep `tunnel-client` health/admin surfaces loopback-only unless there is a deliberate operator-network requirement.
- Use `doctor --explain` before treating the connection as healthy.
- Use the latest OpenAI tunnel documentation as the source of truth when the external client changes.
