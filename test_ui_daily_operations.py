"""Daily operations preserve read gates, binding, Owner authority and recovery."""
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import operator_policy as op
import ui_ops
from test_operator_owner_inbox import (
    CONTRACT_OBJECTIVE,
    REVIEW_TASK,
    _review_delegation,
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / 'hermes'
    root.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, '1')
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, 'workspace')
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, 'direct')
    monkeypatch.delenv('HERMES_GPT_MISSION_ALLOWED_SURFACES', raising=False)
    op.set_audit_log_override(tmp_path / 'audit.jsonl')
    yield root, TestClient(Starlette(routes=ui_ops.ui_ops_routes()))
    op.set_audit_log_override(None)


def review(env, tmp_path, monkeypatch):
    root, client = env
    delegation = _review_delegation(root, tmp_path, monkeypatch)
    items = client.get('/api/ops/owner-inbox').json()['data']['items']
    item = next(i for i in items if i['kind'] == 'contract_review_pending')
    got = client.get('/api/ops/pending-review/' + item['item_id'])
    assert got.status_code == 200
    return delegation, got.json()['data']['item']


def request(client, item, **updates):
    args = dict(item_id=item['item_id'], review_revision=item['review_revision'],
                reviewer='reviewer-two', verdict='SATISFIED', evidence_refs=['artifact:checked'], confirm=True)
    args.update(updates)
    return client.post('/api/ops/action', json={'tool': 'hermes_review_accept', 'args': args, 'dry_run': False})


def owner(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, 'owner')
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, '1')
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)


def test_review_structured_binding_and_owner_gate(env, tmp_path, monkeypatch):
    delegation, item = review(env, tmp_path, monkeypatch)
    root, client = env
    assert item['contract_sha256'] == delegation['contract_sha256']
    assert item['task_id'] == REVIEW_TASK
    assert CONTRACT_OBJECTIVE not in json.dumps(item)
    assert str(tmp_path) not in json.dumps(item)
    assert request(client, item).status_code == 403
    owner(monkeypatch)
    applied = request(client, item)
    assert applied.status_code == 200, applied.text
    assert applied.json()['data']['dry_run'] is False
    assert client.get('/api/ops/pending-review/' + item['item_id']).status_code == 404
    assert request(client, item).status_code == 409
    # Acceptance is separate from reconciliation and final Mission approval.
    import operator_delegations as delegations
    current = json.loads(delegations.hermes_delegation_get(item['delegation_id'], hermes_root=root))
    assert current['delegation']['state'] == 'reconciling'


@pytest.mark.parametrize('change,code', [
    ({'review_revision': 'stale'}, 'REVIEW_STALE'),
    ({'task_id': 'wrong-task'}, 'REVIEW_BINDING_MISMATCH'),
    ({'contract_sha256': 'a' * 64}, 'REVIEW_BINDING_MISMATCH'),
    ({'reviewer': 'coder'}, 'REVIEW_SELF_REVIEW'),
    ({'reviewer': 'default'}, 'REVIEW_SELF_REVIEW'),
])
def test_review_refuses_stale_mismatched_or_executing_identity(env, tmp_path, monkeypatch, change, code):
    _, item = review(env, tmp_path, monkeypatch)
    owner(monkeypatch)
    response = request(env[1], item, **change)
    assert response.json()['error']['code'] == code
    assert env[1].get('/api/ops/pending-review/' + item['item_id']).status_code == 200


@pytest.mark.parametrize('path', ['/api/ops/delivery-status', '/api/ops/launch-receipts/launch-0123456789abcdef', '/api/ops/pending-review/' + 'a' * 24])
def test_new_reads_deny_unavailable_authority_and_allowlist(env, monkeypatch, path):
    _, client = env
    monkeypatch.setenv('HERMES_GPT_MISSION_ALLOWED_SURFACES', '')
    assert client.get(path).status_code == 403
    monkeypatch.delenv('HERMES_GPT_MISSION_ALLOWED_SURFACES')
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, '0')
    assert client.get(path).status_code == 403
    assert client.post(path, json={}).status_code == 405


def test_launch_lookup_and_delivery_read_create_no_work(env, monkeypatch):
    root, client = env
    monkeypatch.setenv('HERMES_GPT_ATTENTION_DELIVERY', '0')
    assert client.get('/api/ops/launch-receipts/launch-0123456789abcdef').json()['data']['found'] is False
    status = client.get('/api/ops/delivery-status').json()['data']
    assert status['enabled'] is False and status['agent_compatibility'] == 'not_probed'
    assert not list(root.rglob('*'))
    resp = client.post('/api/ops/action', json={'tool': 'hermes_mission_launch', 'args': {'spec_json': '{}'}, 'dry_run': True})
    assert resp.json()['error']['code'] == 'INVALID_REQUEST_ID'


def test_browser_launch_payload_preserves_existing_gates(env, monkeypatch):
    import operator_autopilot as autopilot
    root, client = env
    args = {'request_id': 'browser-launch-0123456789',
            'spec_json': json.dumps({'title': 'Test browser Mission', 'objective': 'Synthetic work', 'owner_profile': 'default', 'acceptance_criteria': ['Reviewed'], 'final_approval_required': True}),
            'budget_json': json.dumps({'quota': 20}),
            'autopilot_limits_json': json.dumps({'max_concurrency': 1, 'max_attempts_per_node': 1, 'max_replans': 0, 'max_runtime_seconds': 1800})}
    def action(dry):
        return client.post('/api/ops/action', json={'tool': 'hermes_mission_launch', 'args': {**args, 'confirm': not dry}, 'dry_run': dry})
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, '0')
    assert action(True).status_code == 400
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, '1')
    calls = []
    class FakeProc:
        pid = 4242
    monkeypatch.setattr(autopilot.subprocess, 'Popen', lambda *a, **k: calls.append(a) or FakeProc())
    assert action(True).status_code == 200
    assert not calls
    applied = action(False)
    assert applied.status_code == 200, applied.text
    receipt = client.get('/api/ops/launch-receipts/' + args['request_id']).json()['data']
    assert receipt['receipt']['state'] == 'completed' and len(calls) == 1
    assert action(False).status_code == 200 and len(calls) == 1
