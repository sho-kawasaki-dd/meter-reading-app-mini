"""有限の Decimal 文字列を入力する。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtWidgets import QLineEdit, QWidget

from meter_reading_mini.core.contract_models import NON_NEGATIVE_DECIMAL_STRING_PATTERN
from meter_reading_mini.core.errors import ValidationError


class DecimalLineEdit(QLineEdit):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = label
        self.setPlaceholderText("例: 123.45")

    def decimal_value(self, *, required: bool = False) -> Decimal | None:
        text = self.text().strip()
        if not text and not required:
            return None
        if not NON_NEGATIVE_DECIMAL_STRING_PATTERN.fullmatch(text):
            raise ValidationError(f"{self._label}は通常の10進数文字列で入力してください。")
        try:
            value = Decimal(text)
        except InvalidOperation as error:
            raise ValidationError(f"{self._label}が正しくありません。") from error
        if not value.is_finite():
            raise ValidationError(f"{self._label}は有限の数値で入力してください。")
        return value

    def set_decimal_value(self, value: Decimal | None) -> None:
        self.setText("" if value is None else format(value, "f"))
