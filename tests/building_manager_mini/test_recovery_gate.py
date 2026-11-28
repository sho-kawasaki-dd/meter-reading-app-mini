from __future__ import annotations

from pathlib import Path

import pytest

from building_manager_mini.core.recovery import PendingRecoveryError, refuse_pending_operation


def test_missing_record_allows_startup_without_creating_directories(tmp_path: Path) -> None:
    record = tmp_path / "recovery" / "pending.json"
    refuse_pending_operation(record, tmp_path / "backups" / "safety")
    assert not record.parent.exists()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_existing_record_blocks_startup_without_changing_it(tmp_path: Path, kind: str) -> None:
    record = tmp_path / "pending.json"
    if kind == "file":
        record.write_text("未完了", encoding="utf-8")
    else:
        record.mkdir()
    safety = tmp_path / "safety"
    with pytest.raises(PendingRecoveryError, match="未完了") as captured:
        refuse_pending_operation(record, safety)
    assert str(record) in str(captured.value)
    assert str(safety) in str(captured.value)
    if kind == "file":
        assert record.read_text(encoding="utf-8") == "未完了"
    else:
        assert record.is_dir()


def test_inaccessible_record_blocks_startup_with_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "pending.json"

    def fail(_path: Path) -> None:
        raise PermissionError("アクセス拒否")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "lstat", fail)
        with pytest.raises(PendingRecoveryError, match="確認できない") as captured:
            refuse_pending_operation(record, tmp_path / "safety")
    assert isinstance(captured.value.__cause__, PermissionError)
