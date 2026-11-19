"""Fixtures for the acceptance suite. No real browser session, no Google, no network."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from acceptance.harness import REPO_ROOT, PageWorker
from order_extractor import cli
from order_extractor.config import Settings
from order_extractor.extractor.rules import StubRule


@pytest.fixture(autouse=True)
def _reset_package_logger() -> Iterator[None]:
    """configure_logging() binds handlers to the streams of the moment; undo it after each test."""
    yield
    root = logging.getLogger("order_extractor")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.propagate = True
    root.setLevel(logging.NOTSET)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "app.db"


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean working directory with only the variables `start` needs; returns the DB path."""
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    sa = tmp_path / "secrets" / "sa.json"
    sa.parent.mkdir(exist_ok=True)
    sa.write_text("{}")
    db_path = tmp_path / "data" / "app.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "data" / "logs"))
    monkeypatch.setenv("SHEET_ID", "sheet-id-secret-1234")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(sa))
    monkeypatch.setenv("READ_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("SHEETS_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(cli, "build_rule", lambda settings: StubRule())  # no LLM call in CLI tests
    monkeypatch.setattr(cli, "SELECTORS_PATH", REPO_ROOT / "selectors.json")
    return db_path


@pytest.fixture(scope="module")
def chromium() -> Iterator[PageWorker]:
    pytest.importorskip("playwright.sync_api")
    try:
        worker = PageWorker()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"headless Chromium unavailable: {exc}")
    yield worker
    worker.close()
