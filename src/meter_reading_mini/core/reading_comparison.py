"""正規化済みの検針結果を比較する独立した契約。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from meter_reading_mini.core.errors import ValidationError


@dataclass(frozen=True, slots=True)
class ComparableReadingHeader:
    """日時は呼出し側で UTC 秒単位の DB 形式に正規化して渡す。"""

    result_status: str
    recorded_at: str
    client_updated_at: str


@dataclass(frozen=True, slots=True)
class ComparableReadingContent:
    """構造化入力は serialize_structured_payload で正規化して渡す。"""

    header: ComparableReadingHeader
    raw_reading: Decimal | None
    structured_payload_json: str | None
    note: str | None


def serialize_structured_payload(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"構造化入力を JSON 化できません: {exc}") from exc


def reading_content_matches(
    existing: ComparableReadingContent, incoming: ComparableReadingContent
) -> bool:
    """結果内容のみを比較し、監査情報や再計算値は比較しない。"""

    return (
        existing.header == incoming.header
        and existing.raw_reading == incoming.raw_reading
        and existing.structured_payload_json == incoming.structured_payload_json
        and existing.note == incoming.note
    )
