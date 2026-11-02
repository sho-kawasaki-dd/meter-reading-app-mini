"""シートの保存フォルダーをアプリ起動中だけ共有する。"""

from __future__ import annotations

from pathlib import Path


class SheetSaveDirectoryMemory:
    def __init__(self) -> None:
        self.directory: Path | None = None
