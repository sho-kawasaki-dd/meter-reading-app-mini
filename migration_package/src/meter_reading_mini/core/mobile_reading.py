from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from collections.abc import Mapping
from typing import cast

from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.timeutil import to_local

RESULT_CODE_PREFIX = "MR1:"
RESULT_CODE_SUFFIX = ":END"
DRAFT_KIND = "mrm-meter-reading-draft"
MAX_NOTE_LENGTH = 500
PAYLOAD_VERSION = 1

_SHEET_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_LEADING_QUOTE_PATTERN = re.compile(r"^[ \t>]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")

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


def format_sheet_timestamps(utc_str: str) -> tuple[str, str]:
    try:
        local_dt = to_local(utc_str)
        return (
            local_dt.strftime("%Y-%m-%d %H:%M:%S"),
            local_dt.strftime("%Y%m%d_%H%M%S"),
        )
    except ValueError:
        sanitized = re.sub(r"[^0-9A-Za-z]+", "", utc_str) or "result"
        return utc_str, sanitized


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
