from __future__ import annotations

import re
from pathlib import Path


def regex_once(path: str, pattern: str, replacement: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one regex match, found {count}")
    p.write_text(updated, encoding="utf-8")


# Preserve the existing shadow-controller contract: dry_run means no mission/work
# execution, not no controller bookkeeping. Codex explicitly allowed the normal
# mutation gates as the alternative to a non-persisting preview, so require those
# gates for every reconcile and retain the established result shape/lease behavior.
regex_once(
    "operator_controller.py",
    r"def hermes_controller_reconcile\(\n.*?\n\ndef hermes_controller_status\(",
    '''def hermes_controller_reconcile(\n    mission_id: str,\n    trigger_kind: str = TRIGGER_MANUAL,\n    *,\n    dry_run: bool = True,\n    hermes_root: Path | None = None,\n) -> str:\n    """Run one supervised shadow reconciliation pass.\n\n    The pass is decision-only with respect to Mission/work execution, but it\n    persists controller plans, telemetry, leases, heartbeats, and attention\n    envelopes. Therefore every invocation requires the normal ``workspace`` +\n    ``direct`` mutation gates even when ``dry_run`` is true.\n    """\n    policy = op.OperatorPolicy()\n    try:\n        policy.require_level("workspace")\n        policy.require_mutation(False)\n        if not MISSION_ID_RE.fullmatch(mission_id or ""):\n            raise ValueError("mission_id is invalid")\n        if trigger_kind not in TRIGGERS:\n            raise ValueError(f"trigger_kind must be one of {TRIGGERS}")\n\n        result = reconcile_pass(\n            mission_id,\n            trigger_kind,\n            host=NullHostAdapter(),\n            hermes_root=hermes_root,\n            interval=DEFAULT_INTERVAL_SECONDS,\n        )\n        result["dry_run"] = bool(dry_run)\n        result["changed"] = True\n        _audit(\n            "hermes_controller_reconcile",\n            policy,\n            dry_run=bool(dry_run),\n            success=not any(k in result for k in ("error",)),\n            changed=True,\n            mission_id=mission_id,\n            node_id=result.get("node_id", ""),\n            extra={\n                "classification": result.get("classification", ""),\n                "row_key": result.get("row_key", ""),\n                "pass_result": result.get("pass_result", ""),\n                "lease_acquired": bool(result.get("lease_acquired")),\n            },\n        )\n        return json.dumps(result, ensure_ascii=False, indent=2)\n    except (\n        ValueError,\n        TypeError,\n        PermissionError,\n        LookupError,\n        OSError,\n        sqlite3.Error,\n    ) as exc:\n        _audit(\n            "hermes_controller_reconcile",\n            policy,\n            dry_run=bool(dry_run),\n            success=False,\n            changed=False,\n            mission_id=mission_id,\n        )\n        return _error(\n            exc,\n            "CONTROLLER_RECONCILE_REJECTED",\n            "Check the mission id, trigger kind, and Operator mutation policy.",\n        )\n\n\ndef hermes_controller_status(''',
)

# Replace the generated focused test file with corrected assertions. In particular,
# parse pyproject.toml as TOML rather than splitting on ']' inside mcp[cli].
test = Path("test_codex_pr63_remediation.py")
text = test.read_text(encoding="utf-8")
text = re.sub(
    r"\n\ndef test_controller_reconcile_dry_run_is_non_persisting\(.*?\n\ndef test_controller_trigger_read_only_cannot_enqueue",
    "\n\ndef test_controller_trigger_read_only_cannot_enqueue",
    text,
    count=1,
    flags=re.S,
)
text = text.replace("import sqlite3\n", "import sqlite3\nimport tomllib\n")
text = re.sub(
    r"def test_pyyaml_is_a_runtime_dependency\(\):\n.*?\Z",
    '''def test_pyyaml_is_a_runtime_dependency():\n    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))\n    runtime = [str(dep).lower() for dep in data["project"]["dependencies"]]\n    assert any(dep == "pyyaml" or dep.startswith("pyyaml") for dep in runtime)\n''',
    text,
    count=1,
    flags=re.S,
)
test.write_text(text, encoding="utf-8")

print("PR #63 remediation compatibility pass applied")
