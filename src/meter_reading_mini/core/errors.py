from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

RestoreStage = Literal["pre_backup", "overwrite", "recovery"]


class DomainError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class FileOutputError(DomainError):
    pass


class ValidationError(DomainError):
    pass


class NegativeUsageDeltaError(ValidationError):
    """ロールオーバー補正後も使用量を計算できないことを表す。"""

    def __init__(
        self,
        message: str,
        *,
        raw_reading: Decimal | None = None,
        last_reading: Decimal | None = None,
        rollover_value: Decimal | None = None,
    ) -> None:
        self.raw_reading = raw_reading
        self.last_reading = last_reading
        self.rollover_value = rollover_value
        super().__init__(message)


class TimezoneChangeError(ValidationError):
    """予定枠生成後の業務タイムゾーン変更を表す。"""


class InvalidTimezoneError(ValidationError):
    """不正なタイムゾーン設定値を表す。"""


class ScheduleValidationError(ValidationError):
    """スケジュール値の業務ルール違反を表す。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("スケジュール設定が正しくありません: " + "、".join(self.errors))


class ScheduleHistoryResolutionError(DomainError):
    """適用履歴から対象期間のスケジュールを一意に決められないことを表す。"""

    def __init__(
        self,
        measurement_definition_id: str,
        from_business_date: str,
        through_business_date: str,
        detail: str,
    ) -> None:
        self.measurement_definition_id = measurement_definition_id
        self.from_business_date = from_business_date
        self.through_business_date = through_business_date
        self.detail = detail
        super().__init__(
            f"測定定義 {measurement_definition_id} の期間 "
            f"{from_business_date}〜{through_business_date} のスケジュール履歴を解決できません: "
            f"{detail}"
        )


class MeterAssignmentResolutionError(DomainError):
    """対象日時の物理メーター割当を一意に決められないことを表す。"""

    def __init__(
        self,
        measurement_definition_id: str,
        from_business_date: str,
        through_business_date: str,
        detail: str,
    ) -> None:
        self.measurement_definition_id = measurement_definition_id
        self.from_business_date = from_business_date
        self.through_business_date = through_business_date
        self.detail = detail
        super().__init__(
            f"測定定義 {measurement_definition_id} の期間 "
            f"{from_business_date}〜{through_business_date} のメーター割当を解決できません: "
            f"{detail}"
        )


class InactiveMasterError(DomainError):
    pass


class SupersededPlannedReadingError(ValidationError):
    """交換・再構成・定義無効化で置き換えられた予定枠への結果保存を表す。"""

    def __init__(self, planned_reading_id: str) -> None:
        self.planned_reading_id = planned_reading_id
        super().__init__(f"置き換え済みの予定枠には検針結果を保存できません: {planned_reading_id}")


class MissingPlannedAssignmentError(ValidationError):
    """予定枠の日時・メーターに対応する割当がないことを表す。"""

    def __init__(self, planned_reading_id: str, meter_id: str) -> None:
        self.planned_reading_id = planned_reading_id
        self.meter_id = meter_id
        super().__init__(
            f"予定枠に対応するメーター割当がありません: {planned_reading_id}/{meter_id}"
        )


class AssignmentNeedsReviewError(ValidationError):
    """検針前に割当条件の確認が必要なことを表す。"""

    def __init__(self, measurement_definition_id: str, meter_id: str) -> None:
        self.measurement_definition_id = measurement_definition_id
        self.meter_id = meter_id
        super().__init__(
            "割当条件の確認が必要です。先に条件を訂正してください: "
            f"{measurement_definition_id}/{meter_id}"
        )


class MasterInUseError(DomainError):
    pass


class SchemaTooNewError(DomainError):
    pass


class InvalidBackupError(DomainError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = tuple(reasons)
        message = "バックアップを利用できません: " + "、".join(self.reasons)
        super().__init__(message)


class UnsupportedSchemaError(DomainError):
    pass


class MigrationError(DomainError):
    def __init__(self, message: str, backup_path: Path | None = None) -> None:
        self.backup_path = backup_path
        super().__init__(message)


class RestoreError(DomainError):
    def __init__(
        self,
        message: str,
        *,
        stage: RestoreStage,
        recovered: bool,
        backup_path: Path | None = None,
    ) -> None:
        if stage not in {"pre_backup", "overwrite", "recovery"}:
            raise ValueError(f"不正な復元段階です: {stage}")
        self.stage = stage
        self.recovered = recovered
        self.backup_path = backup_path
        super().__init__(message)
