"""`data/app.lock` and `--since` parsing (task T15)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from order_extractor.lock import AppLock, LockHeld, lock_path_for
from order_extractor.timeparse import parse_since

HCM = ZoneInfo("Asia/Ho_Chi_Minh")


def utc_ms(*args: int) -> int:
    return round(datetime(*args, tzinfo=UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-19", utc_ms(2026, 11, 18, 17, 0)),  # midnight in TZ_DISPLAY (UTC+7)
        ("2026-11-19 10:38", utc_ms(2026, 11, 19, 3, 38)),
        ("2026-11-19T10:38:30", utc_ms(2026, 11, 19, 3, 38, 30)),
        ("  2026-11-19 10:38  ", utc_ms(2026, 11, 19, 3, 38)),
        ("2026-11-19T10:38:00+00:00", utc_ms(2026, 11, 19, 10, 38)),
        ("2026-11-19T10:38:00Z", utc_ms(2026, 11, 19, 10, 38)),
        ("2026-11-19T10:38:00.250+07:00", utc_ms(2026, 11, 19, 3, 38) + 250),
    ],
)
def test_parse_since(text: str, expected: int):
    assert parse_since(text, HCM) == expected


@pytest.mark.parametrize("text", ["", "yesterday", "19/11/2026", "2026-13-01", "1794544726909"])
def test_parse_since_rejects(text: str):
    with pytest.raises(ValueError, match="invalid time"):
        parse_since(text, HCM)


def test_lock_lives_next_to_the_database():
    assert lock_path_for(Path("data/app.db")) == Path("data/app.lock")


def test_lock_is_exclusive_and_released(tmp_path: Path):
    path = tmp_path / "data" / "app.lock"
    first = AppLock(path)
    first.acquire()
    assert first.held
    assert path.read_text() == f"{os.getpid()}\n"
    with pytest.raises(LockHeld, match="held by another"):
        AppLock(path).acquire()
    first.release()
    first.release()  # idempotent
    assert not first.held
    with AppLock(path) as again:
        assert again.held
    assert not again.held
