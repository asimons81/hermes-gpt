# Cloudflare Tunnel deployment

This guide covers the **public HTTPS proxy** deployment path. If the only remote client is a supported OpenAI product and you want Hermes GPT to remain strictly loopback/private with no public inbound hostname, prefer [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md) when it is available for the target account or workspace.

Hermes GPT keeps MCP DNS-rebinding protection enabled. When exposing the HTTP server through a locally managed Cloudflare Tunnel, configure both the proxy and the MCP server so legitimate public requests cannot be rejected with `421 Invalid Host header`.

## Cloudflare ingress

For a server listening on `localhost:4750`, prefer an origin Host override:

```yaml
ingress:
  - hostname: gpt.example.com
    service: http://localhost:4750
    originRequest:
      httpHostHeader: localhost
```

Validate the config before restarting the tunnel:

```bash
cloudflared tunnel --config ~/.cloudflared/config.yml ingress validate
```

## Hermes GPT public-host allowlist

As defense in depth, declare the legitimate public hostname to Hermes GPT:

```text
HERMES_GPT_ALLOWED_HOSTS=gpt.example.com
```

Multiple hosts may be comma-separated. This extends the existing MCP transport-security allowlist; it does not disable DNS-rebinding protection. Loopback hosts remain allowed automatically.

This public-host allowlist is specific to public proxy traffic. Do not add a public Host entry merely because you use OpenAI Secure MCP Tunnel; the Secure MCP Tunnel path targets the loopback MCP URL locally.

For a systemd user service, add the environment variable to the Hermes GPT server unit, then run:

```bash
systemctl --user daemon-reload
systemctl --user restart hermes-gpt-server.service
```

## Verification

A public MCP request should reach the server without a `421` response. If `Host: localhost` succeeds directly at the origin while the public hostname returns `421`, inspect both the Cloudflare origin Host override and `HERMES_GPT_ALLOWED_HOSTS`.

Do not put tunnel credentials, bearer credentials, or other secrets in unit files, documentation, logs, or prompts.
