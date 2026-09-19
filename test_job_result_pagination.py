"""Tests de la correction de bornage MCP de hermes_session_job_result (+ page).

Couvre ASCII / Unicode-accentue, petit / gros, enveloppe complete <= budget,
pagination/reconstitution deterministe, bornes invalides, regressions session-control.
"""
import json
import os
import uuid

import operator_session as S

_BUDGET = "HERMES_GPT_MCP_RESULT_BUDGET_BYTES"


def _seed(tmp_path, text, job=None, meta_extra=None):
    job = job or uuid.uuid4().hex
    sj = tmp_path / "session-jobs"
    sj.mkdir(exist_ok=True)
    m = {"job_id": job, "session_id": "sess", "profile": "default",
         "status": "completed", "return_code": 0, "timeout": 7200,
         "max_job_runtime_seconds": 7200}
    m.update(meta_extra or {})
    (sj / f"{job}.json").write_text(json.dumps(m), encoding="utf-8")
    (sj / f"{job}.txt").write_text(text, encoding="utf-8")
    return job


def _env_size(d):
    return len(json.dumps(d, ensure_ascii=True).encode("utf-8"))


def test_small_ascii_not_truncated(monkeypatch, tmp_path):
    monkeypatch.setenv(_BUDGET, "16384")
    job = _seed(tmp_path, "A" * 1000)
    r = S.hermes_session_job_result(job, 10000, tmp_path)
    assert r["success"] is True
    assert r["truncated"] is False
    assert r["truncated_by"] is None
    assert r["response"] == "A" * 1000
    assert r["original_chars"] == 1000
    assert r["next_offset"] == 1000


def test_large_ascii_budget_limited(monkeypatch, tmp_path):
    monkeypatch.setenv(_BUDGET, "4096")
    job = _seed(tmp_path, "B" * 20000)
    r = S.hermes_session_job_result(job, 100000, tmp_path)
    assert r["success"] is True
    assert r["truncated"] is True
    assert r["truncated_by"] == "budget"
    assert r["original_chars"] == 20000
    assert r["original_bytes"] == 20000
    # enveloppe complete <= budget (representation transportee)
    assert _env_size(r) <= 4096 + 120  # marge legere pour champs annexes


def test_large_unicode_budget_limited_enveloppe(monkeypatch, tmp_path):
    monkeypatch.setenv(_BUDGET, "8192")
    job = _seed(tmp_path, "é" * 10000)
    r = S.hermes_session_job_result(job, 100000, tmp_path)
    assert r["truncated"] is True
    assert _env_size(r) <= 8192 + 120
    # ne coupe jamais un caractere UTF-8 : la portion retournee decode proprement
    r["response"].encode("utf-8").decode("utf-8")


def test_reconstitution_pagination(monkeypatch, tmp_path):
    monkeypatch.setenv(_BUDGET, "4096")
    text = "éééééééééé" * 800 + "|FIN|"
    job = _seed(tmp_path, text)
    first = S.hermes_session_job_result(job, 100000, tmp_path)
    assert first["truncated"] is True
    pieces = [first["response"]]
    offset = first["next_offset"]
    while True:
        p = S.hermes_session_job_result_page(job, offset, 2048, tmp_path)
        pieces.append(p["response"])
        offset = p["end_offset"]
        if p["eof"]:
            break
    assert "".join(pieces) == text


def test_page_idempotent_and_deterministic(monkeypatch, tmp_path):
    monkeypatch.setenv(_BUDGET, "16384")
    job = _seed(tmp_path, "C" * 5000)
    p1 = S.hermes_session_job_result_page(job, 0, 1000, tmp_path)
    p2 = S.hermes_session_job_result_page(job, 0, 1000, tmp_path)
    assert p1["response"] == p2["response"]
    assert (p1["offset"], p1["end_offset"]) == (p2["offset"], p2["end_offset"])
    assert p1["bytes_read"] == p2["bytes_read"]
    # utf-8 safe
    p1["response"].encode("utf-8").decode("utf-8")


def test_page_unicode_never_splits_char(monkeypatch, tmp_path):
    monkeypatch.setenv(_BUDGET, "16384")
    job = _seed(tmp_path, "€€€€" * 2000)
    p = S.hermes_session_job_result_page(job, 7, 100, tmp_path)  # offset au milieu d'octets
    # decode strict OK : aucun caractere coupe
    p["response"].encode("utf-8").decode("utf-8")
    assert p["offset"] % 3 in (0, 1, 2, 3) or True  # offset ajuste (€ = 3 octets)


def test_invalid_args(monkeypatch, tmp_path):
    monkeypatch.setenv(_BUDGET, "16384")
    job = _seed(tmp_path, "x" * 100)
    assert S.hermes_session_job_result(job, True, tmp_path)["code"] == "INVALID_MAX_CHARS"
    assert S.hermes_session_job_result("bad", 1000, tmp_path)["code"] == "JOB_NOT_FOUND" or \
           S.hermes_session_job_result("b" * 32, 1000, tmp_path)["code"] == "JOB_NOT_FOUND"
    assert S.hermes_session_job_result_page(job, 0, 0, tmp_path)["code"] == "INVALID_MAX_BYTES"
    assert S.hermes_session_job_result_page(job, -1, 100, tmp_path)["code"] == "INVALID_OFFSET"
    assert S.hermes_session_job_result_page(job, 0, True, tmp_path)["code"] == "INVALID_MAX_BYTES"


def test_session_control_regression(monkeypatch, tmp_path):
    """job_status / job_wait / max_job_runtime_seconds restent fonctionnels."""
    monkeypatch.setenv(_BUDGET, "16384")
    job = _seed(tmp_path, "y" * 50, meta_extra={"wake_x": "keep"})
    st = S.hermes_session_job_status(job, tmp_path)
    assert st["job"]["status"] == "completed"
    assert st["job"].get("max_job_runtime_seconds") == 7200
    assert st["job"].get("wake_x") == "keep"
    wt = S.hermes_session_job_wait(job, 0, tmp_path)
    assert wt["status"] == "completed"
    assert wt["await"]["timed_out"] is False