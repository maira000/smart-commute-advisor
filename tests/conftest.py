"""Shared pytest fixtures for the Smart Commute app's AppTest suite."""

import logging
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent

for _dir in (PROJECT_ROOT, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import fortyguard_client as fg  # noqa: E402
from helpers import DEFAULT_AREA, DEFAULT_DATE, DEFAULT_THRESHOLD_F, demo_payload, write_cache  # noqa: E402

# Quiet noisy bare-mode / no-runtime streamlit log lines during AppTest runs.
for _logger in (
    "streamlit",
    "streamlit.runtime.scriptrunner_utils.script_run_context",
    "streamlit.runtime.caching",
):
    logging.getLogger(_logger).setLevel(logging.CRITICAL)


@pytest.fixture
def app():
    return AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=60)


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("FORTYGUARD_API_KEY", "apptest-fake-key-0000")


@pytest.fixture
def no_api_key(monkeypatch):
    monkeypatch.delenv("FORTYGUARD_API_KEY", raising=False)


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """Redirects the FortyGuard cache dir so tests never touch data/*.json."""
    monkeypatch.setattr(fg, "CACHE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def demo_caches(isolated_cache, api_key):
    """Pre-writes demo-shaped FortyGuard caches for every analytic_type."""
    for analytic in ("tcm", "exceedance", "persistence"):
        write_cache(
            demo_payload(analytic),
            DEFAULT_AREA,
            DEFAULT_DATE,
            analytic,
            None if analytic == "tcm" else DEFAULT_THRESHOLD_F,
        )