import json
from pathlib import Path

import operator_codex as oc


def enable(monkeypatch, root: Path, *, write: bool = False):
    monkeypatch.setenv("HERMES_GPT_OPERATOR_ENABLED", "1")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_LEVEL", "workspace")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_APPLY_MODE", "direct")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_ALLOWED_PATHS", str(root))
    monkeypatch.setenv(oc.ENABLE_CODEX_RUNNER_ENV, "1")
    if write:
        monkeypatch.setenv(oc.ALLOW_CODEX_WRITE_ENV, "1")


def test_status_and_dry_run_plan(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    assert oc.hermes_codex_status(tmp_path)["enabled"] is True
    plan = oc.hermes_codex_plan("inspect tests", str(tmp_path))
    assert plan["success"] is True and plan["dry_run"] is True
    assert "inspect tests" not in json.dumps(plan)
    assert plan["argv"][-1] == "<prompt>"


def test_gates_and_fixed_arguments(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    assert oc.hermes_codex_start("change", str(tmp_path), sandbox="workspace-write")["code"] == "WRITE_DISABLED"
    assert oc.hermes_codex_plan("x", str(tmp_path), sandbox="danger-full-access")["code"] == "INVALID_SANDBOX"
    assert oc.hermes_codex_plan("x", str(tmp_path.parent / "outside"))["code"] == "POLICY_REFUSED"


def test_result_redacts_and_bounds(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    job_id = "a" * 32
    oc._save({"job_id": job_id, "status": "completed", "return_code": 0, "workdir": str(tmp_path), "sandbox": "read-only"}, tmp_path)
    _, output = oc._paths(job_id, tmp_path)
    output.write_text(json.dumps({"thread_id": "t1", "message": "token=secret-token-123456789 " + "x" * 2000, "usage": {"input_tokens": 1}}) + "\n", encoding="utf-8")
    result = oc.hermes_codex_job_result(job_id, 500, tmp_path)
    assert "secret-token" not in result["response"]
    assert result["truncated"] is True and result["thread_id"] == "t1"


def test_metadata_never_contains_prompt(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path, write=True)
    prompt = "private task body"
    plan = oc.hermes_codex_start(prompt, str(tmp_path), dry_run=True, hermes_root=tmp_path)
    assert prompt not in json.dumps(plan)
    assert plan["prompt_len"] == len(prompt)
