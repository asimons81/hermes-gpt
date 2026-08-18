"""Tests for v0.7 S8: cross-machine seam interfaces (interfaces only).

Validates the ``DispatchAdapter`` / ``EvidenceProvider`` protocols with a
two-process-one-host fake over loopback. No remote implementation exists or is
claimed.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

import seams

HOST = "127.0.0.1"


def test_work_order_envelope_carries_authority_claims():
    wo = seams.WorkOrder(
        task_id="t_seam_1",
        objective_sha256="a" * 64,
        assigned_agent="worker-a",
        authority_class="reversible_write",
        allowed_workspaces=("ws1",),
        forbidden_actions=("publish",),
    )
    assert wo.authority_class == "reversible_write"
    assert "publish" in wo.forbidden_actions
    # No raw objective text on the envelope.
    assert not hasattr(wo, "objective")


class LoopbackDispatch:
    """One-host fake DispatchAdapter: accepts orders, returns bounded status."""

    def __init__(self) -> None:
        self._refs: dict[str, dict] = {}

    def dispatch(self, work_order: seams.WorkOrder) -> str:
        ref = f"ref-{len(self._refs) + 1}"
        self._refs[ref] = {"work_order": work_order, "status": "dispatched"}
        return ref

    def poll(self, ref: str) -> dict[str, Any]:
        item = self._refs.get(ref)
        if not item:
            return {"status": "unknown", "error": "no such ref"}
        return {"status": item["status"], "ref": ref}

    def collect(self, ref: str) -> dict[str, Any]:
        item = self._refs.get(ref)
        if not item:
            return {"status": "unknown", "error": "no such ref"}
        return {
            "status": "completed",
            "ref": ref,
            "task_id": item["work_order"].task_id,
            "evidence": [{"kind": "artifact", "basename": "report.md"}],
        }


class LoopbackEvidence:
    """One-host fake EvidenceProvider: host-local observed state."""

    def __init__(self, host: str) -> None:
        self.host = host

    def collect(self, contract_sha256: str, task_id: str, host: str) -> dict[str, Any]:
        return {
            "host": host,
            "contract_sha256": contract_sha256,
            "task_id": task_id,
            "run_state": {"terminal": True, "outcome_ok": ["completed"]},
            "review_satisfied": True,
        }


def test_dispatch_adapter_protocol_is_satisfied_by_local_impl():
    adapter = LoopbackDispatch()
    assert isinstance(adapter, seams.DispatchAdapter)


def test_evidence_provider_protocol_is_satisfied_by_local_impl():
    provider = LoopbackEvidence(HOST)
    assert isinstance(provider, seams.EvidenceProvider)


def test_local_dispatch_poll_collect_cycle():
    adapter = LoopbackDispatch()
    wo = seams.WorkOrder(
        task_id="t_seam_2",
        objective_sha256="b" * 64,
        assigned_agent="worker-a",
        authority_class="read_only",
    )
    ref = adapter.dispatch(wo)
    assert adapter.poll(ref)["status"] == "dispatched"
    collected = adapter.collect(ref)
    assert collected["status"] == "completed"
    assert collected["task_id"] == "t_seam_2"


def _tcp_pair():
    """Return (listener_socket, connected_socket) on loopback."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((HOST, 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    server_sock: list[socket.socket | None] = [None]

    def accept() -> None:
        conn, _ = listener.accept()
        server_sock[0] = conn

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    client = socket.create_connection((HOST, port), timeout=5)
    thread.join(timeout=5)
    assert server_sock[0] is not None
    return listener, server_sock[0], client


def test_two_process_one_host_fake_over_loopback():
    """Process A listens and serves evidence; process B dispatches over loopback.

    Simulates the cross-machine contract with two processes on one host,
    exercising the seam interfaces without any remote execution. Process A
    owns the listener (like a future remote evidence host); process B is the
    client that would drive a DispatchAdapter.
    """
    repo = Path(__file__).resolve().parent

    # Process A: a loopback server implementing the EvidenceProvider contract.
    provider_code = (
        "import json, socketserver, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "class H(socketserver.StreamRequestHandler):\n"
        "    def handle(self):\n"
        "        line = self.rfile.readline().decode().strip()\n"
        "        req = json.loads(line)\n"
        "        out = {'contract_sha256': req.get('contract_sha256',''), "
        "'task_id': req.get('task_id',''), 'host': 'process-a', "
        "'review_satisfied': True}\n"
        "        self.wfile.write((json.dumps(out) + '\\n').encode())\n"
        "socketserver.ThreadingTCPServer.allow_reuse_address = True\n"
        "srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), H)\n"
        "print(srv.server_address[1], flush=True)\n"
        "srv.serve_forever()\n"
    )
    server_proc = subprocess.Popen(
        [sys.executable, "-c", provider_code, str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert server_proc.stdout is not None
        port_line = server_proc.stdout.readline().strip()
        port = int(port_line)

        # Process B: dispatches a work-order-like request over loopback.
        with socket.create_connection((HOST, port), timeout=10) as client:
            client.sendall(
                (json.dumps({"contract_sha256": "c" * 64, "task_id": "t_seam_3", "host": "process-b"}) + "\n").encode()
            )
            response_line = client.recv(4096).decode().strip()
        response = json.loads(response_line)
        assert response["contract_sha256"] == "c" * 64
        assert response["task_id"] == "t_seam_3"
        assert response["host"] == "process-a"
        assert response["review_satisfied"] is True
    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
