from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLockFile


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self._lock = QLockFile(str(path))
        self._lock.setStaleLockTime(0)
        self._acquired = False

    def try_acquire(self, timeout_ms: int = 100) -> bool:
        self._acquired = self._lock.tryLock(timeout_ms)
        return self._acquired

    def release(self) -> None:
        if self._acquired:
            self._lock.unlock()
            self._acquired = False
