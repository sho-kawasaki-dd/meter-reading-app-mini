from __future__ import annotations

import csv
import io
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from meter_reading_mini.core.aggregation_contracts import (
    AggregationApplication,
    AggregationBaseline,
    AggregationBaselineSource,
    AggregationCode,
    AggregationDetail,
    AggregationDetailSource,
    AggregationFormulaTerm,
    AggregationFormulaVersionRecord,
    AggregationIssue,
    AggregationPeriod,
    AggregationPeriodUnit,
    AggregationReport,
    AggregationResult,
    AggregationTargetType,
)
from meter_reading_mini.core.contract_models import Category, Purpose
from meter_reading_mini.core.errors import FileOutputError, ValidationError
from meter_reading_mini.ui import aggregation_export, atomic_write
from meter_reading_mini.ui.aggregation_display import display_value
from meter_reading_mini.ui.aggregation_export import CSV_HEADERS, aggregation_csv

DAY = date(2026, 2, 1)
NOW = datetime(2026, 2, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "places", "expected"),
    [
        ("12.3450", None, "12.3450"),
        ("1.005", 2, "1.01"),
        ("-1.005", 2, "-1.01"),
        ("-0.005", 2, "-0.01"),
        ("999999999999999999999999999999999999.995", 2, "1000000000000000000000000000000000000.00"),
        ("0", 0, "0"),
    ],
)
def test_display_value_uses_half_up_without_global_precision(
    raw: str, places: int | None, expected: str
) -> None:
    assert display_value(Decimal(raw), places) == expected


def _report(*, multiple_versions: bool = True) -> AggregationReport:
    terms = (AggregationFormulaTerm("@definition", Decimal("-1.5"), 0),)
    version = AggregationFormulaVersionRecord(
        "-formula", DAY, True, True, NOW, date(2026, 2, 3), terms
    )
    next_version = AggregationFormulaVersionRecord(
        "-formula", date(2026, 2, 3), True, False, NOW, DAY, terms
    )
    issues = (
        AggregationIssue(AggregationCode.MISSING_UNMEASURED, "未実測"),
        AggregationIssue(AggregationCode.MISSING_PARTIAL_ITEMS, "一部のみ"),
        AggregationIssue(AggregationCode.WARN_NEGATIVE, "負数"),
        AggregationIssue(AggregationCode.INFO_RETROACTIVE_FORMULA, "後付け"),
        AggregationIssue(AggregationCode.INFO_MULTI_LOCATION, "複数場所"),
        AggregationIssue(AggregationCode.WARN_PERIOD_MISMATCH, "検針期間不一致"),
    )
    detail = AggregationDetail(
        source=AggregationDetailSource.READING,
        source_id="@reading",
        measurement_definition_id="@definition",
        location_id="-location",
        business_date=date(2026, 2, 2),
        at=NOW,
        status="measured",
        baseline=AggregationBaseline(
            AggregationBaselineSource.READING, "baseline", DAY, NOW, Decimal("10")
        ),
        source_usage=Decimal("2.555"),
        coefficient=Decimal("-1.5"),
        contribution=Decimal("-3.8325"),
        version_effective_from=DAY,
        retroactive=True,
        issues=(issues[0], issues[3]),
    )
    applications = (AggregationApplication(DAY, date(2026, 2, 3), version, Decimal("-3.8325")),)
    if multiple_versions:
        applications += (AggregationApplication(date(2026, 2, 3), date(2026, 2, 4), next_version),)
    result = AggregationResult(
        target_type=AggregationTargetType.FORMULA,
        target_id="-formula",
        display_name="=危険な式",
        purpose=Purpose.OPERATIONAL,
        category=Category.ELECTRICITY,
        unit="+kWh",
        decimal_places=2,
        location_ids=("-location", "+location"),
        value=Decimal("-3.8325"),
        details=(detail,),
        issues=issues,
        applications=applications,
    )
    period = AggregationPeriod(
        AggregationPeriodUnit.MONTH,
        DAY,
        date(2026, 2, 4),
        DAY,
        date(2026, 3, 1),
        True,
        (result,),
    )
    return AggregationReport(
        DAY, date(2026, 3, 1), date(2026, 2, 3), Purpose.OPERATIONAL, (period,)
    )


def test_csv_fixed_shape_versions_precision_flags_and_formula_injection() -> None:
    text = aggregation_csv(_report(), AggregationTargetType.FORMULA, "-formula")
    assert text.startswith("\ufeff")
    rows = list(csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline="")))
    assert rows[0] == list(CSV_HEADERS)
    assert all(len(row) == len(CSV_HEADERS) == 30 for row in rows)
    summary, detail = (dict(zip(CSV_HEADERS, row, strict=True)) for row in rows[1:])
    assert summary["行種別"] == "集計"
    assert summary["期間終了業務日_含まない"] == "2026-02-04"
    assert summary["対象ID"] == "'-formula"
    assert summary["対象名"] == "'=危険な式"
    assert summary["単位"] == "'+kWh"
    assert summary["場所ID"] == "'+location;-location"
    assert summary["式版開始業務日"] == summary["後付け設定"] == ""
    assert all(
        summary[column] == ""
        for column in (
            "測定定義ID",
            "係数",
            "明細元",
            "明細ID",
            "基準業務日",
            "実測業務日",
            "元使用量_無丸め",
        )
    )
    assert summary["表示値"] == "-3.83"
    assert summary["無丸め値"] == "-3.8325"
    assert summary["欠測系"] == "MISSING_UNMEASURED;MISSING_PARTIAL_ITEMS"
    assert summary["警告"] == "WARN_NEGATIVE;WARN_PERIOD_MISMATCH"
    assert summary["情報"] == "INFO_RETROACTIVE_FORMULA;INFO_MULTI_LOCATION"
    assert summary["集計状態"] == "参考値 / 進行中"
    assert summary["式適用区間"] == (
        "2026-02-01/2026-02-03（版:2026-02-01）;2026-02-03/2026-02-04（版:2026-02-03）"
    )
    assert detail["行種別"] == "明細"
    assert detail["場所ID"] == "'-location"
    assert detail["式版開始業務日"] == "2026-02-01"
    assert detail["後付け設定"] == "はい"
    assert detail["測定定義ID"] == "'@definition"
    assert detail["明細ID"] == "'@reading"
    assert detail["係数"] == "-1.5"
    assert detail["基準業務日"] == "2026-02-01"
    assert detail["実測業務日"] == "2026-02-02"
    assert detail["元使用量_無丸め"] == "2.555"
    assert detail["表示値"] == "-3.83"
    assert detail["無丸め値"] == "-3.8325"
    assert detail["警告"] == ""
    assert detail["式適用区間"] == "2026-02-01/2026-02-03（版:2026-02-01）"
    assert detail["基準元"] == "前回実測"
    assert detail["基準ID"] == "baseline"
    assert detail["基準読値_無丸め"] == "10"


def test_csv_single_version_and_atomic_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "集計.csv"
    aggregation_export.save_aggregation_csv(
        path, _report(multiple_versions=False), AggregationTargetType.FORMULA, "-formula"
    )
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline="")))
    assert rows[0]["式版開始業務日"] == "2026-02-01"
    assert rows[0]["後付け設定"] == "はい"
    assert list(tmp_path.iterdir()) == [path]


def test_csv_rejects_missing_target() -> None:
    with pytest.raises(ValidationError, match="出力対象"):
        aggregation_csv(_report(), AggregationTargetType.FORMULA, "missing")


def test_csv_preserves_unapplied_period_and_distinguishes_empty_from_zero() -> None:
    report = _report()
    period = report.periods[0]
    unapplied = replace(
        period, start=date(2026, 3, 1), end=date(2026, 3, 2), results=(), is_in_progress=False
    )
    text = aggregation_csv(
        replace(report, periods=(period, unapplied)), AggregationTargetType.FORMULA, "-formula"
    )
    rows = list(csv.DictReader(io.StringIO(text.removeprefix("\ufeff"), newline="")))
    last = rows[-1]
    assert last["集計状態"] == "適用なし"
    assert last["場所ID"] == last["無丸め値"] == last["式適用区間"] == ""
    assert len(last) == len(CSV_HEADERS)

    original = period.results[0]
    empty = replace(
        original,
        target_type=AggregationTargetType.DEFINITION,
        target_id="definition",
        value=None,
        details=(),
        issues=(),
        applications=(),
    )
    zero = replace(empty, value=Decimal("0"))
    text = aggregation_csv(
        replace(
            report,
            periods=(replace(period, results=(empty,)), replace(unapplied, results=(zero,))),
        ),
        AggregationTargetType.DEFINITION,
        "definition",
    )
    rows = list(csv.DictReader(io.StringIO(text.removeprefix("\ufeff"), newline="")))
    assert rows[0]["集計状態"] == "予定なし / 進行中"
    assert rows[0]["無丸め値"] == ""
    assert rows[0]["式版開始業務日"] == rows[0]["後付け設定"] == ""
    assert rows[1]["集計状態"] == "通常"
    assert rows[1]["無丸め値"] == "0"
    assert all(row[column] == "" for row in rows for column in ("欠測系", "警告", "情報"))


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_escapes_all_dangerous_string_prefixes(prefix: str) -> None:
    report = _report()
    period = report.periods[0]
    changed = replace(period.results[0], display_name=prefix + "値")
    text = aggregation_csv(
        replace(report, periods=(replace(period, results=(changed,)),)),
        AggregationTargetType.FORMULA,
        "-formula",
    )
    rows = list(csv.DictReader(io.StringIO(text.removeprefix("\ufeff"), newline="")))
    assert rows[0]["対象名"] == "'" + prefix + "値"


def test_csv_write_failure_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "集計.csv"
    path.write_text("元データ", encoding="utf-8")

    def fail(_path: Path, _text: str) -> None:
        raise OSError("書き込み失敗")

    monkeypatch.setattr(aggregation_export, "atomic_write_text", fail)
    with pytest.raises(FileOutputError, match="保存できません"):
        aggregation_export.save_aggregation_csv(
            path, _report(), AggregationTargetType.FORMULA, "-formula"
        )
    assert path.read_text(encoding="utf-8") == "元データ"


def test_csv_replace_failure_preserves_existing_file_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "集計.csv"
    path.write_text("元データ", encoding="utf-8")

    def fail(_source: object, _target: object) -> None:
        raise OSError("置換失敗")

    monkeypatch.setattr(atomic_write.os, "replace", fail)
    with pytest.raises(FileOutputError, match="保存できません"):
        aggregation_export.save_aggregation_csv(
            path, _report(), AggregationTargetType.FORMULA, "-formula"
        )
    assert path.read_text(encoding="utf-8") == "元データ"
    assert list(tmp_path.iterdir()) == [path]
