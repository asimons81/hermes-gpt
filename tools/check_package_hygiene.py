"""Package-content hygiene guard for hermes-gpt public artifacts.

Scans built wheel (.whl) and sdist (.tar.gz) artifacts for forbidden
private/operational patterns that must never ship publicly:

- absolute /home paths (machine-specific home directories)
- RFC1918 private-network IPs (10/8, 172.16/12, 192.168/16)
- Tailscale-like CGNAT IPs (100.64/10)
- known machine hostnames from this operator's fleet
- live internal profile counts / operational metrics
- high-confidence private member names such as .env, *.pem, *.key, logs,
  __pycache__, and .pytest_cache

Generic localhost (127.0.0.1), placeholder Windows paths, and explicit
placeholder usernames (e.g. ``/home/user``) are allowed and not flagged.

Usage:
    python tools/check_package_hygiene.py dist/
    python tools/check_package_hygiene.py dist/*.whl dist/*.tar.gz

Exit status: 0 = clean, 1 = findings, 2 = usage/scan error.
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

PLACEHOLDER_HOME_USERS = {
    "user",
    "example",
    "someone",
    "username",
    "alice",
    "bob",
    "charlie",
    "operator",
    "test",
    "demo",
    "app",
    "guest",
    "anyuser",
}
HOME_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./])/home/([A-Za-z0-9_.-]+)")

_OCTET = r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
RFC1918_RE = re.compile(
    r"\b(?:"
    rf"10\.{_OCTET}\.{_OCTET}\.{_OCTET}"
    rf"|192\.168\.{_OCTET}\.{_OCTET}"
    rf"|172\.(?:1[6-9]|2[0-9]|3[01])\.{_OCTET}\.{_OCTET}"
    r")\b"
)
TAILSCALE_RE = re.compile(
    r"\b100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\."
    r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\."
    r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
)
MACHINE_HOSTNAMES_RE = re.compile(r"\b(?:TONY-GAMING-TOP|Hermex)\b")
OPERATIONAL_METRIC_RE = re.compile(
    r"(?<![0-9.])[0-9][\d,]{0,6}\s+"
    r"(?:profiles?|actions?|records?|dispatches?|sessions?|messages?|"
    r"executions?|credentials?|fleet|work\s+orders?|system\s+prompts?|"
    r"usage\s+rows?|async\s+delegations?)\b",
    re.IGNORECASE,
)

TEXT_SUFFIXES = {
    ".md", ".txt", ".rst", ".py", ".toml", ".cfg", ".ini", ".json",
    ".yaml", ".yml", ".csv", ".html", ".css", ".js", ".ps1", ".example",
    ".in", ".dist-info", ".pem",
}

PRIVATE_KEY_BASENAMES = {
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "identity",
}
PRIVATE_KEY_BUNDLE_SUFFIXES = (".p12", ".pfx", ".pkcs12", ".jks", ".keystore")
PRIVATE_CONFIG_COMPONENTS = {
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".kube",
}
KNOWN_BINARY_SUFFIXES = {
    ".pyc", ".so", ".dll", ".exe", ".png", ".jpg", ".jpeg", ".gif",
    ".ico", ".woff", ".woff2", ".ttf", ".whl", ".gz", ".zip",
}


def scan_member_name(name: str) -> list[tuple[str, str]]:
    """Flag high-confidence private/cache filenames inside an artifact."""
    path = PurePosixPath(name.replace("\\", "/"))
    parts = tuple(part.lower() for part in path.parts)
    base = path.name.lower()
    findings: list[tuple[str, str]] = []
    if base == ".env" or base.startswith(".env."):
        findings.append(("private_env_file", name))
    if base in PRIVATE_KEY_BASENAMES or base.endswith((".pem", ".key", *PRIVATE_KEY_BUNDLE_SUFFIXES)):
        findings.append(("private_key_file", name))
    if any(part in PRIVATE_CONFIG_COMPONENTS for part in parts[:-1]):
        findings.append(("private_config_path", name))
    if base.endswith(".log"):
        findings.append(("private_log_file", name))
    if "__pycache__" in parts or ".pytest_cache" in parts:
        findings.append(("private_cache_path", name))
    return findings


def iter_archive_members(path: Path):
    """Yield (member_name, bytes) for every regular file in an archive."""
    if path.suffix in {".whl", ".zip"}:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    yield info.filename, zf.read(info.filename)
    elif path.suffix == ".gz" or path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                f = tf.extractfile(member)
                if f is not None:
                    yield member.name, f.read()
    else:
        raise ValueError(f"Unsupported artifact type: {path}")


def is_text_member(name: str, data: bytes) -> bool:
    """Best-effort text detection for archive members."""
    lower = name.lower()
    if any(lower.endswith(sfx) for sfx in KNOWN_BINARY_SUFFIXES):
        return False
    if any(lower.endswith(sfx) for sfx in TEXT_SUFFIXES):
        return True
    if b"\x00" in data[:4096]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def scan_text(text: str) -> list[tuple[str, str]]:
    """Return [(pattern_name, matched_text)] for forbidden patterns in text."""
    findings: list[tuple[str, str]] = []
    for m in HOME_PATH_RE.finditer(text):
        if m.group(1) not in PLACEHOLDER_HOME_USERS:
            findings.append(("absolute_home_path", m.group(0)))
    for m in RFC1918_RE.finditer(text):
        findings.append(("rfc1918_ip", m.group(0)))
    for m in TAILSCALE_RE.finditer(text):
        findings.append(("tailscale_ip", m.group(0)))
    for m in MACHINE_HOSTNAMES_RE.finditer(text):
        findings.append(("machine_hostname", m.group(0)))
    for m in OPERATIONAL_METRIC_RE.finditer(text):
        findings.append(("operational_metric", m.group(0)))
    return findings


def scan_artifact(path: Path) -> list[tuple[str, str, str]]:
    """Scan one artifact; return [(member_name, pattern, matched_text)]."""
    results: list[tuple[str, str, str]] = []
    for member, data in iter_archive_members(path):
        for pattern, matched in scan_member_name(member):
            results.append((member, pattern, matched))
        if not is_text_member(member, data):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for pattern, matched in scan_text(text):
            results.append((member, pattern, matched))
    return results


def collect_artifacts(paths: list[str]) -> list[Path]:
    artifacts: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for child in sorted(p.iterdir()):
                if child.suffix == ".whl" or child.name.endswith(".tar.gz"):
                    artifacts.append(child)
        elif p.exists():
            artifacts.append(p)
        else:
            raise FileNotFoundError(f"Artifact not found: {raw}")
    return artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan built hermes-gpt artifacts for forbidden private/operational patterns."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Artifact file(s) and/or a directory containing .whl / .tar.gz files",
    )
    args = parser.parse_args(argv)
    try:
        artifacts = collect_artifacts(args.paths)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not artifacts:
        print("ERROR: no .whl or .tar.gz artifacts found", file=sys.stderr)
        return 2
    total: list[tuple[str, str, str, str]] = []
    for artifact in artifacts:
        try:
            findings = scan_artifact(artifact)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"ERROR scanning {artifact}: {exc}", file=sys.stderr)
            return 2
        for member, pattern, matched in findings:
            total.append((str(artifact), member, pattern, matched))
    if total:
        print("PACKAGE HYGIENE FAILURES:")
        for artifact, member, pattern, matched in total:
            print(f"  {artifact} :: {member} :: {pattern}: {matched!r}")
        print(f"\n{len(total)} finding(s) — release-blocking hygiene issue.")
        return 1
    print(f"CLEAN: {len(artifacts)} artifact(s) scanned, no forbidden private/operational patterns.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
