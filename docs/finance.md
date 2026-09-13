# Finance bridge

`hermes_finance_analyze` is a narrowly scoped bridge from a trusted ChatGPT client into the local Hermes `finance` profile. It is designed for financial reasoning over a bounded evidence packet, not for direct bank connectivity or money movement.

## Security model

The financial data plane remains outside Hermes. A trusted caller retrieves financial facts from its authorized source, normalizes only the facts required for the current decision, and submits a `finance.evidence/v1` JSON packet.

Hermes GPT validates that packet before execution. It rejects oversized input, the wrong schema, missing coverage metadata, and credential-like fields or private-key material. The packet must be explicitly marked `financial-confidential`.

The validated packet is sent to the Finance worker through subprocess stdin. It is not placed in the child process argv. The child process is fixed to the local `finance` profile and is launched with `shell=False`.

The Finance worker runs with:

- no Hermes tools;
- no SessionDB;
- Hermes persistence disabled;
- memory disabled for the turn;
- background memory/skill review disabled;
- trajectory saving disabled;
- the `finance` profile SOUL loaded as its identity.

The worker must return a JSON object matching `finance.decision/v1` and the original `request_id`. Malformed, oversized, or mismatched output fails closed.

Hermes GPT audit records contain only bounded metadata such as input length and SHA-256 correlation hashes. The evidence packet, normalized financial facts, and Finance decision body are not written to the Operator audit by this tool.

## Activation

The tool is disabled by default. Enable it only after the local `finance` profile has passed its acceptance checks. Either set the trusted-server environment gate:

```bash
HERMES_GPT_ENABLE_FINANCE=1
```

or create the profile-local marker:

```text
~/.hermes/profiles/finance/.finance-enabled
```

The marker is the preferred persistent local activation because it contains no secret material, survives service restarts/reboots, and stays scoped to the Finance profile. If neither activation is present, `hermes_finance_analyze` is not registered on the MCP server and direct module calls fail closed with `FINANCE_DISABLED`.

The local Hermes installation must contain a `finance` profile with a `SOUL.md`. The bridge uses the Hermes Agent virtual-environment Python when available so the isolated child has the same runtime dependencies as Hermes Agent.

## Evidence contract

A request uses `finance.evidence/v1` and includes:

- `request_id`: opaque bounded identifier;
- `intent`: concise decision or analysis class;
- `as_of`: source-data timestamp;
- `coverage.status`: one of `complete`, `partial`, `recent_only`, `stale`, or `unknown`;
- `facts`: only normalized decision-relevant values;
- `ambiguities`: unresolved classifications or missing evidence;
- `assumptions`: explicit assumptions needed for the model;
- `quality_flags`: source or reconciliation warnings;
- `sensitivity`: exactly `financial-confidential`.

Do not send credentials, account-link tokens, authorization headers, private keys, cookies, raw provider secrets, or unrestricted transaction dumps.

## Decision contract

A successful response uses `finance.decision/v1` and contains a matching `request_id`, a structured verdict, current-state synthesis, options, a recommendation, uncertainties, next actions, approval requirements, and specialist-review flags.

The bridge validates structure. It does not grant Finance authority to pay bills, transfer funds, place trades, alter accounts, or mutate external financial systems.

## Data ownership

Financial facts should remain in the authorized financial system. Hermes should retain methodology and non-sensitive operating rules, not a shadow ledger. Nexus, Work Contracts, A2A payloads, and profile memory should not be used as general stores for balances, transaction histories, account identifiers, or holdings dumps.

The `finance` profile currently requires no Vault service. Do not add banking or financial-institution credentials to Hermes simply to make this bridge work.

## Verification

Run the focused test suite first:

```bash
python -m pytest test_operator_finance.py -q
```

Then run the normal repository test suite before shipping a change that touches the bridge or its registration path.

A production acceptance should also prove:

1. the tool is absent when `HERMES_GPT_ENABLE_FINANCE` is unset;
2. the Finance profile launches successfully;
3. a synthetic evidence packet produces a valid `finance.decision/v1` response;
4. malformed or credential-bearing evidence fails closed;
5. the Operator audit contains hashes/lengths rather than the evidence body;
6. no Finance evidence was written into Hermes session history or profile memory during the isolated call.
