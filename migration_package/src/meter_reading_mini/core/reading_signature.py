"""MeterReadingSubmission の署名生成と検証をまとめる helper。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from decimal import Decimal
from typing import Any

from meter_reading_mini.core.contract_models import ResultStatus
from meter_reading_mini.core.errors import DomainError
from meter_reading_mini.core.timeutil import DB_DATETIME_FORMAT
from meter_reading_mini.core.timezone_settings import normalize_to_utc


class MeterReadingSignatureError(DomainError):
    """署名関連エラーの基底例外。"""


class MissingDeviceIdForSignatureError(MeterReadingSignatureError):
    """署名シードに必要な device_id が空だったことを表す。"""


class SignatureCanonicalizationError(MeterReadingSignatureError):
    """署名シード用の canonical JSON を生成できなかったことを表す。"""


class InvalidMeterReadingSignatureError(MeterReadingSignatureError):
    """クライアント署名とサーバー再計算値が一致しないことを表す。"""


def canonicalize_signature_payload(
    *,
    result_status: ResultStatus,
    raw_reading: Decimal | None,
    structured_payload_json: dict[str, Any] | None,
) -> str:
    """署名シードの payload 部分を決定的な文字列へ変換する。"""

    if result_status is not ResultStatus.MEASURED:
        return ""
    if raw_reading is not None:
        return format(raw_reading, "f")
    if structured_payload_json is None:
        return ""
    try:
        return json.dumps(
            structured_payload_json,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SignatureCanonicalizationError(
            "structured_payload could not be canonicalized for signature verification."
        ) from exc


def canonicalize_signature_recorded_at(recorded_at: datetime) -> str:
    """署名シードで使う recorded_at を UTC・秒単位へ正規化する。"""

    return normalize_to_utc(recorded_at).strftime(DB_DATETIME_FORMAT)


def calculate_meter_reading_signature(
    *,
    planned_reading_id: str,
    result_status: ResultStatus,
    raw_reading: Decimal | None,
    structured_payload_json: dict[str, Any] | None,
    recorded_at: datetime,
    device_id: str,
) -> str:
    """企画書の署名シード規約に従って SHA-256 を計算する。"""

    if not device_id:
        raise MissingDeviceIdForSignatureError("device_id is required for signature verification.")
    payload_text = canonicalize_signature_payload(
        result_status=result_status,
        raw_reading=raw_reading,
        structured_payload_json=structured_payload_json,
    )
    seed = (
        f"{planned_reading_id}|{result_status.value}|{payload_text}|"
        f"{canonicalize_signature_recorded_at(recorded_at)}|{device_id}"
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def verify_meter_reading_signature(
    *,
    planned_reading_id: str,
    result_status: ResultStatus,
    raw_reading: Decimal | None,
    structured_payload_json: dict[str, Any] | None,
    recorded_at: datetime,
    device_id: str,
    signature: str,
) -> None:
    """クライアント送信署名をサーバー側で再計算して検証する。"""

    expected_signature = calculate_meter_reading_signature(
        planned_reading_id=planned_reading_id,
        result_status=result_status,
        raw_reading=raw_reading,
        structured_payload_json=structured_payload_json,
        recorded_at=recorded_at,
        device_id=device_id,
    )
    if not secrets.compare_digest(signature, expected_signature):
        raise InvalidMeterReadingSignatureError(
            f"Signature mismatch for planned_reading_id {planned_reading_id}."
        )


__all__ = [
    "InvalidMeterReadingSignatureError",
    "MeterReadingSignatureError",
    "MissingDeviceIdForSignatureError",
    "SignatureCanonicalizationError",
    "calculate_meter_reading_signature",
    "canonicalize_signature_payload",
    "canonicalize_signature_recorded_at",
    "verify_meter_reading_signature",
]
