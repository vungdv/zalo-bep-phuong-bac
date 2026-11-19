"""`data/app.lock`: one `start` at a time, and no `reprocess` while `start` runs (design §10, R7.4).

An exclusive, non-blocking `fcntl.flock` on a file next to the database. The OS drops the lock when the process
exits, even on a crash, so a stale file never blocks the next run. The holder's pid is written for diagnostics.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType

LOCK_FILE_NAME = "app.lock"


class LockHeld(RuntimeError):
    """Another process (normally `order-extractor start`) holds the lock."""


def lock_path_for(db_path: Path) -> Path:
    return Path(db_path).parent / LOCK_FILE_NAME


class AppLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise LockHeld(f"{self.path} is held by another order-extractor process") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> AppLock:
        self.acquire()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()
