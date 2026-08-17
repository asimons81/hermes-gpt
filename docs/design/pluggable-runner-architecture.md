# Pluggable execution runners

Status: implementation-backed design note for `feat/pluggable-runners`.

## Decision

Hermes-gpt work contracts separate **work ownership** from **execution transport/runtime**.

- `assigned_agent` and `assigned_profile` describe semantic ownership and authority.
- Optional `execution.backend` selects how the work is executed.
- Contracts without `execution` retain legacy behavior and dispatch through `fleet`.

The runner boundary is implemented by `operator_runners.py` and consumed by `operator_contract.py` and swarm orchestration.

## Runner interface

A runner backend is responsible for:

- availability/capability reporting;
- dry-run planning;
- dispatching one canonical work contract;
- returning bounded status/observed-run state;
- cancellation where the backend supports it.

Runner output is not accepted as proof that a contract is complete. Contract validation continues to use observed state, declared artifacts/tests, review evidence, and authorization checks.

## Built-in backends

### `fleet`

Compatibility backend for the pre-existing A2A/fleet work-order path. This remains the implicit default for contracts that omit `execution`.

### `pi_rpc`

Pi coding agent through its JSONL RPC protocol (`pi --mode rpc`). This is a protocol integration rather than scraping interactive CLI output.

### `omx`

Oh My Codex through its non-interactive `omx exec --json` automation surface.

### `codex`

Compatibility adapter for the existing raw Codex operator runner. It is not required when OMX is the preferred Codex orchestration layer.

## Extension mechanism

External Python packages may register additional backends through the `hermes_gpt.runners` entry-point group. This allows OpenCode or other execution runtimes to be added without changing the contract dispatcher.

## Contract shape

Example:

```json
{
  "schema": "hermes.work-contract/v1",
  "assigned_agent": "implementation",
  "assigned_profile": "default",
  "execution": {
    "backend": "pi_rpc",
    "options": {
      "model": "provider/model"
    }
  }
}
```

The `execution` block is optional. Secret-like option keys (tokens, API keys, credentials, passwords, private keys) are rejected; credentials belong in the runner's trusted environment/configuration, not in contracts.

## Swarm behavior

Workflow stages may select execution backends independently. A workflow can therefore route research, implementation, tests, review, or other stages to different runtimes without changing stage ownership semantics.

Explicit stage execution configuration takes precedence over legacy backend-specific behavior. Existing workflows without execution selectors remain compatible.

## Durable runner state

Local runner state is stored under the Hermes data root in `runner-jobs`. This state is bounded and feeds the observed-run layer used by contract validation.

The invariant is:

> Runner self-report is transport data; Hermes validation decides whether the declared contract is satisfied.

## Deployment and source-of-truth policy

The Git checkout is the source of truth. Runtime `site-packages` must be produced from a tested commit/build, not edited manually as a normal development workflow.

Promotion sequence:

1. edit on a feature branch;
2. run unit/integration tests and lint;
3. commit the exact source revision;
4. install/deploy that revision;
5. restart/reload Hermes-gpt;
6. smoke-test registered runners and a dry-run contract;
7. record the deployed Git revision.

Direct runtime patches are acceptable only as emergency/prototyping measures and must be reconciled back into Git before the next deployment.
