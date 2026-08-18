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
    readme = _read(ROOT / "README.md")
    operator = _read(ROOT / "docs" / "operator-mode.md")
    compatibility = _read(ROOT / "docs" / "mcp-compatibility.md")

    for text in (readme, operator):
        assert "hermes_export_file" in text
        assert "HERMES_GPT_OPERATOR_ALLOWED_PATHS" in text
        assert "HERMES_GPT_EXPORT_MAX_BYTES" in text
        assert "HERMES_GPT_EXPORT_ALLOWED_EXTENSIONS" in text

    assert "EmbeddedResource(BlobResourceContents)" in compatibility
    assert "client" in compatibility.lower()
    assert "base64" in compatibility.lower()


def test_binary_export_module_ships_in_package() -> None:
    data = tomllib.loads(_read(ROOT / "pyproject.toml"))
    assert "operator_export" in data["tool"]["setuptools"]["py-modules"]


def test_binary_export_ci_has_focused_regressions() -> None:
    workflow = _read(ROOT / ".github" / "workflows" / "ci.yml")
    assert "operator_export.py" in workflow
    assert "test_operator_export.py test_server_export.py" in workflow
