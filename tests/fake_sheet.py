"""`InMemorySheet` (a `recorder.sheet.Sheet` with failure injection), a frozen clock and a fixed-jitter random.

Kept apart from `fakes.py` so the recorder's doubles live with the recorder's tests (task T13).

Failure injection, consumed in order per method (`read_header`, `write_header`, `read_order_ids`, `append_rows`):

* `fail(method, times=1, timeout=False)` — raise `SheetsError` without doing anything.
* `land_then_timeout(times=1)` — `append_rows` writes the rows, then raises a timeout (response lost).
* `hang(method)` — block the calling (worker) thread until `release()`; then behave normally.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from order_extractor.recorder.orders_repo import Order
from order_extractor.recorder.sheet import HEADER, SheetsError, normalize_order_id, order_row

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
HANG_LIMIT_SECONDS = 5.0  # a forgotten release() must not hang the test run


@dataclass
class _Action:
    kind: str  # "error" | "timeout" | "land_timeout" | "hang"


@dataclass
class InMemorySheet:
    header: list[str] = field(default_factory=list)
    rows: list[list[object]] = field(default_factory=list)  # data rows (sheet row = index + 2)
    calls: list[str] = field(default_factory=list)  # every call, in order, including failed ones
    writes: list[tuple[str, int, int]] = field(default_factory=list)  # (what, first sheet row, row count)

    def __post_init__(self) -> None:
        self._actions: dict[str, list[_Action]] = {}
        self._released = threading.Event()
        self.hanging = threading.Event()  # set while a call is blocked in hang()

    # --- injection ------------------------------------------------------------------------------------------

    def fail(self, method: str, times: int = 1, *, timeout: bool = False) -> None:
        self._actions.setdefault(method, []).extend(_Action("timeout" if timeout else "error") for _ in range(times))

    def land_then_timeout(self, times: int = 1) -> None:
        self._actions.setdefault("append_rows", []).extend(_Action("land_timeout") for _ in range(times))

    def hang(self, method: str) -> None:
        self._released.clear()
        self._actions.setdefault(method, []).append(_Action("hang"))

    def release(self) -> None:
        self._released.set()

    def _before(self, method: str) -> str | None:
        self.calls.append(method)
        queue = self._actions.get(method)
        if not queue:
            return None
        action = queue.pop(0).kind
        if action == "hang":
            self.hanging.set()
            self._released.wait(HANG_LIMIT_SECONDS)
            self.hanging.clear()
            return None
        if action == "error":
            raise SheetsError(f"{method} failed (APIError): 503 unavailable")
        if action == "timeout":
            raise SheetsError(f"{method} failed (timeout): read timed out", timeout=True)
        return action

    # --- Sheet protocol -------------------------------------------------------------------------------------

    def read_header(self) -> list[str]:
        self._before("read_header")
        return list(self.header)

    def write_header(self) -> None:
        self._before("write_header")
        self.header = list(HEADER)
        self.writes.append(("header", 1, 1))

    def read_order_ids(self) -> set[str]:
        self._before("read_order_ids")
        return {i for i in (normalize_order_id(r[0]) for r in self.rows) if i}

    def append_rows(self, orders: Sequence[Order]) -> list[int | None]:
        action = self._before("append_rows")
        first = len(self.rows) + 2
        self.rows.extend(order_row(o) for o in orders)
        self.writes.append(("append", first, len(orders)))
        if action == "land_timeout":
            raise SheetsError("append_rows failed (timeout): read timed out", timeout=True)
        return [first + i for i in range(len(orders))]

    # --- helpers --------------------------------------------------------------------------------------------

    @property
    def column_a(self) -> list[str]:
        return [normalize_order_id(r[0]) for r in self.rows]

    def count(self, method: str) -> int:
        return self.calls.count(method)


class FrozenClock:
    """Epoch-ms clock that only moves when told to."""

    def __init__(self, now_ms: int = 1_800_000_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


class FixedJitter:
    """`random.Random` stand-in: `uniform(a, b)` returns `a + (b - a) * fraction`."""

    def __init__(self, fraction: float = 1.0) -> None:
        self.fraction = fraction

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.fraction
