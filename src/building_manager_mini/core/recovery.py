from __future__ import annotations

from pathlib import Path

from building_manager_mini.core.settings_contracts import CommonSettingsError


class PendingRecoveryError(CommonSettingsError):
    pass


def refuse_pending_operation(pending_path: Path, safety_root: Path) -> None:
    try:
        pending_path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise PendingRecoveryError(
            "中断記録を確認できないため起動を中止しました。"
            f"\n中断記録: {pending_path}\n保全先: {safety_root}\n"
            "保存先のアクセス権を確認し、記録と保全先を削除せずに確認してください。"
        ) from error
    raise PendingRecoveryError(
        "未完了の DB 操作があるため起動を中止しました。"
        f"\n中断記録: {pending_path}\n保全先: {safety_root}\n"
        "自動復旧はまだ利用できません。記録と保全先を削除せず、管理者に相談してください。"
    )
