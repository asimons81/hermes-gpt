# Release-planning artifacts

Files in this directory preserve the release brief, integrated plan, risk reviews, counsel packet, and related pre-release evidence.

## Current v0.7 Flight Deck artifacts

- `v0.7-flight-deck-research-package.md` — grounded research package + release brief (G1).
- `v0.7-flight-deck-risk-register.md` — architecture risk register (G2; input to legal review).
- `v0.7-flight-deck-implementation-plan.md` — implementation plan with independently verifiable slices (G3).
- `v0.7-flight-deck-legal-risk-review.md` — legal/compliance/privacy/security risk review memo with go/no-go gate recommendations (G2; input to implementation and G4/G5).
- `v0.7-flight-deck-implementation-evidence.md` — implementation evidence + JUnit test report for slices S0-S9 (G3; t_d31703e4).
- `v0.7-flight-deck-verification-report.md` — independent verification report, G4 code-level pass with gate conditions (t_8e69d56b).
- `v0.7-flight-deck-defect-list.md` — recorded defects D1-D8 from the independent pass (t_8e69d56b; not yet fixed).
- `v0.7-flight-deck-demo-script.md` — runnable local demo of the v0.7 surfaces (t_8e69d56b).
- `v0.7-flight-deck-trt-handoff.md` — TRT technical editorial source pack with verified claims and guardrails (t_8e69d56b).
- `v0.7-flight-deck-tony-handoff.md` — Tony approval-gate summary (t_8e69d56b).

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
