from __future__ import annotations

import base64
import json
import zlib

import pytest

from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.mobile_reading import (
    DRAFT_KIND,
    MAX_NOTE_LENGTH,
    RESULT_CODE_PREFIX,
    RESULT_CODE_SUFFIX,
    dump_mr1_json,
    encode_mr1_envelope,
    extract_mr1_json_object,
    safe_json_for_script,
    validate_note,
    validate_sheet_id,
)

VALID_SHEET_ID = "0123456789abcdef0123456789abcdef"
SECOND_SHEET_ID = "fedcba9876543210fedcba9876543210"


def _sample_payload() -> dict[str, object]:
    return {
        "v": 1,
        "sheet_id": VALID_SHEET_ID,
        "entries": [
            {
                "planned_reading_id": "pr-001",
                "result_status": "measured",
                "raw_reading": "1234.5",
                "note": "現場確認 ✅",
            }
        ],
    }


def _make_mr1_from_bytes(raw_bytes: bytes) -> str:
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    crc = f"{zlib.crc32(encoded.encode('ascii')) & 0xFFFFFFFF:08x}"
    return f"{RESULT_CODE_PREFIX}{encoded}.{crc}{RESULT_CODE_SUFFIX}"


def test_encode_and_extract_mr1_round_trip() -> None:
    payload = _sample_payload()
    code = encode_mr1_envelope(payload)

    assert code.startswith(RESULT_CODE_PREFIX)
    assert code.endswith(RESULT_CODE_SUFFIX)
    assert extract_mr1_json_object(code) == payload


def test_dump_and_extract_json_round_trip() -> None:
    payload = _sample_payload()
    json_text = dump_mr1_json(payload)

    parsed_dict = json.loads(json_text)
    assert parsed_dict["v"] == 1
    assert parsed_dict["sheet_id"] == VALID_SHEET_ID
    assert extract_mr1_json_object(json_text) == payload
    assert extract_mr1_json_object("\ufeff  " + json_text + "\n") == payload


def test_extract_mr1_with_surrounding_text_wrapping_and_quotes() -> None:
    payload = _sample_payload()
    code = encode_mr1_envelope(payload)

    chunks = [code[i : i + 24] for i in range(0, len(code), 24)]
    quoted_wrapped = "\n".join(
        f"{'> ' * ((idx % 3) + 1)}{chunk}" for idx, chunk in enumerate(chunks)
    )
    email_body = (
        "お疲れ様です。本日の検針結果です。\r\n"
        f"{quoted_wrapped}\r\n\r\n"
        "> よろしくお願いいたします。\r\n"
    )

    assert extract_mr1_json_object(email_body) == payload


def test_extract_accepts_identical_duplicate_mr1_blocks_and_rejects_different_blocks() -> None:
    payload = _sample_payload()
    code = encode_mr1_envelope(payload)
    quoted = "\n".join(f">> {code[i : i + 30]}" for i in range(0, len(code), 30))

    reply_text = f"再送します。\n{code}\n\nOn 2026-10-20 wrote:\n{quoted}\n"
    assert extract_mr1_json_object(reply_text) == payload

    second = encode_mr1_envelope({**payload, "sheet_id": SECOND_SHEET_ID})
    with pytest.raises(
        ValidationError,
        match="複数の検針結果が含まれています。取り込む結果を1件だけ貼り付けてください",
    ):
        extract_mr1_json_object(f"{code}\n\n> {second}")


def test_extract_rejects_draft_payload_and_corrupted_blocks() -> None:
    draft_code = encode_mr1_envelope({"kind": DRAFT_KIND, "sheet_id": VALID_SHEET_ID})
    with pytest.raises(ValidationError, match="途中保存用の下書きデータ"):
        extract_mr1_json_object(draft_code)

    valid = encode_mr1_envelope(_sample_payload())
    for invalid_text in (
        valid[:-4],
        valid[4:],
        "MR1:YWJj:END",
        "MR1:YWJj.ABCDEF12:END",
        "MR1:あいうえ.00000000:END",
        _make_mr1_from_bytes(b"\xff\xfe\xfd"),
        _make_mr1_from_bytes(b"{broken json"),
        _make_mr1_from_bytes(b"[]"),
    ):
        with pytest.raises(ValidationError):
            extract_mr1_json_object(invalid_text)


def test_validate_sheet_id_note_and_safe_json_for_script() -> None:
    assert validate_sheet_id(VALID_SHEET_ID) == VALID_SHEET_ID
    with pytest.raises(ValidationError):
        validate_sheet_id("INVALID")

    assert validate_note("  メモ  ") == "メモ"
    assert validate_note("   ") is None
    with pytest.raises(ValidationError):
        validate_note("x" * (MAX_NOTE_LENGTH + 1))

    escaped = safe_json_for_script({"html": "</script><script>&\u2028\u2029"})
    assert "</script>" not in escaped
    assert r"\u003c/script\u003e" in escaped
