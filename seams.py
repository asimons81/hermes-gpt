"""Cross-machine swarm seam interfaces for hermes-gpt v0.7 (Flight Deck, S8).

Per ADR-008 these are **interfaces only** — no remote implementation exists in
v0.7. ``DispatchAdapter`` and ``EvidenceProvider`` define the contracts a
future cross-machine swarm would compose into; validation is a
two-process-one-host fake in ``test_seams.py`` that exercises both interfaces
over loopback. No remote behavior is promised.

Authority propagation stays in the work-order envelope (existing
``operator_fleet``); this module defines the seam interface only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class WorkOrder:
    """Bounded work-order envelope carrying identity + authority claims."""

    task_id: str
    objective_sha256: str
    assigned_agent: str
    authority_class: str
    allowed_workspaces: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    contract_sha256: str = ""
    parent_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DispatchAdapter(Protocol):
    """Interface for dispatching bounded work to an execution host.

    Local implementation today: in-process dispatch. Future remote
    implementation: A2A fleet work order. v0.7 ships interfaces only.
    """

    def dispatch(self, work_order: WorkOrder) -> str:
        """Dispatch a work order; returns an opaque reference."""
        ...

    def poll(self, ref: str) -> dict[str, Any]:
        """Return bounded status for a dispatch reference."""
        ...

    def collect(self, ref: str) -> dict[str, Any]:
        """Return completion evidence for a dispatch reference."""
        ...


@runtime_checkable
class EvidenceProvider(Protocol):
    """Interface for collecting observed-state evidence for a contract.

    Local implementation today: the host-local ``_check_*`` readers in
    ``operator_contract``. Future remote implementation: a remote
    observed-state collector over fleet result bundles.
    """

    def collect(self, contract_sha256: str, task_id: str, host: str) -> dict[str, Any]:
        """Return bounded observed-state evidence (never raw bodies)."""
        ...
