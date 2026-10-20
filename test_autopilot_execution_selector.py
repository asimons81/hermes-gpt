"""Regression: plan-node execution selector must reach the work contract.

Defect (2026-10-20, M3 campaign voided twice): ``_build_contract`` dropped
the node's ``execution`` block, so runner-pinned missions (pi_rpc dev
campaigns) silently executed on the default fleet peer and self-satisfied
in <1s with trivial artifacts. The selector must survive into the contract;
an absent selector must keep the legacy shape (no execution key) so
pre-existing contract hashes stay stable.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import operator_autopilot as autopilot


def test_node_execution_selector_reaches_contract():
    node = {
        "node_id": "n1", "kind": "single",
        "objective": "do real dev work",
        "expected_artifacts": ["receipt.json"],
        "execution": {"backend": "pi_rpc", "options": {"sandbox": "workspace-write"}},
    }
    requirement = {"profile": "default", "authorization_class": "reversible_write"}
    contract = autopilot._build_contract("m1", node, requirement=requirement,
                                         agent="pi", key="k" * 64, attempt=1,
                                         hermes_root=None)
    assert contract.get("execution", {}).get("backend") == "pi_rpc"
    assert contract["execution"]["options"]["sandbox"] == "workspace-write"


def test_absent_selector_keeps_legacy_shape():
    node = {"node_id": "n1", "kind": "single", "objective": "x"}
    requirement = {"profile": "default", "authorization_class": "reversible_write"}
    contract = autopilot._build_contract("m1", node, requirement=requirement,
                                         agent="fleet", key="k" * 64, attempt=1,
                                         hermes_root=None)
    assert "execution" not in contract


def test_invalid_selector_fails_closed():
    node = {"node_id": "n1", "kind": "single", "objective": "x",
            "execution": {"backend": "bad backend!", "options": {}}}
    requirement = {"profile": "default", "authorization_class": "reversible_write"}
    with pytest.raises(ValueError):
        autopilot._build_contract("m1", node, requirement=requirement,
                                  agent="pi", key="k" * 64, attempt=1,
                                  hermes_root=None)
