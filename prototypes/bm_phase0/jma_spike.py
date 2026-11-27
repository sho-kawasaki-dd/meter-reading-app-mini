from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SAMPLE_DIR = ROOT / "jma_samples"
USER_AGENT = "building-manager-mini BM-Phase0 JMA page sampling"
EXPECTED = {
    "kyoto": {
        "station": "京都",
        "page": "daily_s1.php",
        "block_no": "47759",
        "fields": {
            "weather_day": ("天気概況", "昼"),
            "weather_night": ("天気概況", "夜"),
            "temperature_max": ("気温", "最高"),
            "temperature_min": ("気温", "最低"),
            "humidity_average": ("湿度", "平均"),
            "humidity_minimum": ("湿度", "最小"),
        },
        "not_observed_fields": [],
    },
    "kyotanabe": {
        "station": "京田辺",
        "page": "daily_a1.php",
        "block_no": "0598",
        "fields": {
            "temperature_max": ("気温", "最高"),
            "temperature_min": ("気温", "最低"),
            "humidity_average": ("湿度", "平均"),
            "humidity_minimum": ("湿度", "最小"),
        },
        "not_observed_fields": ["weather_day", "weather_night"],
    },
}
QUALITY_ONLY = {"×", "///", "--", "･･･", "…"}


def build_urls(year: int, month: int) -> list[tuple[str, str, str]]:
    months = [(year, month - 1), (year, month)]
    if month == 1:
        months = [(year - 1, 12), (year, month)]
    urls: list[tuple[str, str, str]] = []
    for location, config in EXPECTED.items():
        for sample_year, sample_month in months:
            query = urlencode(
                {
                    "prec_no": "61",
                    "block_no": config["block_no"],
                    "year": sample_year,
                    "month": sample_month,
                    "day": "",
                    "view": "",
                }
            )
            urls.append(
                (
                    location,
                    f"{sample_year}-{sample_month:02}",
                    f"https://www.data.jma.go.jp/stats/etrn/view/{config['page']}?{query}",
                )
            )
    return urls


def fetch_samples(year: int, month: int) -> int:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = SAMPLE_DIR / "manifest.json"
    urls = build_urls(year, month)
    if manifest_path.exists():
        manifest: list[dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = []
    attempted_urls = {entry["url"] for entry in manifest if entry.get("attempts", 0) > 0}
    new_urls = [item for item in urls if item[2] not in attempted_urls]
    if not new_urls:
        raise RuntimeError(
            "All requested URLs were already attempted; no network request was made."
        )
    if len(new_urls) != len(urls):
        print(f"skipped {len(urls) - len(new_urls)} previously attempted URL(s)")

    for index, (location, period, url) in enumerate(new_urls):
        if index:
            time.sleep(3)
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html",
                "Accept-Encoding": "identity",
            },
        )
        entry: dict[str, Any] = {
            "location": location,
            "period": period,
            "url": url,
            "source": "気象庁「過去の気象データ検索」の日ごとの値",
            "requested_at_utc": datetime.now(UTC).isoformat(),
            "user_agent": USER_AGENT,
            "attempts": 1,
            "status": "in_progress",
        }
        manifest.append(entry)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read()
                charset_match = re.search(
                    r"(?:^|;)\s*charset=([^;]+)",
                    response.headers.get("Content-Type", ""),
                    re.IGNORECASE,
                )
                charset = charset_match.group(1).strip("\"' ") if charset_match else None
                if response.status != 200:
                    entry.update(status="http_error", http_status=response.status)
                else:
                    filename = f"{location}_{period}.html"
                    (SAMPLE_DIR / filename).write_bytes(body)
                    entry.update(
                        status="saved",
                        http_status=response.status,
                        final_url=response.geturl(),
                        filename=filename,
                        retrieved_at_utc=datetime.now(UTC).isoformat(),
                        content_type=response.headers.get("Content-Type"),
                        charset=charset,
                        bytes=len(body),
                        sha256=hashlib.sha256(body).hexdigest(),
                    )
        except HTTPError as error:
            entry.update(status="http_error", http_status=error.code, error=str(error))
        except (URLError, TimeoutError, OSError) as error:
            entry.update(status="request_error", error=str(error))
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"{entry['status']}: {url}")
    failures = [item for item in manifest if item["status"] != "saved"]
    print(f"saved {len(manifest) - len(failures)}/{len(manifest)}; failed {len(failures)}")
    return 1 if failures else 0


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("（", "(").replace("）", ")")


def text_of(cell: Any) -> str:
    return cell.get_text(" ", strip=True).replace("\xa0", " ").strip()


def table_headers(rows: list[Any], data_row_index: int) -> dict[int, str]:
    grid: list[dict[int, str]] = []
    occupied: dict[int, tuple[int, str]] = {}
    for row_index, row in enumerate(rows[:data_row_index]):
        current: dict[int, str] = {}
        column = 0
        for cell in row.find_all(["th", "td"], recursive=False):
            while column in occupied and occupied[column][0] >= row_index:
                current[column] = occupied[column][1]
                column += 1
            label = text_of(cell)
            colspan = int(cell.get("colspan", 1))
            rowspan = int(cell.get("rowspan", 1))
            for offset in range(colspan):
                position = column + offset
                current[position] = label
                if rowspan > 1:
                    occupied[position] = (row_index + rowspan - 1, label)
            column += colspan
        for position, (last_row, label) in list(occupied.items()):
            if position not in current and last_row >= row_index:
                current[position] = label
            if last_row < row_index:
                del occupied[position]
        grid.append(current)
    width = max((max(row, default=-1) for row in grid), default=-1) + 1
    result: dict[int, str] = {}
    for column in range(width):
        labels = []
        for row in grid:
            label = row.get(column, "")
            if label and (not labels or labels[-1] != label):
                labels.append(label)
        result[column] = " ".join(labels)
    return result


def is_data_row(row: Any) -> bool:
    cells = row.find_all(["th", "td"], recursive=False)
    if len(cells) < 2:
        return False
    first = normalize(text_of(cells[0]))
    return re.fullmatch(r"\d{1,2}", first) is not None


def match_columns(headers: dict[int, str], location: str) -> dict[str, int]:
    fields = EXPECTED[location]["fields"]
    result: dict[str, int] = {}
    for field, (group, label) in fields.items():
        candidates = [
            column
            for column, header in headers.items()
            if normalize(group) in normalize(header) and normalize(label) in normalize(header)
        ]
        if len(candidates) != 1:
            raise ValueError(f"{field} の列を一意に特定できません: {candidates}")
        result[field] = candidates[0]
    return result


def parse_value(raw: str) -> dict[str, Any]:
    value = normalize(raw)
    if not value:
        return {"raw": raw, "value": None, "quality": None, "status": "blank"}
    if value in QUALITY_ONLY:
        return {"raw": raw, "value": None, "quality": value, "status": "quality_only"}
    match = re.fullmatch(r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))([)\]×*#?]*)", value)
    if match:
        try:
            number = Decimal(match.group(1))
        except InvalidOperation as error:
            raise ValueError(f"数値を Decimal に変換できません: {raw!r}") from error
        if not number.is_finite():
            raise ValueError(f"有限でない数値です: {raw!r}")
        return {
            "raw": raw,
            "value": str(number),
            "quality": match.group(2) or None,
            "status": "value",
        }
    for marker in (")", "]"):
        if value.endswith(marker):
            return {
                "raw": raw,
                "value": value[:-1],
                "quality": marker,
                "status": "text",
            }
    return {"raw": raw, "value": None, "quality": None, "status": "text"}


def parse_page(
    html: bytes,
    location: str,
    year: int,
    month: int,
    source_url: str,
    encoding: str = "UTF-8",
) -> dict[str, Any]:
    config = EXPECTED[location]
    soup = BeautifulSoup(html, "html.parser", from_encoding=encoding)
    heading = normalize(
        " ".join(tag.get_text(" ", strip=True) for tag in soup.find_all(["h1", "h2", "h3"]))
    )
    if config["station"] not in heading or "京都府" not in heading:
        raise ValueError(f"想定外の地点です: {config['station']}")
    if f"{year}年{month}月" not in heading or "日ごとの値" not in heading:
        raise ValueError(f"想定外の年・月またはページ種類です: {year}-{month:02}")

    selected: tuple[Any, int, dict[int, str]] | None = None
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for index, row in enumerate(rows):
            if is_data_row(row):
                selected = (table, index, table_headers(rows, index))
                break
        if selected:
            break
    if selected is None:
        raise ValueError("日別データ表がありません")

    table, first_data_index, headers = selected
    columns = match_columns(headers, location)
    records: list[dict[str, Any]] = []
    rows = table.find_all("tr")
    for row in rows[first_data_index:]:
        if not is_data_row(row):
            continue
        cells = row.find_all(["th", "td"], recursive=False)
        day = int(normalize(text_of(cells[0])))
        values: dict[str, Any] = {}
        for field, column in columns.items():
            if column >= len(cells):
                raise ValueError(f"{day} 日の {field} セルがありません")
            values[field] = parse_value(text_of(cells[column]))
        records.append({"date": f"{year:04}-{month:02}-{day:02}", "fields": values})

    if not records:
        raise ValueError("日別データ行がありません")
    return {
        "location": location,
        "station": config["station"],
        "year": year,
        "month": month,
        "source_url": source_url,
        "headers": {str(column): label for column, label in headers.items()},
        "columns": columns,
        "not_observed_fields": config["not_observed_fields"],
        "records": records,
    }


def day_state(report: dict[str, Any], day: int) -> dict[str, Any]:
    date = f"{report['year']:04}-{report['month']:02}-{day:02}"
    record = next((item for item in report["records"] if item["date"] == date), None)
    if record is None:
        return {"date": date, "status": "pending_unpublished"}
    return {"date": date, "status": "published", "fields": record["fields"]}


def remove_day_row(html: bytes, day: int) -> bytes:
    soup = BeautifulSoup(html, "html.parser", from_encoding="UTF-8")
    matches = []
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if (
            cells
            and normalize(text_of(cells[0])) == str(day)
            and row.find_parent("table", id="tablefix1")
        ):
            matches.append(row)
    if len(matches) != 1:
        raise ValueError(f"{day} 日の行を一意に加工できません: {len(matches)}")
    matches[0].decompose()
    return soup.encode("utf-8")


def expect_invalid_page(html: bytes, location: str, year: int, month: int, label: str) -> None:
    try:
        parse_page(html, location, year, month, "offline-test")
    except ValueError:
        print(f"invalid page rejected: {label}")
        return
    raise AssertionError(f"異常ページを受理しました: {label}")


def verify_samples() -> int:
    manifest_path = SAMPLE_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures = [entry for entry in manifest if entry["status"] != "saved"]
    if failures:
        raise ValueError(f"取得に失敗したページがあります: {failures}")
    urls = [entry["url"] for entry in manifest]
    if len(urls) != len(set(urls)) or any(entry["attempts"] != 1 for entry in manifest):
        raise ValueError("manifest に重複 URL または複数回試行した URL があります")
    current_period = max(entry["period"] for entry in manifest)
    quality_cases = {
        "12.3)": ("12.3", ")"),
        "12.3]": ("12.3", "]"),
        "晴)": ("晴", ")"),
        "///": (None, "///"),
        "": (None, None),
    }
    for raw, expected in quality_cases.items():
        parsed = parse_value(raw)
        if (parsed["value"], parsed["quality"]) != expected:
            raise AssertionError(f"品質記号の分離に失敗しました: {raw!r}")

    reports = []
    for entry in manifest:
        period_year, period_month = map(int, entry["period"].split("-"))
        raw = (SAMPLE_DIR / entry["filename"]).read_bytes()
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"保存試料のサイズまたは SHA-256 が不一致です: {entry['filename']}")
        report = parse_page(
            raw,
            entry["location"],
            period_year,
            period_month,
            entry["url"],
            entry.get("charset") or "UTF-8",
        )
        current_day = 28
        day_28 = day_state(report, current_day) if entry["period"] == current_period else None
        reports.append(
            {
                "location": report["location"],
                "period": entry["period"],
                "columns": report["columns"],
                "not_observed_fields": report["not_observed_fields"],
                "days": len(report["records"]),
                "example": report["records"][0],
                "quality_symbols": sorted(
                    {
                        field["quality"]
                        for record in report["records"]
                        for field in record["fields"].values()
                        if field["quality"]
                    }
                ),
                "blank_count": sum(
                    field["status"] == "blank"
                    for record in report["records"]
                    for field in record["fields"].values()
                ),
                "day_28_state": day_28,
            }
        )

    processed_dir = SAMPLE_DIR / "processed"
    processed_dir.mkdir(exist_ok=True)
    current_entries = [entry for entry in manifest if entry["period"] == current_period]
    processed_results = []
    for entry in current_entries:
        raw = (SAMPLE_DIR / entry["filename"]).read_bytes()
        year, month = map(int, entry["period"].split("-"))
        original = parse_page(
            raw,
            entry["location"],
            year,
            month,
            entry["url"],
            entry.get("charset") or "UTF-8",
        )
        if day_state(original, 28)["status"] != "published":
            raise AssertionError(f"{entry['location']} の実ページに28日行がありません")
        modified = remove_day_row(raw, 28)
        filename = f"{entry['location']}_{current_period}_missing-day-28.html"
        (processed_dir / filename).write_bytes(modified)
        processed = parse_page(
            modified,
            entry["location"],
            year,
            month,
            entry["url"],
        )
        if day_state(processed, 28)["status"] != "pending_unpublished":
            raise AssertionError(f"{entry['location']} の加工試料を未掲載と判定できません")
        processed_results.append(
            {
                "location": entry["location"],
                "filename": filename,
                "modified_day_state": day_state(processed, 28)["status"],
                "source_filename": entry["filename"],
            }
        )

    sample_entry = next(item for item in manifest if item["location"] == "kyoto")
    sample_year, sample_month = map(int, sample_entry["period"].split("-"))
    sample_bytes = (SAMPLE_DIR / sample_entry["filename"]).read_bytes()
    expect_invalid_page(
        "<!doctype html><title>メンテナンス</title>".encode(),
        "kyoto",
        sample_year,
        sample_month,
        "maintenance page",
    )
    expect_invalid_page(
        sample_bytes.replace("京都".encode(), "大阪".encode()),
        "kyoto",
        2026,
        10,
        "different station",
    )
    missing_header = (
        '<th rowspan="2" scope="col">平均</th>'
        '<th rowspan="2" scope="col">最小</th>'
        '<th rowspan="2" scope="col">平均<br />風速</th>'
    ).encode()
    replacement_header = (
        '<th rowspan="2" scope="col">最小</th><th rowspan="2" scope="col">平均<br />風速</th>'
    ).encode()
    stripped_header = sample_bytes.replace(missing_header, replacement_header, 1)
    if stripped_header == sample_bytes:
        raise AssertionError("異常試料の見出し加工に失敗しました")
    expect_invalid_page(
        stripped_header,
        "kyoto",
        sample_year,
        sample_month,
        "missing required column",
    )

    processed_readme = processed_dir / "README.md"
    processed_readme.write_text(
        "# 加工試料\n\n"
        f"このディレクトリの HTML は、隣接する `{current_period}` の実ページから "
        "28 日の行だけを削除し、"
        "BeautifulSoup で UTF-8 に再シリアライズしたものです。追加の HTTP 取得は行っていません。"
        "元ページはこのディレクトリの親に保持しています。加工試料は対象日が未掲載の場合の判定検証専用です。\n",
        encoding="utf-8",
    )
    print(json.dumps(reports, ensure_ascii=True, indent=2))
    print(
        json.dumps(
            {"processed_missing_day_samples": processed_results},
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BM-Phase 0 気象庁日別値ページの試作")
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("--year", type=int, required=True)
    fetch_parser.add_argument("--month", type=int, choices=range(1, 13), required=True)
    subparsers.add_parser("verify")
    args = parser.parse_args()
    if args.command == "fetch":
        return fetch_samples(args.year, args.month)
    return verify_samples()


if __name__ == "__main__":
    raise SystemExit(main())
