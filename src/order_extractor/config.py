"""Runtime configuration (design §9), sourced from the environment and an optional `.env` file.

Precedence: process environment > `.env` file > defaults below. Field names are the lower-case form of the
environment keys (e.g. `READ_INTERVAL_SECONDS` -> `read_interval_seconds`). Secrets are never logged:
`Settings.__repr__` hides `sheet_id`, and the service-account file is referenced by path only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, ValidationError, field_validator


class ConfigError(ValueError):
    """Raised when the configuration is invalid; the message names the offending keys."""


class Settings(BaseModel):
    """All configuration keys of design §9 with their defaults."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    # Browser session [R1]
    zalo_url: str = "https://chat.zalo.me"
    pw_session: str = "zalo"
    pw_profile_dir: Path = Path("./data/browser-profile")

    # Reading [R2]
    read_interval_seconds: PositiveFloat = 2.0
    backfill_max_messages: PositiveInt = 200
    selector_alert_seconds: PositiveFloat = 60.0
    browser_max_restarts: PositiveInt = 3

    # Google Sheets [R5]
    google_service_account_file: Path = Path("./secrets/sa.json")
    sheet_id: str = Field(default="", repr=False)
    sheet_orders_tab: str = "Orders"
    sheets_timeout_seconds: PositiveFloat = 10.0
    record_batch_max: PositiveInt = 50
    record_max_attempts: PositiveInt = 5
    record_backoff_base_seconds: PositiveFloat = 2.0
    record_backoff_max_seconds: PositiveFloat = 300.0

    # Operations [R6]
    retention_days: PositiveInt = 30
    db_path: Path = Path("./data/app.db")
    log_dir: Path = Path("./data/logs")
    tz_display: str = "Asia/Ho_Chi_Minh"

    @field_validator("tz_display")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_display)

    @classmethod
    def load(
        cls,
        env_file: str | Path | None = ".env",
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        """Build settings from `env_file` (if it exists) overlaid with `environ` (default `os.environ`).

        Unknown keys are ignored; empty values fall back to the default. Raises `ConfigError` on invalid values.
        """
        values: dict[str, str] = {}
        if env_file is not None and Path(env_file).is_file():
            values.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
        values.update(os.environ if environ is None else environ)
        known = {name.upper(): name for name in cls.model_fields}
        data = {known[k]: v for k, v in values.items() if k in known and v != ""}
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            keys = sorted({str(err["loc"][0]).upper() for err in exc.errors() if err["loc"]})
            raise ConfigError(f"invalid configuration for {', '.join(keys)}: {exc}") from exc
