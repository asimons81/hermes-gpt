import operator_fleet as fleet


def test_hermes_bin_prefers_explicit_cli_env(monkeypatch, tmp_path):
    fake_cli = tmp_path / "hermes"
    fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CLI", str(fake_cli))

    assert fleet._hermes_bin() == str(fake_cli)


def test_hermes_bin_uses_local_bin_fallback(monkeypatch, tmp_path):
    fake_home = tmp_path
    fake_cli = fake_home / ".local" / "bin" / "hermes"
    fake_cli.parent.mkdir(parents=True)
    fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(fleet.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("HERMES_CLI", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(fleet.shutil, "which", lambda name: None)
    monkeypatch.setattr(fleet.op, "normalize_hermes_data_root", lambda value: None)

    assert fleet._hermes_bin() == str(fake_cli)


def test_load_hermes_config_falls_back_to_yaml_when_cli_unimportable(monkeypatch, tmp_path):
    """Standalone workers (Autopilot) lack hermes_cli on sys.path; the config
    loader must fall back to reading ~/.hermes/config.yaml directly so
    a2a_agents never silently resolves to {} (UNKNOWN_AGENT wedge,
    2026-10-11)."""
    import json as _json

    fake_home = tmp_path
    cfg_dir = fake_home / ".hermes"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "a2a_agents:\n"
        "  loopback-verifier:\n"
        "    url: http://127.0.0.1:4790/\n"
        "    auth:\n"
        "      type: none\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fleet.Path, "home", classmethod(lambda cls: fake_home))

    # Force the hermes_cli import path to fail — the worker reality.
    import builtins

    real_import = builtins.__import__

    def no_hermes_cli(name, *args, **kwargs):
        if name == "hermes_cli.config" or name.startswith("hermes_cli"):
            raise ModuleNotFoundError("No module named 'hermes_cli'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_hermes_cli)

    cfg = fleet._load_hermes_config()
    peers = cfg.get("a2a_agents") or {}
    assert "loopback-verifier" in peers, f"fallback must resolve peers: {peers}"
    assert peers["loopback-verifier"]["url"] == "http://127.0.0.1:4790/"
