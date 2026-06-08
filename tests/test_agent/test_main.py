import logging

from src.agent.main import run


def test_run_logs_env_vars(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("INSTALLATION_ID", "999")
    monkeypatch.setenv("REPO_FULL_NAME", "test-owner/test-repo")
    monkeypatch.setenv("PR_NUMBER", "42")

    run()

    assert "Installation ID: 999" in caplog.text
    assert "Repository: test-owner/test-repo" in caplog.text
    assert "PR Number: 42" in caplog.text


def test_run_defaults_when_missing_env(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("INSTALLATION_ID", raising=False)
    monkeypatch.delenv("REPO_FULL_NAME", raising=False)
    monkeypatch.delenv("PR_NUMBER", raising=False)

    run()

    assert "Installation ID: unknown" in caplog.text
    assert "Repository: unknown" in caplog.text
    assert "PR Number: unknown" in caplog.text
