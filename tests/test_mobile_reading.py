from __future__ import annotations

import base64
import json
import zlib
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from meter_reading_mini.core.contract_models import ReadingCycle
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.mobile_reading import (
    DRAFT_KIND,
    MAX_NOTE_LENGTH,
    RESULT_CODE_PREFIX,
    RESULT_CODE_SUFFIX,
    RESULT_KIND,
    SHEET_KIND,
    SheetFilter,
    cycle_group_for,
    dump_mr1_json,
    encode_mr1_envelope,
    extract_mr1_json_object,
    format_sheet_timestamps,
    parse_mobile_reading_result,
    read_sheet_asset,
    render_sheet_html,
    safe_json_for_script,
    validate_note,
    validate_sheet_id,
)

VALID_SHEET_ID = "0123456789abcdef0123456789abcdef"
SECOND_SHEET_ID = "fedcba9876543210fedcba9876543210"


def _sample_payload() -> dict[str, object]:
    return {
        "kind": RESULT_KIND,
        "v": 1,
        "sheet_id": VALID_SHEET_ID,
        "entries": [
            {
                "planned_reading_id": "pr-001",
                "result_status": "measured",
                "raw_reading": "1234.5",
                "recorded_at": "2026-10-27T08:00:00+09:00",
                "client_updated_at": "2026-10-27T08:01:00+09:00",
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


def test_extract_accepts_plain_json_and_json_embedded_in_text() -> None:
    payload = _sample_payload()
    text = json.dumps(payload, ensure_ascii=False)

    assert extract_mr1_json_object(f"結果です\n> {text}\nよろしく") == payload
    assert extract_mr1_json_object(text) == payload


@pytest.mark.parametrize(
    "text",
    [
        None,
        "   ",
        "{broken",
        "結果です {broken",
        "メッセージだけ",
        "[]",
        'x {"a": 1} y {"b": 2}',
        "ただの :END だけ",
        "MR1:YWJj",
        "MR1:YWJj.ABCDEF12",
        "終端 :END の後に MR1:YWJj.00000000:END",
    ],
)
def test_extract_rejects_unusable_text(text: object) -> None:
    with pytest.raises(ValidationError):
        extract_mr1_json_object(text)


def test_extract_rejects_trailing_end_marker_after_block() -> None:
    code = encode_mr1_envelope(_sample_payload())

    with pytest.raises(ValidationError):
        extract_mr1_json_object(f"{code}:END")


def _result_code(payload: dict[str, object]) -> str:
    return encode_mr1_envelope(payload)


def test_parse_result_from_mr1_and_json_normalizes_timestamps_and_headers() -> None:
    payload = {
        **_sample_payload(),
        "business_date": "2026-10-27",
        "generated_at": "2026-10-27 02:00:00",
        "filter": {"reading_sessions": ["08:00"], "cycle_groups": None},
    }
    entries = payload["entries"]
    assert isinstance(entries, list)
    entries[0]["recorded_at"] = "2026-10-27T08:00:00.987+09:00"
    entries.append(
        {
            "planned_reading_id": "pr-002",
            "result_status": "measured",
            "structured_payload": {"percent": "95.5", "phase": "lag"},
            "recorded_at": "2026-10-26T23:00:00Z",
            "client_updated_at": "2026-10-26T23:01:00Z",
        }
    )
    entries.append(
        {
            "planned_reading_id": "pr-003",
            "result_status": "skipped",
            "recorded_at": "2026-10-27T08:00:00+09:00",
            "client_updated_at": "2026-10-27T08:00:00+09:00",
            "note": "   ",
        }
    )

    for raw in (_result_code(payload), json.dumps(payload, ensure_ascii=False)):
        result = parse_mobile_reading_result(raw)
        assert result.sheet_id == VALID_SHEET_ID
        assert result.business_date == date(2026, 10, 27)
        assert result.generated_at == "2026-10-27 02:00:00"
        assert result.filter == SheetFilter(reading_sessions=("08:00",), cycle_groups=None)
        first, second, third = result.entries
        assert first.recorded_at == datetime(2026, 10, 26, 23, 0, 0, tzinfo=UTC)
        assert first.recorded_at.microsecond == 0
        assert first.note == "現場確認 ✅"
        assert second.structured_payload == {"percent": "95.5", "phase": "lag"}
        assert second.recorded_at == datetime(2026, 10, 26, 23, 0, 0, tzinfo=UTC)
        assert third.note is None

        request = result.to_sync_request()
        assert [item.planned_reading_id for item in request.reading_items] == [
            "pr-001",
            "pr-002",
            "pr-003",
        ]
        assert request.replacement_items == []
        context = result.to_confirmation_context(staff_id="1", staff_name="担当")
        assert (context.sheet_id, context.staff_id, context.staff_name) == (
            VALID_SHEET_ID,
            "1",
            "担当",
        )


def test_parse_result_allows_minimal_payload_without_headers() -> None:
    result = parse_mobile_reading_result(_result_code(_sample_payload()))

    assert result.business_date is None
    assert result.generated_at is None
    assert result.filter is None


def _with_entry(**changes: object) -> dict[str, object]:
    payload = _sample_payload()
    entries = payload["entries"]
    assert isinstance(entries, list)
    entry = {**entries[0], **changes}
    entry = {key: value for key, value in entry.items() if value != "__drop__"}
    payload["entries"] = [entry]
    return payload


def _with_top(**changes: object) -> dict[str, object]:
    payload = {**_sample_payload(), **changes}
    return {key: value for key, value in payload.items() if value != "__drop__"}


@pytest.mark.parametrize(
    "payload",
    [
        _with_top(kind="other"),
        _with_top(kind="__drop__"),
        _with_top(v=2),
        _with_top(v=True),
        _with_top(v="__drop__"),
        _with_top(extra="x"),
        _with_top(sheet_id="__drop__"),
        _with_top(sheet_id="NOT-HEX"),
        _with_top(entries="__drop__"),
        _with_top(entries=[]),
        _with_top(entries="x"),
        _with_top(entries=["x"]),
        _with_top(business_date=20261027),
        _with_top(business_date="2026-13-01"),
        _with_top(business_date="2026-1-1"),
        _with_top(generated_at=1),
        _with_top(generated_at="2026-10-27T02:00:00Z"),
        _with_top(filter="x"),
        _with_top(filter={"unknown": 1}),
        _with_top(filter={"reading_sessions": "08:00"}),
        _with_top(filter={"reading_sessions": []}),
        _with_top(filter={"reading_sessions": ["09:99"]}),
        _with_top(filter={"cycle_groups": [1]}),
        _with_entry(planned_reading_id="__drop__"),
        _with_entry(planned_reading_id=1),
        _with_entry(extra="x"),
        _with_entry(result_status="replaced"),
        _with_entry(result_status="unknown"),
        _with_entry(raw_reading=1234.5),
        _with_entry(raw_reading="1e3"),
        _with_entry(raw_reading="-1"),
        _with_entry(raw_reading="__drop__"),
        _with_entry(note=5),
        _with_entry(note="x" * (MAX_NOTE_LENGTH + 1)),
        _with_entry(structured_payload="x"),
        _with_entry(recorded_at=1_800_000_000),
        _with_entry(recorded_at="2026-10-27T08:00:00"),
        _with_entry(recorded_at="yesterday"),
        _with_entry(client_updated_at="__drop__"),
        _with_entry(result_status="skipped"),
    ],
)
def test_parse_result_rejects_invalid_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse_mobile_reading_result(_result_code(payload))


def test_parse_result_rejects_duplicate_planned_reading_ids() -> None:
    payload = _sample_payload()
    entries = payload["entries"]
    assert isinstance(entries, list)
    payload["entries"] = [entries[0], dict(entries[0])]

    with pytest.raises(ValidationError, match="重複"):
        parse_mobile_reading_result(_result_code(payload))


def test_parse_result_rejects_drafts_unknown_kinds_and_non_json() -> None:
    with pytest.raises(ValidationError, match="下書き"):
        parse_mobile_reading_result(encode_mr1_envelope({"kind": DRAFT_KIND, "v": 1}))
    with pytest.raises(ValidationError, match="検針結果データではありません"):
        parse_mobile_reading_result(json.dumps({"hello": "world"}))
    with pytest.raises(ValidationError):
        parse_mobile_reading_result("検針結果ではありません")


def test_sheet_filter_normalizes_vocabulary_order_and_json() -> None:
    created = SheetFilter.create(["14:00", "08:00", "08:00"], ("weekly", "daily"))

    assert created.reading_sessions == ("08:00", "14:00")
    assert created.cycle_groups == ("daily", "weekly")
    assert created.to_json() == {
        "reading_sessions": ["08:00", "14:00"],
        "cycle_groups": ["daily", "weekly"],
    }
    assert SheetFilter.create().is_empty
    assert SheetFilter.create().to_json() == {"reading_sessions": None, "cycle_groups": None}
    assert not created.is_empty
    assert SheetFilter.from_json(created.to_json()) == created
    with pytest.raises(ValidationError):
        SheetFilter.create(["99:00"])
    with pytest.raises(ValidationError):
        SheetFilter.create(None, ["monthly", "yearly"])


def test_cycle_group_for_collapses_daily_cycles() -> None:
    assert {
        cycle_group_for(cycle)
        for cycle in (ReadingCycle.DAILY_1X, ReadingCycle.DAILY_2X, ReadingCycle.DAILY_3X)
    } == {"daily"}
    assert cycle_group_for(ReadingCycle.WEEKLY) == "weekly"
    assert cycle_group_for(ReadingCycle.MONTHLY) == "monthly"
    assert cycle_group_for(ReadingCycle.EVERY_TWO_MONTHS) == "every_two_months"


def test_format_sheet_timestamps_uses_the_given_timezone() -> None:
    tokyo = format_sheet_timestamps("2026-10-27 11:00:00", ZoneInfo("Asia/Tokyo"))
    utc = format_sheet_timestamps("2026-10-27 11:00:00", UTC)

    assert tokyo == ("2026-10-27 20:00:00", "20261027_200000")
    assert utc == ("2026-10-27 11:00:00", "20261027_110000")
    assert format_sheet_timestamps("broken value", UTC) == ("broken value", "brokenvalue")
    assert format_sheet_timestamps("--", UTC) == ("--", "result")


def _sheet_data(**changes: object) -> dict[str, object]:
    return {
        "kind": SHEET_KIND,
        "v": 1,
        "sheet_id": VALID_SHEET_ID,
        "business_date": "2026-10-27",
        "generated_at": "2026-10-27 11:00:00",
        "generated_at_local": "2026-10-27 20:00:00",
        "filter": {"reading_sessions": None, "cycle_groups": None},
        "slots": [],
        **changes,
    }


def test_render_sheet_html_embeds_assets_and_escaped_data_in_one_file() -> None:
    hostile = "</script><script>alert(1)</script><!--MRM:SCRIPT-->"
    html = render_sheet_html(_sheet_data(locations=[{"location_name": hostile}]))

    assert html.startswith("<!DOCTYPE html>")
    assert "<!--MRM:" not in html
    assert read_sheet_asset("sheet.css").strip() in html
    assert read_sheet_asset("sheet.js").strip() in html
    assert html.count("</script>") == 2
    assert "<script>alert(1)" not in html
    assert "localStorage" not in html
    assert "http://" not in html
    assert "https://" not in html
    data_text = html.split('id="mrm-sheet-data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(data_text)["locations"][0]["location_name"] == hostile
    assert "Chrome" in html


def test_render_sheet_html_rejects_invalid_sheet_data() -> None:
    with pytest.raises(ValidationError):
        render_sheet_html(_sheet_data(kind="other"))
    with pytest.raises(ValidationError):
        render_sheet_html(_sheet_data(sheet_id="short"))
