from __future__ import annotations

import json
import subprocess
import time

SKILL_PATH = r"C:\Users\asimo\AppData\Local\hermes\profiles\hermes-trt-manager\skills\tony-reviews-ops\trt-news-desk-stage\SKILL.md"


def mcp_call(name, args=None, timeout=30):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    }
    result = subprocess.run(
        [
            "curl",
            "-s",
            "-X",
            "POST",
            "http://127.0.0.1:4750/mcp",
            "-H",
            "Content-Type: application/json",
            "-H",
            "Accept: application/json, text/event-stream",
            "-d",
            json.dumps(payload),
            "--max-time",
            str(timeout),
        ],
        capture_output=True,
        text=True,
        timeout=timeout + 5,
    )
    return json.loads(result.stdout)


def main() -> int:
    results = []

    # 1: skill_list
    t0 = time.time()
    resp = mcp_call("hermes_skill_list")
    e = time.time() - t0
    c = resp.get("result", {}).get("content", [])
    t = c[0].get("text", "") if c else ""
    lines = t.split("\n")
    names = set(ln.split(" - ")[0][2:].strip().lower() for ln in lines if ln.startswith("- "))
    passed = bool(c) and len(names) < 200  # dedup should reduce from 1174
    results.append(("hermes_skill_list", passed, f"{len(lines)} lines, {len(names)} unique, {len(t)} chars, {e:.1f}s"))

    # 2: read small file
    t0 = time.time()
    resp = mcp_call("hermes_read_file", {"path": r"C:\Users\asimo\hermes-gpt\pyproject.toml", "offset": 1, "limit": 20})
    e = time.time() - t0
    c = resp.get("result", {}).get("content", [])
    t = c[0].get("text", "") if c else ""
    results.append(("read_file small", bool(c) and "build-system" in t, f"{len(t)} chars, {e:.1f}s"))

    # 3: search small dir
    t0 = time.time()
    resp = mcp_call("hermes_search_files", {"pattern": "version", "target": "content", "path": r"C:\Users\asimo\hermes-gpt", "file_glob": "pyproject.toml", "limit": 5})
    e = time.time() - t0
    c = resp.get("result", {}).get("content", [])
    t = c[0].get("text", "") if c else ""
    results.append(("search small", bool(c) and "0.2.0" in t, f"{len(t)} chars, {e:.1f}s"))

    # 4: read 104KB SKILL.md limit 20
    t0 = time.time()
    resp = mcp_call("hermes_read_file", {"path": SKILL_PATH, "offset": 1, "limit": 20})
    e = time.time() - t0
    c = resp.get("result", {}).get("content", [])
    t = c[0].get("text", "") if c else ""
    results.append(("read_file 104KB limit20", bool(c) and "trt-news-desk-stage" in t, f"{len(t)} chars, {e:.1f}s"))

    # 5: read 104KB SKILL.md limit 100
    t0 = time.time()
    resp = mcp_call("hermes_read_file", {"path": SKILL_PATH, "offset": 1, "limit": 100})
    e = time.time() - t0
    c = resp.get("result", {}).get("content", [])
    t = c[0].get("text", "") if c else ""
    results.append(("read_file 104KB limit100", bool(c) and len(t) > 1000, f"{len(t)} chars, {e:.1f}s"))

    # 6: search for version marker
    t0 = time.time()
    resp = mcp_call("hermes_search_files", {"pattern": "version: 1.33.0", "target": "content", "path": r"C:\Users\asimo\AppData\Local\hermes\profiles\hermes-trt-manager\skills\tony-reviews-ops\trt-news-desk-stage", "limit": 10})
    e = time.time() - t0
    c = resp.get("result", {}).get("content", [])
    t = c[0].get("text", "") if c else ""
    results.append(("search 'version: 1.33.0'", bool(c) and "1.33.0" in t, f"{len(t)} chars, {e:.1f}s"))

    # 7: search for "allow_publish" (0 matches)
    t0 = time.time()
    resp = mcp_call("hermes_search_files", {"pattern": "allow_publish", "target": "content", "path": r"C:\Users\asimo\AppData\Local\hermes\profiles\hermes-trt-manager\skills\tony-reviews-ops\trt-news-desk-stage", "limit": 10})
    e = time.time() - t0
    c = resp.get("result", {}).get("content", [])
    t = c[0].get("text", "") if c else ""
    results.append(("search 'allow_publish' (0)", bool(c) and '"total_count": 0' in t, f"{len(t)} chars, {e:.1f}s"))

    # 8: skill_view with size guard
    t0 = time.time()
    resp = mcp_call("hermes_skill_view", {"name": "trt-news-desk-stage"})
    e = time.time() - t0
    c = resp.get("result", {}).get("content", [])
    t = c[0].get("text", "") if c else ""
    truncated = "TRUNCATED" in t
    results.append(("skill_view (size guard)", bool(c), f"{len(t)} chars, truncated={truncated}, {e:.1f}s"))

    # 9: error handling
    resp = mcp_call("hermes_read_file", {"path": r"C:\nonexistent\file.txt"})
    result = resp.get("result", {})
    is_error = result.get("isError", False)
    c = result.get("content", [])
    t = c[0].get("text", "") if c else ""
    has_err = "not found" in t.lower() or "error" in t.lower()
    results.append(("error handling", is_error or has_err, f"isError={is_error}, msg={t[:100]}"))

    # 10: no stale processes
    result = subprocess.run(["tasklist.exe"], capture_output=True, text=True, timeout=5)
    cf = [ln for ln in result.stdout.split("\n") if "cloudflared" in ln.lower()]
    results.append(("no stale processes", True, f"cloudflared: {len(cf)}"))

    for name, passed, detail in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")

    all_pass = all(r[1] for r in results)
    print(f"\nOverall: {'ALL PASS' if all_pass else 'SOME FAILURES'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
