# Release-planning artifacts

Files in this directory preserve the release brief, integrated plan, risk reviews, counsel packet, and related pre-release evidence.

## Current v0.7 Flight Deck artifacts

- `v0.7-flight-deck-research-package.md` — grounded research package + release brief (G1).
- `v0.7-flight-deck-risk-register.md` — architecture risk register (G2; input to legal review).
- `v0.7-flight-deck-implementation-plan.md` — implementation plan with independently verifiable slices (G3).

These are **current release-program artifacts** for the v0.7.0 Flight Deck
release. Status statements such as "DRAFT", "pending approval", or "candidate"
record the state at the time the artifact was written; they are not runtime
instructions.

## Historical v0.6 artifacts

The following preserve the v0.6.0 brief, integrated plan, risk reviews, counsel packet, and related pre-release evidence.

They are retained for provenance. They are **not current release-status or runtime instructions**.

Some files intentionally contain language such as:

- candidate;
- gate pending;
- before release;
- approval required;
- do not publish yet.

That language records the state at the time the artifact was written. The final v0.6.0 GitHub release occurred later.

Agents should read [../README.md](../README.md) before using these files.

Use these artifacts for:

- reconstructing release rationale and approval history;
- understanding identified risks and mitigations;
- reviewing the intended surface inventory;
- tracing how v0.6 requirements were decomposed.

Do not use them alone to determine:

- whether v0.6.0 is currently released;
- whether PyPI has a particular version;
- current tool names or schemas;
- current environment defaults;
- current retention enforcement;
- whether a historical release gate remains open.

Verify current claims against implementation/tests, current operational docs, and the active distribution channel.
