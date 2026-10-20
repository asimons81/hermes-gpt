"""Regression: plan-node execution selector survives canonicalization.

P0 correction, second half. PR #92 taught _build_contract to honor
``node["execution"]`` — but _canonical_node silently DROPPED the key, so
no plan could ever carry a selector to contract build (the M3 attempt-3
fleet-routed hollow completion). These pins hold:

1. a valid execution block canonicalizes onto the node verbatim;
2. an invalid selector is rejected at plan canonicalization (fail-closed);
3. an absent selector keeps the legacy shape — and the contract signature
   is BYTE-IDENTICAL with and without a selector (pre-existing plan hashes
   stay stable; the selector routes execution, it does not define the work).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import operator_mission_plan as mp  # noqa: E402


def _raw_node(**over):
    node = {
        "node_id": "fix-boot-lock",
        "kind": "single",
        "owner": "default",
        "parents": [],
        "objective": "implement the fix",
        "capability_req": {"profile": "default", "skills": [], "authorization_class": "reversible_write"},
        "budget": {"est_minutes": 30, "est_tokens": 50000},
        "expected_artifacts": ["receipt.json"],
    }
    node.update(over)
    return node


def test_execution_selector_canonicalizes_onto_node():
    node = mp._canonical_node(_raw_node(
        execution={"backend": "hermes_agent", "options": {"delegate": "pi", "sandbox": "workspace-write"}},
    ))
    assert node["execution"]["backend"] == "hermes_agent"
    assert node["execution"]["options"]["delegate"] == "pi"


def test_execution_selector_direct_backend_also_carried():
    node = mp._canonical_node(_raw_node(
        execution={"backend": "pi_rpc", "options": {"sandbox": "workspace-write"}},
    ))
    assert node["execution"]["backend"] == "pi_rpc"


def test_invalid_execution_selector_rejected():
    with pytest.raises(ValueError, match="execution invalid"):
        mp._canonical_node(_raw_node(execution={"backend": "bad backend!", "options": {}}))
    with pytest.raises(ValueError, match="execution must be an object"):
        mp._canonical_node(_raw_node(execution="pi_rpc"))


def test_absent_selector_keeps_legacy_shape():
    node = mp._canonical_node(_raw_node())
    assert "execution" not in node


def test_contract_signature_stable_with_selector():
    """The selector must not perturb the node contract signature.

    Pre-existing plans (selector-less) keep identical signatures; a selector
    routes execution only.
    """
    base = mp._canonical_node(_raw_node())
    pinned = mp._canonical_node(_raw_node(
        execution={"backend": "hermes_agent", "options": {"delegate": "pi"}},
    ))
    assert base["contract_sha256"] == pinned["contract_sha256"]
