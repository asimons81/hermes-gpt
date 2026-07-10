"""Single source of truth for the Hermes GPT runtime version."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
import re

UNKNOWN_VERSION = "0+unknown"


def get_version() -> str:
    """Prefer the adjacent checkout metadata, then the installed distribution."""
    pyproject = Path(__file__).resolve().with_name("pyproject.toml")
    if pyproject.is_file():
        try:
            match = re.search(
                r'^version\s*=\s*["\']([^"\']+)["\']',
                pyproject.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            if match:
                return match.group(1)
        except OSError:
            pass
        # An adjacent pyproject identifies a checkout. Never substitute a stale
        # installed distribution when its source metadata is malformed.
        return UNKNOWN_VERSION
    try:
        return distribution_version("hermes-gpt")
    except (PackageNotFoundError, OSError, ValueError):
        return UNKNOWN_VERSION


VERSION = get_version()
