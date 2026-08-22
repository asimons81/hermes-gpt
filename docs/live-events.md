# Live Events and WebSocket stream (v0.9)

Hermes GPT v0.9 adds a durable, bounded live-event bus for clients and parent orchestrators that need completion/wake-up delivery without polling every underlying store.

## Authority model

Live events are notifications, not proof. Mission, Swarm, Work Contract, runner, and Fabric journals remain authoritative. A missing, delayed, duplicated, or reconnected event must never advance work or change an authority decision. Clients use event references to re-read durable state before acting.

## MCP surfaces

- `hermes_live_events_cursor()` returns the current durable high-water cursor.
- `hermes_live_events_since(cursor, mission_id, topic, kind, limit, wait_ms)` returns bounded events after a cursor and may long-poll for up to the configured maximum.

Reads are non-creating when the live-event store does not yet exist.

## WebSocket stream

`/events/ws` provides the same durable stream over WebSocket. Operator mode must be enabled for a connection to be accepted. In unauthenticated loopback deployments, no additional credential is required. In static-bearer deployments, non-browser WebSocket clients must send the same `Authorization: Bearer ...` credential used by Hermes HTTP/MCP before the socket is accepted. OAuth-only deployments fail closed for WebSocket connections because browser WebSocket APIs cannot attach the existing OAuth bearer header; those clients use the authenticated MCP cursor/long-poll surfaces instead.

Supported client control frames are intentionally narrow:

- `ping` — liveness response only;
- `subscribe` — change cursor/topic/kind/mission filters;
- `ack` — advance the client-side acknowledgement cursor.

The WebSocket accepts no Hermes mutation commands.

## Event safety

Events are bounded and redacted before persistence. Secret-like keys, prompt/body/content/transcript fields, credentials, authorization material, cookies, passwords, private keys, and API/access/refresh key fields are replaced with `[REDACTED]`. Oversized payloads are represented by bounded digest metadata rather than raw content.

Event IDs are idempotent. Consumers should persist the returned cursor and resume from it after reconnects.

## Producers

The initial v0.9 producers are first-class Mission lifecycle changes and Swarm operator actions. Producer failure is deliberately non-fatal: publishing a wake-up event cannot roll back or modify the authoritative transaction that produced it.

## Retention

The live-event journal is bounded. `HERMES_GPT_LIVE_EVENT_RETENTION` controls the retained row count within the implementation hard cap. Retention affects notification history only; it never removes the underlying Mission/Swarm/Fabric evidence stores.
