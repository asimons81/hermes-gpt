"""Property-based invariants for the native execution rails (TEST-01 tier).

Complements the example-based suites (test_operator_runners.py,
test_autopilot_execution_selector.py, test_operator_mission_budget*.py)
with generator-driven properties over the mapping surfaces the migration
added: the plan-node execution selector mapping into work contracts, the
backend-agnostic ``normalize_execution`` schema, and the budget envelope
admission arithmetic. Runs under plain pytest + hypothesis; no network,
no live services, no writes outside the pytest sandbox.

Property families (TEST-01 acceptance):
  P1 selector mapping    — every well-formed node selector reaches the
                           contract verbatim-and-canonical; absent selectors
                           never create an execution key (hash stability).
  P2 adapter schema      — normalize_execution is total on valid inputs
                           (idempotent, canonical, bounded) and fail-closed
                           on invalid ones (raises, never half-normalized).
  P3 budget admission    — envelope arithmetic partitions exactly at
                           spend==quota and is monotone in spend.
  P4 signature determinism — _node_contract_signature is stable under key
                           order and sensitive to objective content.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import operator_autopilot as autopilot
import operator_mission_budget as budget
import operator_mission_plan as mission_plan
import operator_runners as runners

_REQUIREMENT = {"profile": "default", "authorization_class": "reversible_write"}


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

_BACKENDS = st.sampled_from(["pi_rpc", "opencode", "omx", "fleet", "a", "z9-_"])

_OPTION_KEYS = st.sampled_from(
    ["sandbox", "tools", "profile", "model", "cwd", "retries", "extra_opt"]
)
_OPTION_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**31), max_value=2**31),
    st.text(min_size=0, max_size=64),
)

valid_options = st.dictionaries(keys=_OPTION_KEYS, values=_OPTION_VALUES, max_size=8)

valid_selectors = st.fixed_dictionaries(
    {"backend": _BACKENDS, "options": valid_options}
)

valid_backend_names = st.from_regex(r"^[a-z][a-z0-9_-]{0,63}$", fullmatch=True)

_objectives = st.text(min_size=1, max_size=120)
_artifact_names = st.sampled_from(["receipt.json", "report.md", "out.txt"])


def _build(node: dict) -> dict:
    return autopilot._build_contract(
        "m1", node, requirement=_REQUIREMENT, agent="pi",
        key="k" * 64, attempt=1, hermes_root=None,
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# P1 — selector mapping into work contracts
# --------------------------------------------------------------------------


@settings(max_examples=75, deadline=None)
@given(selector=valid_selectors, objective=_objectives, artifacts=st.lists(_artifact_names, max_size=3))
def test_prop_selector_reaches_contract_canonical(selector, objective, artifacts):
    node = {
        "node_id": "n1",
        "kind": "single",
        "objective": objective,
        "expected_artifacts": artifacts,
        "execution": selector,
    }
    contract = _build(node)
    got = contract.get("execution")
    assert got is not None, "selector dropped from contract"
    assert got["backend"] == selector["backend"].strip().lower()
    assert got["options"] == selector["options"]
    assert runners.normalize_execution(got) == got  # canonical form


@settings(max_examples=40, deadline=None)
@given(objective=_objectives)
def test_prop_absent_selector_never_creates_execution_key(objective):
    node = {"node_id": "n1", "kind": "single", "objective": objective}
    contract = _build(node)
    assert "execution" not in contract


# --------------------------------------------------------------------------
# P2 — adapter schema: normalize_execution
# --------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(selector=valid_selectors)
def test_prop_normalize_execution_idempotent_and_canonical(selector):
    once = runners.normalize_execution(selector)
    assert runners.normalize_execution(once) == once
    assert once["backend"] == selector["backend"].strip().lower()
    assert set(once) == {"backend", "options"}
    enc = json.dumps(once["options"], sort_keys=True, separators=(",", ":"))
    assert len(enc.encode()) <= runners._MAX_OPTIONS_BYTES


@settings(max_examples=60, deadline=None)
@given(name=valid_backend_names)
def test_prop_backend_regex_accepts_wellformed_names(name):
    assert runners.normalize_execution({"backend": name, "options": {}}) == {
        "backend": name,
        "options": {},
    }


def _is_valid_backend(s: str) -> bool:
    return bool(runners._BACKEND_RE.fullmatch(s.strip().lower()))


@settings(max_examples=60, deadline=None)
@given(
    bad=st.one_of(
        st.text(min_size=1, max_size=20).filter(lambda s: not _is_valid_backend(s)),
        st.integers().filter(lambda n: not _is_valid_backend(str(n))),
    )
)
def test_prop_bad_backend_always_raises(bad):
    with pytest.raises((ValueError, TypeError)):
        runners.normalize_execution({"backend": bad, "options": {}})


@settings(max_examples=60, deadline=None)
@given(
    secret_key=st.sampled_from(
        ["api_key", "my-secret", "password", "token", "credentials", "private_key", "API-KEY"]
    ),
    value=st.one_of(st.text(max_size=16), st.integers()),
)
def test_prop_secretish_option_keys_rejected(secret_key, value):
    with pytest.raises(ValueError):
        runners.normalize_execution({"backend": "pi_rpc", "options": {secret_key: value}})


# --------------------------------------------------------------------------
# P3 — budget admission arithmetic
# --------------------------------------------------------------------------

positive_finite = st.floats(
    min_value=1e-9, max_value=1e12, allow_nan=False, allow_infinity=False
)


@settings(max_examples=150, deadline=None)
@given(
    spend=st.floats(0, 1e12, allow_nan=False, allow_infinity=False),
    quota=positive_finite,
)
def test_prop_envelope_partitions_at_quota(spend, quota):
    env = budget._envelope_status(spend, quota, "usd")
    crosses = spend >= quota
    assert env["crosses_envelope"] == crosses
    assert env["within_envelope"] == (not crosses)
    assert (env["status"] == budget.STATUS_CROSSING) == crosses
    assert (env["status"] == budget.STATUS_WITHIN) == (not crosses)
    if not crosses:
        assert env["remaining"] == pytest.approx(quota - spend, abs=1e-6)


@settings(max_examples=100, deadline=None)
@given(quota=positive_finite, extra=st.floats(1e-9, 1e9, allow_nan=False, allow_infinity=False))
def test_prop_envelope_monotone_in_spend(quota, extra):
    s = quota * 0.5
    below = budget._envelope_status(s, quota, "usd")
    above = budget._envelope_status(s + extra, quota, "usd")
    assert above["consumed"] >= below["consumed"] - 1e-9
    if below["crosses_envelope"]:
        assert above["crosses_envelope"]


@settings(max_examples=40, deadline=None)
@given(quota=st.one_of(st.just(0), st.just(-1.0), st.floats(max_value=0, allow_nan=False)))
def test_prop_nonpositive_quota_is_invalid(quota):
    env = budget._envelope_status(10.0, quota, "usd")
    assert env["status"] == budget.STATUS_INVALID
    assert env["within_envelope"] is False


def _policy(hard: bool, pause: bool) -> dict:
    return {
        "schema": budget.POLICY_SCHEMA,
        "unit": "usd",
        "hard_block_enabled": hard,
        "pause_on_cross": pause,
        "breaker_signal": "budget_breaker",
        "authorization_class": "reversible_write",
    }


@settings(max_examples=60, deadline=None)
@given(
    spend=st.floats(0, 1e9, allow_nan=False, allow_infinity=False),
    quota=positive_finite,
    hard=st.booleans(),
    pause=st.booleans(),
)
def test_prop_would_block_only_when_crossing(spend, quota, hard, pause):
    env = budget._envelope_status(spend, quota, "usd")
    decision = budget._would_block(env, _policy(hard, pause))
    assert decision["would_pause"] == (env["crosses_envelope"] and hard and pause)
    assert decision["enforced_now"] is False
    assert decision["enforcement_phase"] == "phase_4_5"


# --------------------------------------------------------------------------
# P4 — contract signature determinism
# --------------------------------------------------------------------------


def _sig_node(objective_sha: str) -> dict:
    return {
        "node_id": "n1",
        "kind": "single",
        "owner": "autopilot",
        "parents": [],
        "objective_sha256": objective_sha,
        "capability_req": {},
        "budget": {"max_cost_usd": 5},
        "expected_artifacts": ["receipt.json"],
    }


@settings(max_examples=40, deadline=None)
@given(objective=_objectives)
def test_prop_signature_stable_under_key_order(objective):
    base = _sig_node(_sha(objective))
    reordered = dict(reversed(list(base.items())))
    assert mission_plan._node_contract_signature(base) == mission_plan._node_contract_signature(
        reordered
    )


@settings(max_examples=60, deadline=None)
@given(o1=_objectives, o2=_objectives)
def test_prop_signature_sensitive_to_objective(o1, o2):
    s1 = mission_plan._node_contract_signature(_sig_node(_sha(o1)))
    s2 = mission_plan._node_contract_signature(_sig_node(_sha(o2)))
    assert (s1 == s2) == (_sha(o1) == _sha(o2))
