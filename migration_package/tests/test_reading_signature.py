from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from application.reading_signature import (
    InvalidMeterReadingSignatureError,
    MissingDeviceIdForSignatureError,
    SignatureCanonicalizationError,
    calculate_meter_reading_signature,
    canonicalize_signature_payload,
    canonicalize_signature_recorded_at,
    verify_meter_reading_signature,
)
from api.models import ResultStatus


TEST_DEVICE_ID = "550e8400-e29b-41d4-a716-446655440000"


def test_calculate_meter_reading_signature_rejects_empty_device_id() -> None:
    with pytest.raises(MissingDeviceIdForSignatureError):
        calculate_meter_reading_signature(
            planned_reading_id="plan-001",
            result_status=ResultStatus.MEASURED,
            raw_reading=Decimal("10.5"),
            structured_payload_json=None,
            recorded_at=datetime.fromisoformat("2026-04-01T08:00:00+09:00"),
            device_id="",
        )


def test_canonicalize_signature_payload_rejects_nan_values() -> None:
    with pytest.raises(SignatureCanonicalizationError):
        canonicalize_signature_payload(
            result_status=ResultStatus.MEASURED,
            raw_reading=None,
            structured_payload_json={"value": float("nan")},
        )


def test_canonicalize_signature_recorded_at_requires_timezone_aware_datetime() -> None:
    with pytest.raises(ValueError):
        canonicalize_signature_recorded_at(datetime(2026, 4, 1, 8, 0, 0))


def test_verify_meter_reading_signature_rejects_mismatch() -> None:
    signature = calculate_meter_reading_signature(
        planned_reading_id="plan-001",
        result_status=ResultStatus.MEASURED,
        raw_reading=Decimal("10.5"),
        structured_payload_json=None,
        recorded_at=datetime.fromisoformat("2026-04-01T08:00:00+09:00"),
        device_id=TEST_DEVICE_ID,
    )
    mismatched_signature = f"{signature[:-1]}{'0' if signature[-1] != '0' else '1'}"

    with pytest.raises(InvalidMeterReadingSignatureError):
        verify_meter_reading_signature(
            planned_reading_id="plan-001",
            result_status=ResultStatus.MEASURED,
            raw_reading=Decimal("10.5"),
            structured_payload_json=None,
            recorded_at=datetime.fromisoformat("2026-04-01T08:00:00+09:00"),
            device_id=TEST_DEVICE_ID,
            signature=mismatched_signature,
        )


def test_verify_meter_reading_signature_accepts_matching_signature() -> None:
    signature = calculate_meter_reading_signature(
        planned_reading_id="plan-pf-001",
        result_status=ResultStatus.MEASURED,
        raw_reading=None,
        structured_payload_json={"percent": "97.5", "phase": "lag"},
        recorded_at=datetime.fromisoformat("2026-04-01T14:00:00+09:00"),
        device_id=TEST_DEVICE_ID,
    )

    verify_meter_reading_signature(
        planned_reading_id="plan-pf-001",
        result_status=ResultStatus.MEASURED,
        raw_reading=None,
        structured_payload_json={"phase": "lag", "percent": "97.5"},
        recorded_at=datetime.fromisoformat("2026-04-01T14:00:00+09:00"),
        device_id=TEST_DEVICE_ID,
        signature=signature,
    )
