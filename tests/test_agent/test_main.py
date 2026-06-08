import os
import logging

from src.agent.main import run


def test_run_logs_env_vars(caplog):
    caplog.set_level(logging.INFO)
    os.environ["INSTALLATION_ID"] = "999"
    os.environ["REPO_FULL_NAME"] = "test-owner/test-repo"
    os.environ["PR_NUMBER"] = "42"

    run()

    assert "Installation ID: 999" in caplog.text
    assert "Repository: test-owner/test-repo" in caplog.text
    assert "PR Number: 42" in caplog.text


def test_run_defaults_when_missing_env(caplog):
    caplog.set_level(logging.INFO)
    for key in ["INSTALLATION_ID", "REPO_FULL_NAME", "PR_NUMBER"]:
        os.environ.pop(key, None)

    run()

    assert "Installation ID: unknown" in caplog.text
    assert "Repository: unknown" in caplog.text
    assert "PR Number: unknown" in caplog.text
