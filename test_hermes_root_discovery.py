"""Root-discovery hardening tests (audit t_9d200636, Class C).

``server.is_hermes_root`` accepted any directory containing a ``tools/``
subdirectory. A stray ``tools/`` dir at the Hermes DATA root (e.g. an old
nexus tool install) made the data root masquerade as an agent SOURCE root:
with ``HERMES_HOME=<data root>`` exported, ``candidate_roots()`` picked the
data root, ``sys.path`` gained it, and ``import tools`` resolved to a
namespace package that lacks ``file_tools`` — silently degrading the tool
surface to read-only fallbacks.

A real Hermes agent source root has a regular ``tools`` package
(``tools/__init__.py``) or ``hermes_state.py`` at its top level. Namespace
``tools/`` directories must NOT qualify.
"""

from __future__ import annotations

from pathlib import Path

import server


def _make_fake_root(tmp_path: Path) -> Path:
    fake = tmp_path / "data-root"
    (fake / "tools").mkdir(parents=True)
    (fake / "tools" / "unrelated-script.py").write_text("# stray tool, not a Hermes install\n", encoding="utf-8")
    return fake


def test_is_hermes_root_rejects_namespace_tools_dir(tmp_path):
    """A bare tools/ dir (no __init__.py, no hermes_state.py) is not a root."""
    fake = _make_fake_root(tmp_path)
    assert not server.is_hermes_root(fake)


def test_is_hermes_root_accepts_regular_tools_package(tmp_path):
    real_like = tmp_path / "hermes-agent"
    (real_like / "tools").mkdir(parents=True)
    (real_like / "tools" / "__init__.py").write_text("", encoding="utf-8")
    assert server.is_hermes_root(real_like)


def test_is_hermes_root_accepts_hermes_state_marker(tmp_path):
    """Flat checkouts without tools/__init__.py but with hermes_state.py qualify."""
    real_like = tmp_path / "hermes-agent"
    real_like.mkdir(parents=True)
    (real_like / "hermes_state.py").write_text("", encoding="utf-8")
    assert server.is_hermes_root(real_like)


def test_find_hermes_root_skips_data_root_with_stray_tools_dir(tmp_path, monkeypatch):
    """HERMES_HOME at a data root with a stray tools/ must not be selected."""
    data_root = _make_fake_root(tmp_path)
    # Real agent source root at the default <home>/.hermes/hermes-agent spot.
    real_root = tmp_path / ".hermes" / "hermes-agent"
    (real_root / "tools").mkdir(parents=True)
    (real_root / "tools" / "__init__.py").write_text("", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(data_root))
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: tmp_path))
    # Neutralize pip-metadata candidates so only the two paths above compete.
    monkeypatch.setattr(server.importlib.metadata, "distribution", _raise_distribution_not_found)

    assert server.find_hermes_root() == real_root.resolve()


def _raise_distribution_not_found(name: str):
    raise server.importlib.metadata.PackageNotFoundError(name)
