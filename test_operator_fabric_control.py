from __future__ import annotations

import json

import operator_fabric as fabric
import operator_fabric_control as control


class Policy:
    level = "workspace"
    apply_mode = "direct"

    def require_level(self, _required):
        return None

    def require_mutation(self, _dry_run):
        return None

    def effective_dry_run(self, dry_run):
        return bool(dry_run)


class Coordinator:
    def __init__(self):
        self.calls = []

    def poll(self, attempt_id, *, reconcile, timeout):
        self.calls.append(("poll", attempt_id, reconcile, timeout))
        return {
            "success": True,
            "backend": "fabric",
            "node": "node-a",
            "dispatch_id": "fabd-" + "1" * 32,
            "attempt_id": attempt_id,
            "task_id": "task-a",
            "state": "TERMINAL_REPORTED",
            "peer_state": "SUCCEEDED",
        }

    def collect(self, attempt_id, *, timeout):
        self.calls.append(("collect", attempt_id, timeout))
        return {
            "success": True,
            "backend": "fabric",
            "node": "node-a",
            "dispatch_id": "fabd-" + "1" * 32,
            "attempt_id": attempt_id,
            "state": "COMPLETED",
            "evidence": {"schema": fabric.EVIDENCE_SCHEMA},
        }

    def cancel(self, attempt_id, *, timeout):
        self.calls.append(("cancel", attempt_id, timeout))
        return {
            "success": True,
            "changed": True,
            "backend": "fabric",
            "node": "node-a",
            "dispatch_id": "fabd-" + "1" * 32,
            "attempt_id": attempt_id,
            "task_id": "task-a",
            "state": "CANCELLED",
        }


def test_reconcile_emits_recovery_and_terminal_audit(monkeypatch):
    monkeypatch.setattr(control.op, "OperatorPolicy", Policy)
    coordinator = Coordinator()
    audit = []
    payload = json.loads(
        control.hermes_fabric_status(
            "faba-" + "a" * 32,
            reconcile=True,
            coordinator=coordinator,
            audit=lambda **kwargs: audit.append(kwargs),
        )
    )
    assert payload["state"] == "TERMINAL_REPORTED"
    assert [entry["tool"] for entry in audit] == [
        "hermes_fabric_reconcile",
        "hermes_fabric_terminal",
    ]
    assert coordinator.calls == [("poll", "faba-" + "a" * 32, True, 15)]


def test_evidence_receipt_emits_evidence_and_terminal_audit(monkeypatch):
    monkeypatch.setattr(control.op, "OperatorPolicy", Policy)
    coordinator = Coordinator()
    audit = []
    payload = json.loads(
        control.hermes_fabric_evidence(
            "faba-" + "b" * 32,
            coordinator=coordinator,
            audit=lambda **kwargs: audit.append(kwargs),
        )
    )
    assert payload["state"] == "COMPLETED"
    assert [entry["tool"] for entry in audit] == [
        "hermes_fabric_evidence_receipt",
        "hermes_fabric_terminal",
    ]


def test_cancel_is_dry_run_first_and_attempt_specific(monkeypatch):
    monkeypatch.setattr(control.op, "OperatorPolicy", Policy)
    coordinator = Coordinator()
    audit = []
    attempt = "faba-" + "c" * 32
    plan = json.loads(
        control.hermes_fabric_cancel(
            attempt,
            dry_run=True,
            coordinator=coordinator,
            audit=lambda **kwargs: audit.append(kwargs),
        )
    )
    assert plan["dry_run"] is True
    assert coordinator.calls == []

    result = json.loads(
        control.hermes_fabric_cancel(
            attempt,
            confirm=True,
            dry_run=False,
            coordinator=coordinator,
            audit=lambda **kwargs: audit.append(kwargs),
        )
    )
    assert result["state"] == "CANCELLED"
    assert coordinator.calls == [("cancel", attempt, 15)]
    assert audit[-2]["tool"] == "hermes_fabric_cancel"
    assert audit[-1]["tool"] == "hermes_fabric_terminal"


def test_invalid_attempt_id_fails_closed_without_coordinator_call(monkeypatch):
    monkeypatch.setattr(control.op, "OperatorPolicy", Policy)
    coordinator = Coordinator()
    payload = json.loads(
        control.hermes_fabric_status(
            "not-an-attempt",
            coordinator=coordinator,
            audit=lambda **_kwargs: None,
        )
    )
    assert payload["success"] is False
    assert payload["code"] == "FABRIC_STATUS_ERROR"
    assert coordinator.calls == []


def test_fabric_error_code_is_preserved(monkeypatch):
    monkeypatch.setattr(control.op, "OperatorPolicy", Policy)

    class Broken(Coordinator):
        def poll(self, attempt_id, *, reconcile, timeout):
            raise fabric.FabricError("FABRIC_PEER_JOURNAL_MISSING", "peer journal missing")

    payload = json.loads(
        control.hermes_fabric_status(
            "faba-" + "d" * 32,
            reconcile=True,
            coordinator=Broken(),
            audit=lambda **_kwargs: None,
        )
    )
    assert payload["success"] is False
    assert payload["code"] == "FABRIC_PEER_JOURNAL_MISSING"
