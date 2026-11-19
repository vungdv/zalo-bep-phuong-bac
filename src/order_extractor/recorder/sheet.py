"""Google Sheets adapter (design §6): header, column A, and the one append path [R5.2-R5.5].

Every method is **blocking** (gspread over HTTP) and is meant to run in a worker thread
(`asyncio.to_thread`). The HTTP timeout is set on the gspread client (`SHEETS_TIMEOUT_SECONDS`), because asyncio
cannot cancel a running thread. Every failure — API error, auth error, network error, timeout, a missing service
account file — is raised as `SheetsError`, with `timeout=True` when the request timed out.

The app writes only the header row (once, when it is empty) and appended rows. It never writes an existing row,
so anything a human adds to the right of the four columns is left alone [R5.4].
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from functools import wraps
from typing import TYPE_CHECKING, Any, Protocol

import gspread
import requests
from gspread.utils import InsertDataOption, ValueInputOption

from order_extractor.recorder.orders_repo import Order

if TYPE_CHECKING:
    from order_extractor.config import Settings

HEADER: tuple[str, ...] = ("Number", "Quantity", "Customer", "Message")
_ROW_RANGE = re.compile(r"![A-Z]+(\d+)(?::[A-Z]+(\d+))?$")


class SheetsError(Exception):
    """A Sheets call failed; the orders it carried are retried with backoff."""

    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


class Sheet(Protocol):
    """What OrderRecorder needs from the sheet. All methods block and raise `SheetsError`."""

    def read_header(self) -> list[str]:
        """Row 1 as trimmed strings, trailing blanks removed (`[]` when the row is empty)."""
        ...

    def write_header(self) -> None:
        """Write `HEADER` into row 1."""
        ...

    def read_order_ids(self) -> set[str]:
        """Column A below the header as trimmed, non-empty strings [R5.5]."""
        ...

    def append_rows(self, orders: Sequence[Order]) -> list[int | None]:
        """Append one row per order in one call (all or nothing); return each row's 1-based number if known."""
        ...


def normalize_order_id(value: object) -> str:
    """Column-A cell → comparable id: `str()` and trimmed [R5.5]. gspread returns `"42"` for a written `42`."""
    return str(value).strip() if value is not None else ""


def order_row(order: Order) -> list[str | int]:
    """The sheet row for `order` (column order = `HEADER`): order id, quantity, customer (message sender), text."""
    return [str(order.id), order.number, order.sender or "", order.text or ""]


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, requests.exceptions.Timeout | TimeoutError)


# gspread's `open_by_key` turns HTTP 403 into a bare `PermissionError`, which would log an empty message.
_FORBIDDEN_HINT = (
    "403 forbidden by Google: share the sheet with the service account's client_email as Editor, "
    "and check SHEET_ID and that the Sheets and Drive APIs are enabled"
)


def _sheets_call[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Translate every failure of a Sheets call into `SheetsError`."""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except SheetsError:
            raise
        except Exception as exc:
            cause: BaseException = exc
            while cause.__cause__ is not None and not _is_timeout(cause):
                cause = cause.__cause__
            timeout = _is_timeout(cause)
            kind = "timeout" if timeout else type(exc).__name__
            detail = str(exc) or (_FORBIDDEN_HINT if isinstance(exc, PermissionError) else "")
            raise SheetsError(f"{func.__name__} failed ({kind}): {detail}", timeout=timeout) from exc

    return wrapper


def _rows_from_response(response: Any, count: int) -> list[int | None]:
    """Row numbers from `append_rows`' `updates.updatedRange` (e.g. `'Orders'!A5:D7` → 5, 6, 7)."""
    updated = (response or {}).get("updates", {}).get("updatedRange", "")
    match = _ROW_RANGE.search(updated)
    if not match:
        return [None] * count
    first = int(match.group(1))
    return [first + i for i in range(count)]


class GoogleSheet:
    """`Sheet` over a gspread worksheet. The worksheet is opened lazily, inside the first (threaded) call."""

    def __init__(self, open_worksheet: Callable[[], gspread.Worksheet]) -> None:
        self._open_worksheet = open_worksheet
        self._worksheet: gspread.Worksheet | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> GoogleSheet:
        """Service-account client with `SHEETS_TIMEOUT_SECONDS` on its HTTP session; no network until first use."""

        def open_worksheet() -> gspread.Worksheet:
            if not settings.sheet_id:
                raise SheetsError("SHEET_ID is not set")
            client = gspread.service_account(filename=settings.google_service_account_file)
            client.set_timeout(settings.sheets_timeout_seconds)
            return client.open_by_key(settings.sheet_id).worksheet(settings.sheet_orders_tab)

        return cls(open_worksheet)

    def _ws(self) -> gspread.Worksheet:
        with self._lock:
            if self._worksheet is None:
                self._worksheet = self._open_worksheet()
            return self._worksheet

    @_sheets_call
    def read_header(self) -> list[str]:
        values = [str(v).strip() for v in self._ws().row_values(1)]
        while values and values[-1] == "":
            values.pop()
        return values

    @_sheets_call
    def write_header(self) -> None:
        self._ws().update([list(HEADER)], "A1:D1", value_input_option=ValueInputOption.raw)

    @_sheets_call
    def read_order_ids(self) -> set[str]:
        ids = (normalize_order_id(v) for v in self._ws().col_values(1)[1:])
        return {i for i in ids if i}

    @_sheets_call
    def append_rows(self, orders: Sequence[Order]) -> list[int | None]:
        if not orders:
            return []
        response = self._ws().append_rows(
            [order_row(o) for o in orders],
            value_input_option=ValueInputOption.raw,  # text starting with = + - @ is never a formula [R5.2]
            insert_data_option=InsertDataOption.insert_rows,
            table_range="A1",
        )
        return _rows_from_response(response, len(orders))
