from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.reading_comparison import (
    ComparableReadingContent,
    ComparableReadingHeader,
    reading_content_matches,
    serialize_structured_payload,
)

CONTENT = ComparableReadingContent(
    header=ComparableReadingHeader(
        result_status="measured",
        recorded_at="2026-02-04 08:00:00",
        client_updated_at="2026-02-04 08:01:00",
    ),
    raw_reading=Decimal("10.50"),
    structured_payload_json=None,
    note="確認済み",
)


def test_comparison_uses_decimal_value_instead_of_decimal_scale() -> None:
    assert reading_content_matches(CONTENT, replace(CONTENT, raw_reading=Decimal("10.5")))


@pytest.mark.parametrize(
    "changed",
    [
        replace(CONTENT, header=replace(CONTENT.header, result_status="skipped")),
        replace(CONTENT, header=replace(CONTENT.header, recorded_at="2026-02-04 08:00:01")),
        replace(CONTENT, header=replace(CONTENT.header, client_updated_at="2026-02-04 08:01:01")),
        replace(CONTENT, raw_reading=Decimal("10.51")),
        replace(CONTENT, raw_reading=None),
        replace(CONTENT, structured_payload_json="{}"),
        replace(CONTENT, note=None),
        replace(CONTENT, note="確認済み "),
    ],
)
def test_comparison_distinguishes_every_content_field(changed: ComparableReadingContent) -> None:
    assert not reading_content_matches(CONTENT, changed)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (None, None, True),
        (None, {}, False),
        ({"outer": {"b": 1, "a": "値"}}, {"outer": {"a": "値", "b": 1}}, True),
        ({"value": True}, {"value": 1}, False),
        ({"value": 1}, {"value": 1.0}, False),
        ({"value": "1"}, {"value": 1}, False),
        ({"values": [1, 2]}, {"values": [2, 1]}, False),
    ],
)
def test_comparison_uses_canonical_json_not_python_object_equality(
    left: dict[str, Any] | None, right: dict[str, Any] | None, expected: bool
) -> None:
    existing = replace(
        CONTENT, raw_reading=None, structured_payload_json=serialize_structured_payload(left)
    )
    incoming = replace(
        CONTENT, raw_reading=None, structured_payload_json=serialize_structured_payload(right)
    )
    assert reading_content_matches(existing, incoming) is expected


def test_structured_payload_serialization_surfaces_invalid_values() -> None:
    with pytest.raises(ValidationError, match="JSON 化"):
        serialize_structured_payload({"invalid": object()})
