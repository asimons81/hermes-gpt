from pathlib import Path

# PEP 701 relaxed f-string quoting is Python 3.12+. hermes-gpt supports 3.10+.
ledger = Path("operator_mission_ledger.py")
text = ledger.read_text(encoding="utf-8")
old = '                                "event_id": f"kanban:{slug}:{int(row["source_rowid"])}",\n'
new = '                                "event_id": f"kanban:{slug}:{int(row[\'source_rowid\'])}",\n'
if text.count(old) != 1:
    raise RuntimeError("expected exactly one Python 3.12-only kanban event_id f-string")
ledger.write_text(text.replace(old, new), encoding="utf-8")

# tomllib entered the stdlib in Python 3.11; use the existing tomli backport on 3.10.
test = Path("test_codex_pr63_remediation.py")
text = test.read_text(encoding="utf-8")
old = "import sqlite3\nimport tomllib\nfrom pathlib import Path\n"
new = "import sqlite3\nfrom pathlib import Path\n\ntry:\n    import tomllib\nexcept ModuleNotFoundError:  # Python 3.10\n    import tomli as tomllib\n"
if text.count(old) != 1:
    raise RuntimeError("expected exactly one direct tomllib import block")
test.write_text(text.replace(old, new), encoding="utf-8")

print("PR #64 Python 3.10 compatibility fixes applied")
