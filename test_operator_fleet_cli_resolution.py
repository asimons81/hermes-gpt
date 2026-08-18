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
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("HERMES_CLI", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(fleet.shutil, "which", lambda name: None)
    monkeypatch.setattr(fleet.op, "normalize_hermes_data_root", lambda value: None)

    assert fleet._hermes_bin() == str(fake_cli)
