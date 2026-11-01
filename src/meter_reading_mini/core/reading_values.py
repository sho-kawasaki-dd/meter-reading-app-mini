"""割当期間の読値に共通する数値検証。"""

from __future__ import annotations

from decimal import Decimal

from meter_reading_mini.core.errors import ValidationError


def validate_reading_value(
    value: Decimal, *, decimal_places: int | None, rollover_value: Decimal | None
) -> None:
    if not value.is_finite() or value < 0:
        raise ValidationError("読値は有限の 0 以上の数値で指定してください。")
    exponent = value.as_tuple().exponent
    if (
        decimal_places is not None
        and isinstance(exponent, int)
        and max(0, -exponent) > decimal_places
    ):
        raise ValidationError(f"読値の小数は {decimal_places} 桁以内で指定してください。")
    if rollover_value is not None and value >= rollover_value:
        raise ValidationError(
            f"読値 {value} はロールオーバー値 {rollover_value} 未満で指定してください。"
        )
