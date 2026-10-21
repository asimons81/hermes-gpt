import operator_workspace as ow


def test_hermes_cli_uses_local_bin_fallback_when_path_missing(monkeypatch, tmp_path):
    fake_home = tmp_path
    fake_cli = fake_home / ".local" / "bin" / "hermes"
    fake_cli.parent.mkdir(parents=True)
    fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(ow.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("HERMES_CLI", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(ow.shutil, "which", lambda name: None)

    assert ow._hermes_cli() == str(fake_cli)
    assert ow._hermes_argv("default", ["gateway", "restart"]) == ["hermes", "gateway", "restart"]


def test_hermes_cli_prefers_explicit_env(monkeypatch, tmp_path):
    executable = tmp_path / "bin" / "hermes"
    monkeypatch.setenv("HERMES_CLI", str(executable))

    assert ow._hermes_cli() == str(executable)
    assert ow._hermes_argv("work", ["gateway", "restart"]) == [
        "hermes",
        "-p",
        "work",
        "gateway",
        "restart",
    ]
