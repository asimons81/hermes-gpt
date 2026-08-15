# Design artifacts

Files in this directory preserve technical design work produced before or during implementation.

## Current v0.7 Flight Deck artifacts

- `v0.7-flight-deck-architecture.md` — v0.7 architecture and technical design (current design for the v0.7 release cycle; pending G2 review).
- `v0.7-flight-deck-adrs.md` — architecture decision records for v0.7 (D1–D4 and supporting decisions).
- `v0.7-flight-deck-interaction-design.md` — Flight Deck interaction and visual design: user flows (launch/monitor, event history, evidence review, authority), wireframes, visual direction, interaction specs, and reusable asset list (kanban t_dd366ab6).
- `v0.7-flight-deck-wireframes.html` — self-contained wireframe gallery for the Flight Deck views.
- `assets/v0.7-flight-deck-wireframes/*.svg` — individual Flight Deck wireframes.

These are **design authority, not runtime authority**: verify any implementation
claim against the current module and its tests before acting.

## Historical v0.6 artifacts

The following preserve v0.6.0 design work. They are **historical design
authority, not current runtime authority**.

When a design statement differs from the current implementation or tests, use the implementation/tests as the source of truth and update current operational documentation if needed.

Agents should read [../README.md](../README.md) before using these files.

Use these documents for:

- architecture rationale;
- intended invariants;
- threat/risk assumptions that informed implementation;
- understanding why a current behavior exists.

Do not use them alone to determine:

- current release status;
- current distribution-channel availability;
- exact current tool schemas;
- exact current environment defaults;
- whether a pre-release gate is still pending.

Before turning design text into a code or documentation change, verify the relevant implementation module and tests.
