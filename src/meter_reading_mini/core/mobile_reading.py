from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from importlib.resources import files
from typing import cast

from pydantic import ValidationError as PydanticValidationError

from meter_reading_mini.core.contract_models import (
    MeterReadingSubmission,
    MeterReadingSyncRequest,
    ReadingCycle,
    ReadingSession,
    ResultStatus,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import ReadingConfirmationContext
from meter_reading_mini.core.timeutil import DB_DATETIME_FORMAT, parse_utc, to_local

RESULT_CODE_PREFIX = "MR1:"
RESULT_CODE_SUFFIX = ":END"
SHEET_KIND = "mrm-meter-reading-sheet"
DRAFT_KIND = "mrm-meter-reading-draft"
RESULT_KIND = "mrm-meter-reading-result"
MAX_NOTE_LENGTH = 500
PAYLOAD_VERSION = 1
CYCLE_GROUPS = ("daily", "weekly", "monthly", "every_two_months")
ASSET_DIRECTORY = "mobile_reading_assets"
ASSET_FILENAMES = ("sheet.html", "sheet.css", "sheet.js")

_SHEET_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_LEADING_QUOTE_PATTERN = re.compile(r"^[ \t>]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_ASSET_PLACEHOLDER_PATTERN = re.compile(r"<!--MRM:(STYLE|SCRIPT|DATA)-->")
_RESULT_TOP_LEVEL_KEYS = frozenset(
    {"kind", "v", "sheet_id", "business_date", "generated_at", "filter", "entries"}
)
_RESULT_REQUIRED_KEYS = frozenset({"kind", "v", "sheet_id", "entries"})
_ENTRY_KEYS = frozenset(
    {
        "planned_reading_id",
        "result_status",
        "raw_reading",
        "structured_payload",
        "recorded_at",
        "client_updated_at",
        "note",
    }
)
_ENTRY_REQUIRED_KEYS = frozenset(
    {"planned_reading_id", "result_status", "recorded_at", "client_updated_at"}
)
_ACCEPTED_RESULT_STATUSES = frozenset(
    status.value for status in ResultStatus if status is not ResultStatus.REPLACED
)
_DAILY_CYCLES = frozenset({ReadingCycle.DAILY_1X, ReadingCycle.DAILY_2X, ReadingCycle.DAILY_3X})

_SCRIPT_JSON_ESCAPE_TABLE = str.maketrans(
    {
        "<": r"\u003c",
        ">": r"\u003e",
        "&": r"\u0026",
        "\u2028": r"\u2028",
        "\u2029": r"\u2029",
    }
)


def crc32_hex(value: bytes) -> str:
    return f"{zlib.crc32(value) & 0xFFFFFFFF:08x}"


def validate_sheet_id(value: object) -> str:
    if not isinstance(value, str) or _SHEET_ID_PATTERN.fullmatch(value) is None:
        raise ValidationError("シート ID の形式が正しくありません")
    return value


def validate_note(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("備考は文字列で指定してください")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_NOTE_LENGTH:
        raise ValidationError(f"備考は {MAX_NOTE_LENGTH} 文字以内で入力してください")
    return normalized


def safe_json_for_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).translate(
        _SCRIPT_JSON_ESCAPE_TABLE
    )


def format_sheet_timestamps(utc_str: str, tz: tzinfo) -> tuple[str, str]:
    """UTC 文字列を業務タイムゾーンの表示用・ファイル名用文字列へ変換する。"""

    try:
        local_dt = to_local(utc_str, tz)
        return (
            local_dt.strftime("%Y-%m-%d %H:%M:%S"),
            local_dt.strftime("%Y%m%d_%H%M%S"),
        )
    except ValueError:
        sanitized = re.sub(r"[^0-9A-Za-z]+", "", utc_str) or "result"
        return utc_str, sanitized


def cycle_group_for(reading_cycle: ReadingCycle) -> str:
    """予定枠の周期を、シートの絞り込みで使う 4 区分へ丸める。"""

    return "daily" if reading_cycle in _DAILY_CYCLES else reading_cycle.value


@dataclass(frozen=True, slots=True)
class SheetFilter:
    """シート出力・結果に記録する絞り込み条件。`None` は条件なし。"""

    reading_sessions: tuple[str, ...] | None = None
    cycle_groups: tuple[str, ...] | None = None

    @classmethod
    def create(
        cls,
        reading_sessions: object = None,
        cycle_groups: object = None,
    ) -> SheetFilter:
        """語彙を検証し、正規の順序へ並べ替えて重複を除く。"""

        return cls(
            reading_sessions=_normalize_filter_values(
                reading_sessions, tuple(session.value for session in ReadingSession), "検針時間"
            ),
            cycle_groups=_normalize_filter_values(cycle_groups, CYCLE_GROUPS, "周期区分"),
        )

    @classmethod
    def from_json(cls, value: object) -> SheetFilter:
        if not isinstance(value, dict):
            raise ValidationError("絞り込み条件の形式が正しくありません")
        data = cast(dict[str, object], value)
        unknown = sorted(set(data) - {"reading_sessions", "cycle_groups"})
        if unknown:
            raise ValidationError(f"絞り込み条件に未知の項目があります: {', '.join(unknown)}")
        return cls.create(data.get("reading_sessions"), data.get("cycle_groups"))

    def to_json(self) -> dict[str, list[str] | None]:
        return {
            "reading_sessions": None if self.reading_sessions is None else [*self.reading_sessions],
            "cycle_groups": None if self.cycle_groups is None else [*self.cycle_groups],
        }

    @property
    def is_empty(self) -> bool:
        return self.reading_sessions is None and self.cycle_groups is None


def _normalize_filter_values(
    values: object, vocabulary: tuple[str, ...], label: str
) -> tuple[str, ...] | None:
    if values is None:
        return None
    if isinstance(values, str) or not isinstance(values, (list, tuple, set, frozenset)):
        raise ValidationError(f"{label}の絞り込みは一覧で指定してください")
    items = list(cast(Iterable[object], values))
    if not items:
        raise ValidationError(f"{label}の絞り込みが空です")
    invalid = [item for item in items if not isinstance(item, str) or item not in vocabulary]
    if invalid:
        raise ValidationError(f"{label}の絞り込みに未対応の値があります: {invalid}")
    selected = set(items)
    return tuple(value for value in vocabulary if value in selected)


def render_sheet_html(sheet_data: Mapping[str, object]) -> str:
    """資産とシートデータを、外部参照のない 1 つの HTML にまとめる。"""

    if sheet_data.get("kind") != SHEET_KIND:
        raise ValidationError("シートデータの種別が正しくありません")
    validate_sheet_id(sheet_data.get("sheet_id"))
    replacements = {
        "STYLE": read_sheet_asset("sheet.css"),
        "SCRIPT": read_sheet_asset("sheet.js"),
        "DATA": safe_json_for_script(dict(sheet_data)),
    }
    template = read_sheet_asset("sheet.html")
    return _ASSET_PLACEHOLDER_PATTERN.sub(lambda match: replacements[match.group(1)], template)


def read_sheet_asset(name: str) -> str:
    return (
        files("meter_reading_mini.core").joinpath(ASSET_DIRECTORY, name).read_text(encoding="utf-8")
    )


def encode_mr1_envelope(payload: Mapping[str, object]) -> str:
    json_bytes = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    encoded_bytes = base64.b64encode(json_bytes)
    encoded_str = encoded_bytes.decode("ascii")
    checksum = crc32_hex(encoded_bytes)
    return f"{RESULT_CODE_PREFIX}{encoded_str}.{checksum}{RESULT_CODE_SUFFIX}"


def dump_mr1_json(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, indent=2)


def _decode_mr1_block(block: str) -> str:
    inner = block[len(RESULT_CODE_PREFIX) : -len(RESULT_CODE_SUFFIX)]
    if "." not in inner:
        raise ValidationError("結果テキストが途中で欠けているか改変されています")
    encoded, checksum = inner.rsplit(".", 1)
    if not encoded or _CHECKSUM_PATTERN.fullmatch(checksum) is None:
        raise ValidationError("結果テキストが途中で欠けているか改変されています")

    try:
        encoded_bytes = encoded.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValidationError("結果テキストが途中で欠けているか改変されています") from error

    expected_checksum = crc32_hex(encoded_bytes)
    if checksum != expected_checksum:
        raise ValidationError("結果テキストが途中で欠けているか改変されています")

    if len(encoded) % 4 != 0 or _BASE64_PATTERN.fullmatch(encoded) is None:
        raise ValidationError("結果テキストの Base64 形式が正しくありません")

    try:
        decoded_bytes = base64.b64decode(encoded_bytes, validate=True)
        return decoded_bytes.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        raise ValidationError("結果テキストの Base64 形式が正しくありません") from error


def _extract_raw_json_object(raw_text: object) -> object:
    if not isinstance(raw_text, str):
        raise ValidationError("検針結果テキストを入力してください")

    stripped = raw_text.lstrip("\ufeff").strip()
    if not stripped:
        raise ValidationError("検針結果テキストを入力してください")

    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as error:
            raise ValidationError("結果データの JSON 形式が正しくありません") from error

    cleaned_lines = [_LEADING_QUOTE_PATTERN.sub("", line) for line in stripped.splitlines()]
    cleaned_text = "\n".join(cleaned_lines)

    starts = [match.start() for match in re.finditer(re.escape(RESULT_CODE_PREFIX), cleaned_text)]
    if starts:
        prefix_before_first = _WHITESPACE_PATTERN.sub("", cleaned_text[: starts[0]])
        if RESULT_CODE_SUFFIX in prefix_before_first:
            raise ValidationError("結果テキストが途中で欠けているか改変されています")

        normalized_blocks: list[str] = []
        for index, start in enumerate(starts):
            next_start = starts[index + 1] if index + 1 < len(starts) else len(cleaned_text)
            segment = cleaned_text[start:next_start]
            compact_segment = _WHITESPACE_PATTERN.sub("", segment)
            end_pos = compact_segment.find(RESULT_CODE_SUFFIX)
            if end_pos == -1:
                raise ValidationError(
                    "結果テキストの終端(:END)が見つかりません。"
                    "結果テキストが途中で欠けているか改変されています"
                )
            block = compact_segment[: end_pos + len(RESULT_CODE_SUFFIX)]
            remainder = compact_segment[end_pos + len(RESULT_CODE_SUFFIX) :]
            if RESULT_CODE_SUFFIX in remainder:
                raise ValidationError("結果テキストが途中で欠けているか改変されています")
            normalized_blocks.append(block)

        unique_blocks = list(dict.fromkeys(normalized_blocks))
        if len(unique_blocks) > 1:
            raise ValidationError(
                "複数の検針結果が含まれています。取り込む結果を1件だけ貼り付けてください"
            )

        decoded_json = _decode_mr1_block(unique_blocks[0])
        try:
            return json.loads(decoded_json)
        except json.JSONDecodeError as error:
            raise ValidationError("結果データの JSON 形式が正しくありません") from error

    compact_all = _WHITESPACE_PATTERN.sub("", cleaned_text)
    if RESULT_CODE_SUFFIX in compact_all:
        raise ValidationError("結果テキストが途中で欠けているか改変されています")

    brace_pos = cleaned_text.find("{")
    if brace_pos != -1:
        decoder = json.JSONDecoder()
        try:
            parsed, end_idx = decoder.raw_decode(cleaned_text, brace_pos)
        except json.JSONDecodeError as error:
            raise ValidationError("結果データの JSON 形式が正しくありません") from error
        trailing = cleaned_text[end_idx:]
        if "{" in trailing:
            raise ValidationError(
                "複数の検針結果が含まれています。取り込む結果を1件だけ貼り付けてください"
            )
        return parsed

    raise ValidationError("有効な検針結果データが見つかりません")


def extract_mr1_json_object(raw_text: object) -> dict[str, object]:
    data = _extract_raw_json_object(raw_text)
    if not isinstance(data, dict):
        raise ValidationError("検針結果データの形式が正しくありません")
    payload_dict = cast(dict[str, object], data)
    if payload_dict.get("kind") == DRAFT_KIND:
        raise ValidationError(
            "これは途中保存用の下書きデータです。確認済みの検針結果データを読み込んでください"
        )
    return payload_dict


@dataclass(frozen=True, slots=True)
class MobileReadingResult:
    """検証済みのスマホ検針結果。ヘッダ値はシート由来の参考情報。"""

    sheet_id: str
    entries: tuple[MeterReadingSubmission, ...]
    business_date: date | None = None
    generated_at: str | None = None
    filter: SheetFilter | None = None

    def to_sync_request(self) -> MeterReadingSyncRequest:
        return MeterReadingSyncRequest(reading_items=list(self.entries), replacement_items=[])

    def to_confirmation_context(
        self, *, staff_id: str, staff_name: str
    ) -> ReadingConfirmationContext:
        return ReadingConfirmationContext(
            sheet_id=self.sheet_id, staff_id=staff_id, staff_name=staff_name
        )


def parse_mobile_reading_result(raw_text: object) -> MobileReadingResult:
    """MR1 テキストまたは結果 JSON を検証し、保存要求へ変換できる形にする。"""

    payload = extract_mr1_json_object(raw_text)
    if payload.get("kind") != RESULT_KIND:
        raise ValidationError("検針結果データではありません")
    version = payload.get("v")
    if isinstance(version, bool) or version != PAYLOAD_VERSION:
        raise ValidationError(f"未対応の結果データ版数です: {version!r}")
    unknown = sorted(set(payload) - _RESULT_TOP_LEVEL_KEYS)
    if unknown:
        raise ValidationError(f"結果データに未知の項目があります: {', '.join(unknown)}")
    missing = sorted(_RESULT_REQUIRED_KEYS - set(payload))
    if missing:
        raise ValidationError(f"結果データに必須項目がありません: {', '.join(missing)}")

    sheet_id = validate_sheet_id(payload["sheet_id"])
    entries = _parse_entries(payload["entries"])
    return MobileReadingResult(
        sheet_id=sheet_id,
        entries=entries,
        business_date=_parse_header_date(payload.get("business_date")),
        generated_at=_parse_header_timestamp(payload.get("generated_at")),
        filter=SheetFilter.from_json(payload["filter"]) if "filter" in payload else None,
    )


def _parse_header_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("結果データの対象業務日の形式が正しくありません")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValidationError("結果データの対象業務日の形式が正しくありません") from error
    if parsed.isoformat() != value:
        raise ValidationError("結果データの対象業務日の形式が正しくありません")
    return parsed


def _parse_header_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("結果データの出力日時の形式が正しくありません")
    try:
        parse_utc(value)
    except ValueError as error:
        raise ValidationError(
            f"結果データの出力日時は {DB_DATETIME_FORMAT} 形式で指定してください"
        ) from error
    return value


def _parse_entries(value: object) -> tuple[MeterReadingSubmission, ...]:
    if not isinstance(value, list) or not value:
        raise ValidationError("結果データに検針結果が 1 件以上必要です")
    submissions = [
        _parse_entry(entry, index) for index, entry in enumerate(cast(list[object], value))
    ]
    seen: set[str] = set()
    for submission in submissions:
        if submission.planned_reading_id in seen:
            raise ValidationError(
                f"同じ予定枠の検針結果が重複しています: {submission.planned_reading_id}"
            )
        seen.add(submission.planned_reading_id)
    return tuple(submissions)


def _parse_entry(value: object, index: int) -> MeterReadingSubmission:
    label = f"検針結果 {index + 1} 件目"
    if not isinstance(value, dict):
        raise ValidationError(f"{label}の形式が正しくありません")
    entry = cast(dict[str, object], value)
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        raise ValidationError(f"{label}に未知の項目があります: {', '.join(unknown)}")
    missing = sorted(_ENTRY_REQUIRED_KEYS - set(entry))
    if missing:
        raise ValidationError(f"{label}に必須項目がありません: {', '.join(missing)}")

    for key in ("planned_reading_id", "result_status", "recorded_at", "client_updated_at"):
        if not isinstance(entry[key], str):
            raise ValidationError(f"{label}の {key} は文字列で指定してください")
    for key in ("raw_reading", "note"):
        if entry.get(key) is not None and not isinstance(entry[key], str):
            raise ValidationError(f"{label}の {key} は文字列で指定してください")
    structured_payload = entry.get("structured_payload")
    if structured_payload is not None and not isinstance(structured_payload, dict):
        raise ValidationError(f"{label}の structured_payload はオブジェクトで指定してください")
    if entry["result_status"] not in _ACCEPTED_RESULT_STATUSES:
        raise ValidationError(f"{label}の結果状態が正しくありません: {entry['result_status']}")

    try:
        return MeterReadingSubmission.model_validate(
            {
                "planned_reading_id": entry["planned_reading_id"],
                "result_status": entry["result_status"],
                "raw_reading": entry.get("raw_reading"),
                "structured_payload": structured_payload,
                "recorded_at": _normalize_timestamp(entry["recorded_at"], label),
                "client_updated_at": _normalize_timestamp(entry["client_updated_at"], label),
                "note": validate_note(entry.get("note")),
            }
        )
    except PydanticValidationError as error:
        issues = error.errors(include_url=False)
        detail = "; ".join(str(issue["msg"]) for issue in issues) or str(error)
        raise ValidationError(f"{label}が正しくありません: {detail}") from error


def _normalize_timestamp(value: object, label: str) -> datetime:
    """オフセット付き ISO 8601 を UTC 秒単位へ正規化する。"""

    try:
        parsed = datetime.fromisoformat(cast(str, value))
    except ValueError as error:
        raise ValidationError(f"{label}の日時が ISO 8601 形式ではありません") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{label}の日時にタイムゾーンのオフセットが必要です")
    return parsed.astimezone(UTC).replace(microsecond=0)
