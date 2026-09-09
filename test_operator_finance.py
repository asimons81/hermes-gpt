from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import finance_worker
import operator_finance as finance


def evidence(**overrides):
    payload = {
        "schema": "finance.evidence/v1",
        "request_id": "req-123",
        "intent": "affordability decision",
        "as_of": "2026-08-23T16:00:00-05:00",
        "coverage": {
            "status": "complete",
            "requested_period": "current",
            "covered_period": "current",
            "missing_domains": [],
        },
        "facts": {"liquid_cash": 1000, "planned_purchase": 250},
        "ambiguities": [],
        "assumptions": [],
        "quality_flags": [],
        "sensitivity": "financial-confidential",
    }
    payload.update(overrides)
    return json.dumps(payload)


def decision(request_id="req-123"):
    return {
        "schema": "finance.decision/v1",
        "request_id": request_id,
        "verdict": {"summary": "Do not buy yet.", "confidence": "high"},
        "current_state": "The purchase would reduce liquidity materially.",
        "options": [],
        "recommendation": {"preferred_option": "wait", "rationale": "Preserve liquidity."},
        "uncertainties": [],
        "next_actions": ["Reassess after the next expected inflow."],
        "approval_required": [],
        "specialist_review": {
            "legal": False,
            "tax": False,
            "investment": False,
            "growth": False,
            "outreach": False,
            "developer": False,
            "qa": False,
        },
    }


def runtime(tmp_path: Path, monkeypatch):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "finance"
    profile.mkdir(parents=True)
    (profile / "SOUL.md").write_text("Finance", encoding="utf-8")
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    monkeypatch.setattr(finance.op, "normalize_hermes_data_root", lambda value: Path(value))
    return root, agent_root, profile


def enable(monkeypatch):
    monkeypatch.setenv(finance.ENABLE_FINANCE_ENV, "1")


def test_gate_off_refuses_before_subprocess(monkeypatch):
    monkeypatch.delenv(finance.ENABLE_FINANCE_ENV, raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(finance.subprocess, "run", boom)
    result = json.loads(finance.hermes_finance_analyze(evidence()))
    assert result["success"] is False
    assert result["code"] == "FINANCE_DISABLED"


def test_profile_marker_enables_without_environment(tmp_path, monkeypatch):
    monkeypatch.delenv(finance.ENABLE_FINANCE_ENV, raising=False)
    root, agent_root, profile = runtime(tmp_path, monkeypatch)
    (profile / finance.FINANCE_ENABLE_MARKER).write_text("enabled\n", encoding="utf-8")

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(decision()), stderr="")

    monkeypatch.setattr(finance.subprocess, "run", fake_run)
    monkeypatch.setattr(finance.op, "audit_record", lambda **kwargs: None)
    result = json.loads(
        finance.hermes_finance_analyze(
            evidence(),
            hermes_root=root,
            agent_root=agent_root,
        )
    )
    assert result["schema"] == finance.DECISION_SCHEMA


def test_invalid_schema_rejected(monkeypatch):
    enable(monkeypatch)
    payload = json.loads(evidence())
    payload["schema"] = "finance.evidence/v999"
    result = json.loads(finance.hermes_finance_analyze(json.dumps(payload)))
    assert result["success"] is False
    assert result["code"] == "INVALID_EVIDENCE_SCHEMA"


def test_recursive_credential_like_field_rejected(monkeypatch):
    enable(monkeypatch)
    payload = json.loads(evidence())
    payload["facts"] = {"nested": {"api_key": "do-not-pass"}}
    result = json.loads(finance.hermes_finance_analyze(json.dumps(payload)))
    assert result["success"] is False
    assert result["code"] == "SENSITIVE_MATERIAL_REJECTED"
    assert "do-not-pass" not in json.dumps(result)


def test_oversized_input_rejected(monkeypatch):
    enable(monkeypatch)
    result = json.loads(finance.hermes_finance_analyze("x" * (finance.MAX_EVIDENCE_CHARS + 1)))
    assert result["success"] is False
    assert result["code"] == "EVIDENCE_TOO_LARGE"


def test_fixed_argv_stdin_shell_false_and_finance_profile(tmp_path, monkeypatch):
    enable(monkeypatch)
    root, agent_root, profile = runtime(tmp_path, monkeypatch)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(decision()), stderr="")

    monkeypatch.setattr(finance.subprocess, "run", fake_run)
    monkeypatch.setattr(finance.op, "audit_record", lambda **kwargs: None)
    result = json.loads(
        finance.hermes_finance_analyze(
            evidence(), hermes_root=root, agent_root=agent_root
        )
    )
    assert result["schema"] == "finance.decision/v1"
    assert seen["shell"] is False
    assert seen["text"] is True
    assert seen["capture_output"] is True
    assert seen["cwd"] == str(profile)
    assert seen["env"]["HERMES_PROFILE"] == "finance"
    assert seen["env"]["HERMES_HOME"] == str(profile)
    assert "req-123" not in " ".join(seen["argv"])
    assert "liquid_cash" not in " ".join(seen["argv"])
    assert json.loads(seen["input"])["request_id"] == "req-123"


def test_malformed_child_output_fails_closed(tmp_path, monkeypatch):
    enable(monkeypatch)
    root, agent_root, _ = runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        finance.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="```json\n{}\n```", stderr=""),
    )
    result = json.loads(finance.hermes_finance_analyze(evidence(), hermes_root=root, agent_root=agent_root))
    assert result["success"] is False
    assert result["code"] == "INVALID_FINANCE_RESULT"


def test_request_id_mismatch_fails_closed(tmp_path, monkeypatch):
    enable(monkeypatch)
    root, agent_root, _ = runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        finance.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(decision("other")), stderr=""),
    )
    result = json.loads(finance.hermes_finance_analyze(evidence(), hermes_root=root, agent_root=agent_root))
    assert result["success"] is False
    assert result["code"] == "FINANCE_REQUEST_MISMATCH"


def test_audit_contains_hashes_not_raw_evidence(tmp_path, monkeypatch):
    enable(monkeypatch)
    root, agent_root, _ = runtime(tmp_path, monkeypatch)
    payload = json.loads(evidence())
    payload["facts"]["merchant_note"] = "private-finance-marker-9371"
    raw = json.dumps(payload)
    monkeypatch.setattr(
        finance.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(decision()), stderr=""),
    )

    class Policy:
        level = "owner"
        apply_mode = "direct"

    captured = {}
    monkeypatch.setattr(finance.op, "OperatorPolicy", Policy)
    monkeypatch.setattr(finance.op, "audit_record", lambda **kwargs: captured.update(kwargs))
    result = json.loads(finance.hermes_finance_analyze(raw, hermes_root=root, agent_root=agent_root))
    assert result["schema"] == "finance.decision/v1"
    audit_text = json.dumps(captured, sort_keys=True)
    assert "private-finance-marker-9371" not in audit_text
    assert "merchant_note" not in audit_text
    assert captured["extra"]["evidence_sha256"]
    assert captured["extra"]["evidence_chars"] > 0


def test_worker_constructs_persistence_disabled_tool_free_agent(tmp_path, monkeypatch):
    agent_root = tmp_path / "agent"
    profile_home = tmp_path / "profile"
    agent_root.mkdir()
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", "original-home")
    monkeypatch.setenv("HERMES_PROFILE", "original-profile")

    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config = lambda: {"model": {"default": "model-x", "provider": "provider-x"}}
    fake_fallback = types.ModuleType("hermes_cli.fallback_config")
    fake_fallback.get_fallback_chain = lambda cfg: []
    fake_runtime = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime.resolve_runtime_provider = lambda requested, target_model: {
        "api_key": "runtime-only",
        "base_url": "https://example.invalid",
        "provider": requested,
        "requested_provider": requested,
        "api_mode": "chat_completions",
        "credential_pool": None,
    }

    observed = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            observed["kwargs"] = kwargs
            self._persist_disabled = False
            self._session_db = "unexpected"
            self._owns_session_db = True
            self.suppress_status_output = False
            self.stream_delta_callback = object()
            self.tool_gen_callback = object()

        def run_conversation(self, prompt):
            observed["persist_disabled_during_run"] = self._persist_disabled
            observed["session_db_during_run"] = self._session_db
            observed["prompt"] = prompt
            return {"final_response": json.dumps(decision())}

        def close(self):
            observed["closed"] = True

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    monkeypatch.setitem(sys.modules, "hermes_cli.fallback_config", fake_fallback)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime)
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    response = finance_worker.run(str(agent_root), str(profile_home), evidence())
    assert json.loads(response)["schema"] == "finance.decision/v1"
    kwargs = observed["kwargs"]
    assert kwargs["enabled_toolsets"] == []
    assert kwargs["session_db"] is None
    assert kwargs["skip_memory"] is True
    assert kwargs["skip_background_review"] is True
    assert kwargs["skip_context_files"] is True
    assert kwargs["load_soul_identity"] is True
    assert kwargs["save_trajectories"] is False
    assert observed["persist_disabled_during_run"] is True
    assert observed["session_db_during_run"] is None
    assert observed["closed"] is True
