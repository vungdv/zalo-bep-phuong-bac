from pathlib import Path

import pytest

from order_extractor.config import ConfigError, Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_defaults_match_design():
    s = Settings.load(env_file=None, environ={})
    assert s.zalo_url == "https://chat.zalo.me"
    assert s.pw_session == "zalo"
    assert s.read_interval_seconds == 2
    assert s.backfill_max_messages == 200
    assert s.record_max_attempts == 5
    assert s.retention_days == 30
    assert s.db_path == Path("./data/app.db")
    assert s.tz.key == "Asia/Ho_Chi_Minh"


def test_env_example_covers_every_setting():
    keys = {line.split("=", 1)[0] for line in ENV_EXAMPLE.read_text().splitlines() if line and not line.startswith("#")}
    assert keys == {name.upper() for name in Settings.model_fields}


def test_env_overrides_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("READ_INTERVAL_SECONDS=5\nSHEET_ID=from-file\nUNRELATED=x\nRETENTION_DAYS=\n")
    s = Settings.load(env_file=env_file, environ={"SHEET_ID": "from-env"})
    assert s.read_interval_seconds == 5
    assert s.sheet_id == "from-env"
    assert s.retention_days == 30  # empty value falls back to the default


def test_missing_env_file_is_fine(tmp_path):
    assert Settings.load(env_file=tmp_path / "nope", environ={}).sheet_orders_tab == "Orders"


def test_sheet_id_not_in_repr():
    s = Settings.load(env_file=None, environ={"SHEET_ID": "secret-sheet"})
    assert "secret-sheet" not in repr(s)


@pytest.mark.parametrize(
    ("key", "value"),
    [("READ_INTERVAL_SECONDS", "0"), ("RECORD_BATCH_MAX", "abc"), ("TZ_DISPLAY", "Mars/Base")],
)
def test_invalid_values_name_the_key(key, value):
    with pytest.raises(ConfigError, match=key):
        Settings.load(env_file=None, environ={key: value})
