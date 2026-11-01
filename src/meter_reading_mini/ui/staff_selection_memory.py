"""直前の担当者選択を起動中だけ共有する。"""

from __future__ import annotations


class StaffSelectionMemory:
    def __init__(self) -> None:
        self.staff_id: str | None = None
