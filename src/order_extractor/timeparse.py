"""`--since` values (R2.11, R7.3): a time given by the operator → epoch milliseconds (UTC).

Accepted: `YYYY-MM-DD` (midnight), `YYYY-MM-DD HH:MM[:SS]`, `YYYY-MM-DDTHH:MM[:SS]`, each optionally with a UTC
offset (`+07:00`, `Z`). A value without an offset is local time in `TZ_DISPLAY`.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

FORMATS_HINT = "use YYYY-MM-DD, 'YYYY-MM-DD HH:MM' or an ISO 8601 time"


def parse_since(text: str, tz: tzinfo) -> int:
    """Raises `ValueError` (with a hint) for anything else."""
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"invalid time {text!r}: {FORMATS_HINT}") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return round(value.timestamp() * 1000)
