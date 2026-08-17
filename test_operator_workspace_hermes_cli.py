import operator_workspace as ow


def test_hermes_argv_uses_local_bin_fallback_when_path_missing(monkeypatch, tmp_path):
    fake_home = tmp_path
    fake_cli = fake_home / ".local" / "bin" / "hermes"
    fake_cli.parent.mkdir(parents=True)
    fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("HERMES_CLI", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(ow.shutil, "which", lambda name: None)

    assert ow._hermes_argv("default", ["gateway", "restart"]) == [str(fake_cli), "gateway", "restart"]


def test_hermes_argv_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("HERMES_CLI", "/opt/hermes/bin/hermes")

    assert ow._hermes_argv("work", ["gateway", "restart"]) == [
        "/opt/hermes/bin/hermes",
        "-p",
        "work",
        "gateway",
        "restart",
    ]
