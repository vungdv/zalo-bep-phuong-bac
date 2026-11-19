"""Structured logging (design §8, R6.1, R6.5).

* One JSON object per line: `ts`, `level`, `event` (the log message), `logger`, `tick_id`, plus any `extra=`
  fields such as `conversation_key`, `message_key`, `order_id`, `attempt`.
* `tick_id` lives in a `contextvars.ContextVar`; asyncio tasks created during a tick inherit it, so the Sheets job
  a tick starts logs with that tick's id.
* Handlers: stderr (WARNING+) and `LOG_DIR/app.jsonl` (INFO+, rotated at midnight, `RETENTION_DAYS` kept).
  Nothing is ever written to stdout (the status line owns it).
* Privacy guard: the extras `text`, `sender`, `conversation_title` and `title` are emitted only on DEBUG records;
  at INFO and above they are dropped by the formatter. Likewise an exception's message (`str(exc)`, which may
  quote chat content) is written only at DEBUG: above DEBUG a record carries `exc_type` and the traceback frames
  (file, line, function, source line) of each chained exception, but no exception message [R6.1].

Usage::

    log = get_logger(__name__)
    with tick():
        log.info("message_stored", extra={"conversation_key": k, "message_key": m, "text": t})  # text dropped
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

ROOT_LOGGER = "order_extractor"
LOG_FILE_NAME = "app.jsonl"
SENSITIVE_FIELDS = frozenset({"text", "sender", "conversation_title", "title"})

_tick_id: ContextVar[str | None] = ContextVar("tick_id", default=None)

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}


def new_tick_id() -> str:
    return uuid.uuid4().hex[:12]


def current_tick_id() -> str | None:
    return _tick_id.get()


def start_tick(tick_id: str | None = None) -> str:
    """Set a new tick id for the current context and return it (use in the poll loop)."""
    value = tick_id or new_tick_id()
    _tick_id.set(value)
    return value


@contextmanager
def tick(tick_id: str | None = None) -> Iterator[str]:
    """Scope a tick id to a `with` block, restoring the previous one afterwards."""
    token = _tick_id.set(tick_id or new_tick_id())
    try:
        yield _tick_id.get()  # type: ignore[misc]
    finally:
        _tick_id.reset(token)


class JsonFormatter(logging.Formatter):
    """Formats a record as a single JSON line; see module docstring for the fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
            "tick_id": getattr(record, "tick_id", None) or _tick_id.get(),
        }
        debug = record.levelno <= logging.DEBUG
        for key, value in vars(record).items():
            if key in _RESERVED or key in payload:
                continue
            if key in SENSITIVE_FIELDS and not debug:
                continue
            payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            if debug:
                payload["exc"] = self.formatException(record.exc_info)
            else:
                payload["exc_type"] = _type_name(record.exc_info[0])
                payload["exc"] = _redacted_traceback(record.exc_info[1])
        return json.dumps(payload, ensure_ascii=False, default=str)


def _type_name(exc_type: type[BaseException]) -> str:
    module = exc_type.__module__
    return exc_type.__qualname__ if module == "builtins" else f"{module}.{exc_type.__qualname__}"


def _redacted_traceback(exc: BaseException | None) -> str:
    """The traceback of `exc` and its causes/contexts, with each exception's type but never its message."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append(exc)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    parts: list[str] = []
    for item in reversed(chain):
        if parts:
            parts.append("\nThe above exception was followed by:\n\n")
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_tb(item.__traceback__))
        parts.append(f"{_type_name(type(item))}\n")
    return "".join(parts).rstrip("\n")


def get_logger(name: str = ROOT_LOGGER) -> logging.Logger:
    """Logger under the package root, e.g. `get_logger(__name__)`."""
    if name != ROOT_LOGGER and not name.startswith(ROOT_LOGGER + "."):
        name = f"{ROOT_LOGGER}.{name}"
    return logging.getLogger(name)


def configure_logging(
    log_dir: str | Path,
    retention_days: int = 30,
    *,
    level: int = logging.INFO,
    stderr_level: int = logging.WARNING,
) -> logging.Logger:
    """Install the stderr and rotating-file handlers on the package root logger (idempotent).

    `level` is the file threshold (INFO by default; DEBUG enables text/sender/title in the file).
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(stderr_level)
    file_handler = TimedRotatingFileHandler(
        log_dir / LOG_FILE_NAME, when="midnight", backupCount=retention_days, encoding="utf-8", utc=True
    )
    file_handler.setLevel(level)

    root = logging.getLogger(ROOT_LOGGER)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in (stderr_handler, file_handler):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(min(level, stderr_level))
    root.propagate = False  # never reach the stdout-capable root logger
    return root
