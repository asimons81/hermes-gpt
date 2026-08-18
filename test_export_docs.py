from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_binary_export_is_documented_with_security_boundaries() -> None:
    guide = _read(ROOT / "docs" / "file-export.md")
    compatibility = _read(ROOT / "docs" / "mcp-compatibility.md")
    docs_map = _read(ROOT / "docs" / "README.md")

    for marker in (
        "hermes_export_file",
        "HERMES_GPT_OPERATOR_ALLOWED_PATHS",
        "HERMES_GPT_EXPORT_MAX_BYTES",
        "HERMES_GPT_EXPORT_ALLOWED_EXTENSIONS",
        "16 MiB",
        "Owner Mode",
        "client",
    ):
        assert marker in guide

    assert "EmbeddedResource(BlobResourceContents)" in compatibility
    assert "client" in compatibility.lower()
    assert "base64" in compatibility.lower()
    assert "file-export.md" in docs_map


def test_binary_export_module_and_guide_ship_in_package() -> None:
    data = tomllib.loads(_read(ROOT / "pyproject.toml"))
    setuptools = data["tool"]["setuptools"]
    assert "operator_export" in setuptools["py-modules"]
    assert "docs/file-export.md" in setuptools["data-files"]["share/hermes-gpt/docs"]


def test_binary_export_ci_has_focused_regressions() -> None:
    workflow = _read(ROOT / ".github" / "workflows" / "ci.yml")
    assert "operator_export.py" in workflow
    assert "test_operator_export.py test_server_export.py" in workflow
