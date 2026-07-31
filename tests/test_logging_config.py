"""
Tests for logging configuration.
"""

import logging
from logging.handlers import RotatingFileHandler

import pytest

from config import logging_config


@pytest.fixture
def fresh_handlers():
    """Clear the cached shared handlers so each test builds its own."""
    original = logging_config._shared_handlers
    logging_config._shared_handlers = None
    yield
    logging_config._shared_handlers = original


def test_file_handler_disabled_by_env_var(fresh_handlers, monkeypatch):
    monkeypatch.setenv(logging_config.LOG_TO_FILE_ENV_VAR, "0")

    handlers = logging_config._get_shared_handlers()

    assert not any(isinstance(h, RotatingFileHandler) for h in handlers)
    # Something has to receive the records, or logs vanish silently.
    assert any(isinstance(h, logging.StreamHandler) for h in handlers)


def test_file_handler_enabled_by_default(fresh_handlers, monkeypatch, tmp_path):
    monkeypatch.delenv(logging_config.LOG_TO_FILE_ENV_VAR, raising=False)
    monkeypatch.setattr(logging_config, "LOG_FILE", tmp_path / "logs" / "noteflow.log")

    handlers = logging_config._get_shared_handlers()

    assert any(isinstance(h, RotatingFileHandler) for h in handlers)
    for handler in handlers:
        handler.close()


def test_test_suite_does_not_write_to_the_log_file():
    """The suite itself must not append to the service's shared log."""
    import os

    assert os.environ.get(logging_config.LOG_TO_FILE_ENV_VAR) == "0"
    assert not any(
        isinstance(h, RotatingFileHandler)
        for h in logging_config._get_shared_handlers()
    )
