import logging

import pytest

from loockit.cli import _configure_logging


def test_log_level_environment_overrides_verbosity(monkeypatch):
    monkeypatch.setenv("LOOCKIT_LOG_LEVEL", "error")

    _configure_logging(2)

    assert logging.getLogger().level == logging.ERROR


def test_log_level_defaults_to_warning(monkeypatch):
    monkeypatch.delenv("LOOCKIT_LOG_LEVEL", raising=False)

    _configure_logging(0)

    assert logging.getLogger().level == logging.WARNING


def test_invalid_log_level_is_rejected(monkeypatch):
    monkeypatch.setenv("LOOCKIT_LOG_LEVEL", "TRACE")

    with pytest.raises(ValueError, match="LOOCKIT_LOG_LEVEL"):
        _configure_logging(0)
