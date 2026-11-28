from __future__ import annotations

from collections.abc import Callable


class ApplicationLauncher:
    def __init__(self) -> None:
        self._openers: dict[str, Callable[[], int]] = {}

    def register(self, application_id: str, opener: Callable[[], int]) -> None:
        if not application_id:
            raise ValueError("アプリ識別子は空にできません。")
        if application_id in self._openers:
            raise ValueError(f"アプリは既に登録されています: {application_id}")
        self._openers[application_id] = opener

    def open(self, application_id: str) -> int:
        try:
            opener = self._openers[application_id]
        except KeyError as error:
            raise LookupError(f"未登録のアプリです: {application_id}") from error
        return opener()
