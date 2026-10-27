from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest
from conftest import SheetBrowser
from mobile_sheet_helpers import definition, meter, sheet_html, simple_sheet_html, slot
from playwright.sync_api import Locator, Page, expect

from meter_reading_mini.core.decimal_arithmetic import exact_multiply, exact_subtract

VIEWPORTS = {
    "phone-portrait": (360, 640),
    "phone-landscape": (640, 360),
    "pc": (1366, 768),
}


def card(page: Page, slot_id: str) -> Locator:
    return page.locator(f'.card[data-slot-id="{slot_id}"]')


def enter_and_confirm(page: Page, slot_id: str, value: str) -> None:
    target = card(page, slot_id)
    target.locator(".raw").fill(value)
    target.locator(".confirm").click()


def open_all_tab(sheet_browser: SheetBrowser, html: str) -> Page:
    page = sheet_browser.open_html(html)
    page.locator('.tab[data-tab="all"]').click()
    return page


def tab_labels(page: Page) -> list[str]:
    return [text.strip() for text in page.locator(".tab").all_text_contents()]


def visible_slot_ids(page: Page) -> list[str]:
    return page.locator(".card:visible").evaluate_all(
        "nodes => nodes.map(node => node.dataset.slotId)"
    )


def section_titles(page: Page) -> list[str]:
    return [
        text.strip()
        for text in page.locator(".location:visible .location-toggle").all_text_contents()
    ]


def multi_session_html() -> str:
    meters = [meter("meter-d"), meter("meter-w"), meter("meter-m")]
    definitions = [
        definition("def-d", display_name="日次メーター"),
        definition("def-w", display_name="週次メーター"),
        definition("def-m", display_name="月次メーター"),
    ]
    slots = [
        slot("d-0800", "def-d", "meter-d", session="08:00"),
        slot("d-1400", "def-d", "meter-d", session="14:00"),
        slot("d-2200", "def-d", "meter-d", session="22:00"),
        slot("w-1400", "def-w", "meter-w", session="14:00", cycle="weekly"),
        slot("m-0800", "def-m", "meter-m", session="08:00", cycle="monthly"),
    ]
    return sheet_html(slots, definitions, meters)


def test_notice_and_header_are_shown(sheet_browser: SheetBrowser) -> None:
    page = sheet_browser.open_html(simple_sheet_html())

    expect(page.locator("#app .notice")).to_contain_text("Chrome で開いてください")
    expect(page.locator(".sheet-header h1")).to_have_text("検針シート 2026-02-04")
    expect(page.locator(".sheet-meta")).to_contain_text("出力日時 2026-02-04 12:00:00")


def test_tabs_group_sessions_and_non_daily_slots_with_progress_badges(
    sheet_browser: SheetBrowser,
) -> None:
    page = sheet_browser.open_html(multi_session_html())

    assert tab_labels(page) == [
        "すべて (0/5)",
        "08:00 (0/1)",
        "14:00 (0/1)",
        "22:00 (0/1)",
        "週次・月次・隔月 (0/2)",
    ]
    expect(page.locator('.tab[data-tab="s:08:00"]')).to_have_attribute("aria-selected", "true")
    assert visible_slot_ids(page) == ["d-0800"]

    page.locator('.tab[data-tab="s:14:00"]').click()
    assert visible_slot_ids(page) == ["d-1400"]

    page.locator('.tab[data-tab="nondaily"]').click()
    assert visible_slot_ids(page) == ["m-0800", "w-1400"] or visible_slot_ids(page) == [
        "w-1400",
        "m-0800",
    ]
    assert sorted(visible_slot_ids(page)) == ["m-0800", "w-1400"]

    page.locator('.tab[data-tab="all"]').click()
    assert sorted(visible_slot_ids(page)) == sorted(
        ["d-0800", "d-1400", "d-2200", "w-1400", "m-0800"]
    )

    enter_and_confirm(page, "d-0800", "10")
    assert tab_labels(page)[:2] == ["すべて (1/5)", "08:00 (1/1)"]


def test_initial_tab_is_non_daily_when_only_non_daily_slots_exist(
    sheet_browser: SheetBrowser,
) -> None:
    page = sheet_browser.open_html(
        sheet_html(
            [slot("w-1", "def-w", "meter-w", cycle="weekly")],
            [definition("def-w")],
            [meter("meter-w")],
        )
    )

    expect(page.locator('.tab[data-tab="nondaily"]')).to_have_attribute("aria-selected", "true")
    assert tab_labels(page) == ["すべて (0/1)", "週次・月次・隔月 (0/1)"]


def location_sheet_html() -> str:
    meters = [
        meter("m-water", location_id="loc-b", location_name="ボイラー室", category="water"),
        meter("m-gas", location_id="loc-b", location_name="ボイラー室", category="gas"),
        meter("m-elec", location_id="loc-b", location_name="ボイラー室", category="electricity"),
        meter("m-misc", location_id="loc-b", location_name="ボイラー室", category="others"),
        meter("m-a", location_id="loc-a", location_name="受変電室", category="water"),
        meter("m-none", location_id=None, location_name="未設定", category="water"),
    ]
    definitions = [
        definition("d-water-b", location_id="loc-b", display_name="水 B", category="water"),
        definition("d-water-a", location_id="loc-b", display_name="水 A", category="water"),
        definition("d-gas", location_id="loc-b", display_name="ガス", category="gas"),
        definition("d-elec", location_id="loc-b", display_name="電力", category="electricity"),
        definition("d-misc", location_id="loc-b", display_name="その他", category="others"),
        definition("d-a", location_id="loc-a", display_name="受変電 水", category="water"),
        definition("d-none", location_id=None, display_name="場所なし", category="water"),
    ]
    slots = [
        slot("s-water-b", "d-water-b", "m-water"),
        slot("s-water-a", "d-water-a", "m-water"),
        slot("s-gas", "d-gas", "m-gas"),
        slot("s-elec", "d-elec", "m-elec"),
        slot("s-misc", "d-misc", "m-misc"),
        slot("s-a", "d-a", "m-a"),
        slot("s-none", "d-none", "m-none"),
    ]
    return sheet_html(slots, definitions, meters)


def test_slots_are_grouped_by_location_in_category_then_name_order(
    sheet_browser: SheetBrowser,
) -> None:
    page = sheet_browser.open_html(location_sheet_html())

    assert section_titles(page) == ["ボイラー室", "受変電室", "場所未設定"]
    boiler = page.locator('.location[data-location-id="loc-b"] .card')
    assert boiler.evaluate_all("nodes => nodes.map(node => node.dataset.slotId)") == [
        "s-elec",
        "s-gas",
        "s-water-a",
        "s-water-b",
        "s-misc",
    ]
    assert page.locator('.location[data-location-id=""] .card').count() == 1


def test_location_dropdown_unentered_toggle_collapse_and_completion_badge(
    sheet_browser: SheetBrowser,
) -> None:
    page = sheet_browser.open_html(location_sheet_html())

    page.locator(".location-select").select_option("loc-a")
    assert visible_slot_ids(page) == ["s-a"]
    assert section_titles(page) == ["受変電室"]

    badge = page.locator('.location[data-location-id="loc-a"] .complete-badge')
    expect(badge).to_be_hidden()
    enter_and_confirm(page, "s-a", "5")
    expect(badge).to_be_visible()
    expect(page.locator('.location[data-location-id="loc-a"] .location-progress')).to_have_text(
        "1/1"
    )

    page.locator(".unentered-toggle").check()
    expect(page.locator(".empty-message")).to_be_visible()
    page.locator(".unentered-toggle").uncheck()
    assert visible_slot_ids(page) == ["s-a"]

    page.locator(".location-select").select_option("")
    page.locator('.location[data-location-id="loc-b"] .location-toggle').click()
    expect(card(page, "s-gas")).to_be_hidden()
    page.locator('.location[data-location-id="loc-b"] .location-toggle').click()
    expect(card(page, "s-gas")).to_be_visible()
    page.locator(".unentered-toggle").check()
    assert "s-a" not in visible_slot_ids(page)


def test_card_shows_meter_information_and_previous_reading(sheet_browser: SheetBrowser) -> None:
    html = sheet_html(
        [
            slot(
                "slot-1",
                "def-1",
                "meter-1",
                previous_source="measured",
                last_reading="123.40",
                previous_scheduled_at="2026-02-03 05:00:00",
                previous_planned_reading_id="old-1",
            )
        ],
        [definition("def-1", display_name="水道メーター", unit="m3", multiplier="10")],
        [meter("meter-1", meter_name="主水道", meter_serial="SN-001")],
    )
    page = sheet_browser.open_html(html)
    target = card(page, "slot-1")

    expect(target.locator(".card-title")).to_have_text("水道メーター")
    expect(target.locator(".card-meta")).to_contain_text("主水道 (SN-001)")
    expect(target.locator(".card-meta")).to_contain_text("単位: m3")
    expect(target.locator(".previous")).to_have_text("前回値: 123.40 m3")
    expect(target).to_have_attribute("data-previous", "123.40")


def test_result_status_controls_input_visibility_and_can_be_confirmed(
    sheet_browser: SheetBrowser,
) -> None:
    page = sheet_browser.open_html(simple_sheet_html())
    target = card(page, "slot-1")

    expect(target.locator(".confirm")).to_be_disabled()
    expect(target.locator(".status option")).to_have_text(
        ["実測", "未実施", "到達不可", "メーター故障"]
    )
    for status in ("skipped", "unreachable", "meter_fault"):
        target.locator(".status").select_option(status)
        expect(target.locator(".reading-field")).to_be_hidden()
        expect(target.locator(".confirm")).to_be_enabled()
        target.locator(".confirm").click()
        expect(target).to_have_attribute("data-confirmed", "true")
        target.locator(".status").select_option("measured")
        expect(target).to_have_attribute("data-confirmed", "false")
        expect(target.locator(".reading-field")).to_be_visible()
        expect(target.locator(".confirm")).to_be_disabled()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("abc", "0 以上の数値"),
        ("-1", "0 以上の数値"),
        ("1e3", "0 以上の数値"),
        ("1,000", "0 以上の数値"),
        ("1.234", "小数は 2 桁以内"),
        ("100", "ロールオーバー値 100 未満"),
        ("101", "ロールオーバー値 100 未満"),
    ],
)
def test_reading_validation_blocks_confirmation(
    sheet_browser: SheetBrowser, value: str, message: str
) -> None:
    page = sheet_browser.open_html(
        simple_sheet_html(definition_overrides={"rollover_value": "100"})
    )
    target = card(page, "slot-1")

    target.locator(".raw").fill(value)

    expect(target.locator(".field-error").first).to_contain_text(message)
    expect(target.locator(".confirm")).to_be_disabled()


def test_valid_boundary_values_and_edit_releases_confirmation(
    sheet_browser: SheetBrowser,
) -> None:
    page = sheet_browser.open_html(
        simple_sheet_html(definition_overrides={"rollover_value": "100"})
    )
    target = card(page, "slot-1")

    enter_and_confirm(page, "slot-1", "99.99")
    expect(target).to_have_attribute("data-confirmed", "true")
    expect(target.locator(".confirm")).to_have_text("確認を解除")

    target.locator(".raw").fill("99.98")
    expect(target).to_have_attribute("data-confirmed", "false")
    target.locator(".confirm").click()
    target.locator(".note").fill("メモ")
    expect(target).to_have_attribute("data-confirmed", "false")
    target.locator(".confirm").click()
    target.locator(".status").select_option("skipped")
    expect(target).to_have_attribute("data-confirmed", "false")

    target.locator(".status").select_option("measured")
    target.locator(".note").fill("あ" * 501)
    expect(target.locator(".field-error").last).to_contain_text("500 文字以内")
    expect(target.locator(".confirm")).to_be_disabled()
    target.locator(".note").fill("😀" * 500)
    expect(target.locator(".confirm")).to_be_enabled()


def pf_html() -> str:
    return sheet_html(
        [slot("pf-1", "def-pf", "meter-pf")],
        [definition("def-pf", value_schema="power_factor_percent_phase", unit="%")],
        [meter("meter-pf")],
    )


def test_power_factor_phase_rules(sheet_browser: SheetBrowser) -> None:
    page = sheet_browser.open_html(pf_html())
    target = card(page, "pf-1")
    percent = target.locator(".pf-percent")
    phase = target.locator(".pf-phase")
    unity = phase.locator('option[value="unity"]')

    expect(target.locator(".reading-field")).to_have_count(0)
    expect(target.locator(".confirm")).to_be_disabled()
    assert unity.evaluate("option => option.disabled")

    percent.fill("100")
    expect(phase).to_have_value("unity")
    expect(phase).to_be_disabled()
    expect(target.locator(".confirm")).to_be_enabled()
    target.locator(".confirm").click()
    expect(target).to_have_attribute("data-confirmed", "true")

    percent.fill("99.5")
    expect(target).to_have_attribute("data-confirmed", "false")
    expect(phase).to_have_value("")
    expect(phase).to_be_enabled()
    assert unity.evaluate("option => option.disabled")
    expect(target.locator(".confirm")).to_be_disabled()

    phase.select_option("lag")
    expect(target.locator(".confirm")).to_be_enabled()
    target.locator(".confirm").click()
    expect(target).to_have_attribute("data-confirmed", "true")

    for invalid in ("100.1", "abc", "-5"):
        percent.fill(invalid)
        expect(target.locator(".confirm")).to_be_disabled()
        expect(target.locator(".pf-field .field-error").first).not_to_be_empty()
    percent.fill("0")
    phase.select_option("lead")
    expect(target.locator(".confirm")).to_be_enabled()


def usage_page(
    sheet_browser: SheetBrowser,
    *,
    definition_overrides: Mapping[str, Any] | None = None,
    slot_overrides: Mapping[str, Any] | None = None,
    value: str,
) -> Locator:
    page = sheet_browser.open_html(
        simple_sheet_html(definition_overrides=definition_overrides, slot_overrides=slot_overrides)
    )
    target = card(page, "slot-1")
    target.locator(".raw").fill(value)
    return target


MEASURED_PREVIOUS = {
    "previous_source": "measured",
    "last_reading": "100.00",
    "previous_scheduled_at": "2026-02-03 05:00:00",
    "previous_planned_reading_id": "old-1",
}


def test_usage_applies_multiplier_scale_and_trailing_zeros(sheet_browser: SheetBrowser) -> None:
    target = usage_page(
        sheet_browser,
        definition_overrides={"multiplier": "2.50", "decimal_places": 3},
        slot_overrides=MEASURED_PREVIOUS,
        value="102.250",
    )

    expect(target).to_have_attribute("data-usage", "5.62500")
    expect(target.locator(".usage")).to_have_text("参考使用量: 5.62500 m3")


def test_usage_uses_exact_arithmetic_beyond_28_digits(sheet_browser: SheetBrowser) -> None:
    previous = "1234567890123456789012345678.9"
    raw = "1234567890123456789012345680.15"
    target = usage_page(
        sheet_browser,
        definition_overrides={"multiplier": "3.333333333333333333", "decimal_places": 2},
        slot_overrides={**MEASURED_PREVIOUS, "last_reading": previous},
        value=raw,
    )

    # 1.25 x 3.333333333333333333 を丸めずに計算した値。Python の共通計算と一致する。
    expected = exact_multiply(
        exact_subtract(Decimal(raw), Decimal(previous)), Decimal("3.333333333333333333")
    )
    assert format(expected, "f") == "4.16666666666666666625"
    expect(target).to_have_attribute("data-usage", format(expected, "f"))


def test_usage_corrects_rollover_and_reports_it(sheet_browser: SheetBrowser) -> None:
    target = usage_page(
        sheet_browser,
        definition_overrides={"rollover_value": "1000", "multiplier": "2"},
        slot_overrides={**MEASURED_PREVIOUS, "last_reading": "990.5"},
        value="4.50",
    )

    expect(target).to_have_attribute("data-usage", "28.00")
    expect(target.locator(".info")).to_contain_text("ロールオーバー値 1000 で補正")
    expect(target.locator(".warning")).to_have_count(0)


def test_negative_difference_without_rollover_warns_but_can_be_recorded(
    sheet_browser: SheetBrowser,
) -> None:
    target = usage_page(sheet_browser, slot_overrides=MEASURED_PREVIOUS, value="99.00")

    expect(target.locator(".warning")).to_contain_text("前回値 100.00 より小さく")
    expect(target.locator(".usage")).to_contain_text("計算できません")
    expect(target.locator(".confirm")).to_be_enabled()
    target.locator(".confirm").click()
    expect(target).to_have_attribute("data-confirmed", "true")


def test_usage_over_threshold_warns_without_blocking(sheet_browser: SheetBrowser) -> None:
    target = usage_page(
        sheet_browser,
        definition_overrides={"warning_threshold": "10", "multiplier": "2"},
        slot_overrides=MEASURED_PREVIOUS,
        value="105.01",
    )

    expect(target).to_have_attribute("data-usage", "10.02")
    expect(target.locator(".warning")).to_contain_text("警告閾値 10 を超えています")
    expect(target.locator(".confirm")).to_be_enabled()

    target.locator(".raw").fill("105.00")
    expect(target).to_have_attribute("data-usage", "10.00")
    expect(target.locator(".warning")).to_have_count(0)


@pytest.mark.parametrize(
    ("overrides", "kind", "expected_usage"),
    [
        ({"previous_source": "none"}, "none", "0"),
        ({"previous_source": "replacement_no_initial"}, "replacement_no_initial", "0"),
        (
            {"previous_source": "replacement_initial", "last_reading": "50"},
            "replacement_initial",
            "5.50",
        ),
    ],
)
def test_usage_for_first_reading_and_replacement_boundaries(
    sheet_browser: SheetBrowser,
    overrides: Mapping[str, Any],
    kind: str,
    expected_usage: str,
) -> None:
    target = usage_page(sheet_browser, slot_overrides=overrides, value="55.50")

    expect(target).to_have_attribute("data-previous-kind", kind)
    expect(target).to_have_attribute("data-usage", expected_usage)


def test_raw_only_definition_shows_no_usage(sheet_browser: SheetBrowser) -> None:
    target = usage_page(
        sheet_browser,
        definition_overrides={"calculation_method": "raw_only"},
        slot_overrides=MEASURED_PREVIOUS,
        value="150",
    )

    expect(target.locator(".usage")).to_be_hidden()
    expect(target).to_have_attribute("data-usage", "")
    expect(target.locator(".confirm")).to_be_enabled()


def carry_html() -> str:
    previous = {
        "previous_source": "measured",
        "last_reading": "200",
        "previous_scheduled_at": "2026-02-04 05:00:00",
        "previous_planned_reading_id": "done-1400",
    }
    return sheet_html(
        [
            slot("s-0800", "def-1", "meter-1", session="08:00", previous_source="none"),
            slot("s-2200", "def-1", "meter-1", session="22:00", **previous),
        ],
        [definition("def-1")],
        [meter("meter-1")],
    )


def test_completed_slot_between_sheet_slots_keeps_the_database_previous_reading(
    sheet_browser: SheetBrowser,
) -> None:
    page = open_all_tab(sheet_browser, carry_html())

    enter_and_confirm(page, "s-0800", "150")
    enter_and_confirm(page, "s-2200", "250")

    expect(card(page, "s-2200")).to_have_attribute("data-previous", "200")
    expect(card(page, "s-2200")).to_have_attribute("data-previous-kind", "measured")
    expect(card(page, "s-2200")).to_have_attribute("data-usage", "50")
    expect(card(page, "s-2200")).to_have_attribute("data-confirmed", "true")


def chain_html() -> str:
    return sheet_html(
        [
            slot("s-0800", "def-1", "meter-1", session="08:00"),
            slot("s-1400", "def-1", "meter-1", session="14:00", previous_source="none"),
            slot("s-2200", "def-1", "meter-1", session="22:00"),
        ],
        [definition("def-1", multiplier="1")],
        [meter("meter-1")],
    )


def test_previous_reading_is_carried_from_the_latest_confirmed_preceding_slot(
    sheet_browser: SheetBrowser,
) -> None:
    page = open_all_tab(sheet_browser, chain_html())
    morning, afternoon, night = (card(page, slot_id) for slot_id in ("s-0800", "s-1400", "s-2200"))

    expect(night).to_have_attribute("data-previous-kind", "none")
    expect(night).to_have_attribute("data-previous", "")

    enter_and_confirm(page, "s-0800", "100")
    expect(afternoon).to_have_attribute("data-previous", "100")
    expect(afternoon).to_have_attribute("data-previous-kind", "carried")
    expect(night.locator(".previous")).to_contain_text("同日 08:00 の入力値を引き継ぎ")

    afternoon.locator(".raw").fill("130")
    expect(afternoon).to_have_attribute("data-usage", "30")
    afternoon.locator(".confirm").click()
    expect(night).to_have_attribute("data-previous", "130")
    night.locator(".raw").fill("145")
    night.locator(".confirm").click()
    expect(night).to_have_attribute("data-usage", "15")

    morning.locator(".raw").fill("101")
    expect(morning).to_have_attribute("data-confirmed", "false")
    expect(afternoon).to_have_attribute("data-confirmed", "false")
    expect(night).to_have_attribute("data-confirmed", "false")
    expect(afternoon).to_have_attribute("data-previous-kind", "none")
    expect(night).to_have_attribute("data-previous-kind", "none")


def test_unconfirmed_or_non_measured_preceding_slots_are_not_carried(
    sheet_browser: SheetBrowser,
) -> None:
    page = open_all_tab(sheet_browser, chain_html())
    afternoon = card(page, "s-1400")

    card(page, "s-0800").locator(".raw").fill("100")
    expect(afternoon).to_have_attribute("data-previous-kind", "none")

    card(page, "s-0800").locator(".status").select_option("skipped")
    card(page, "s-0800").locator(".confirm").click()
    expect(afternoon).to_have_attribute("data-previous-kind", "none")


def test_confirming_an_earlier_slot_releases_a_later_confirmation_that_it_changes(
    sheet_browser: SheetBrowser,
) -> None:
    page = open_all_tab(sheet_browser, chain_html())

    enter_and_confirm(page, "s-1400", "130")
    expect(card(page, "s-1400")).to_have_attribute("data-confirmed", "true")
    expect(card(page, "s-1400")).to_have_attribute("data-usage", "0")

    enter_and_confirm(page, "s-0800", "100")

    expect(card(page, "s-1400")).to_have_attribute("data-confirmed", "false")
    expect(card(page, "s-1400")).to_have_attribute("data-previous", "100")


def test_same_scheduled_time_candidates_are_ordered_by_planned_reading_id(
    sheet_browser: SheetBrowser,
) -> None:
    same_time = "2026-02-04 05:00:00"
    html = sheet_html(
        [
            slot("plan-1", "def-1", "meter-1", session="14:00", scheduled_at=same_time),
            slot("plan-3", "def-1", "meter-1", session="14:00", scheduled_at=same_time),
            slot(
                "plan-z",
                "def-1",
                "meter-1",
                session="22:00",
                previous_source="measured",
                last_reading="300",
                previous_scheduled_at=same_time,
                previous_planned_reading_id="plan-2",
            ),
        ],
        [definition("def-1")],
        [meter("meter-1")],
    )
    page = open_all_tab(sheet_browser, html)
    target = card(page, "plan-z")

    enter_and_confirm(page, "plan-1", "150")
    expect(target).to_have_attribute("data-previous", "300")

    enter_and_confirm(page, "plan-3", "180")
    expect(target).to_have_attribute("data-previous", "180")
    expect(target).to_have_attribute("data-previous-kind", "carried")

    target.locator(".raw").fill("200")
    target.locator(".confirm").click()
    expect(target).to_have_attribute("data-confirmed", "true")

    card(page, "plan-3").locator(".confirm").click()
    expect(card(page, "plan-3")).to_have_attribute("data-confirmed", "false")
    expect(target).to_have_attribute("data-confirmed", "false")
    expect(target).to_have_attribute("data-previous", "300")
    expect(target).to_have_attribute("data-previous-kind", "measured")


def test_replacement_meters_do_not_inherit_readings_across_meter_ids(
    sheet_browser: SheetBrowser,
) -> None:
    html = sheet_html(
        [
            slot("old-slot", "def-1", "meter-old", session="08:00"),
            slot(
                "new-slot",
                "def-1",
                "meter-new",
                session="14:00",
                previous_source="replacement_initial",
                last_reading="5",
            ),
        ],
        [definition("def-1")],
        [meter("meter-old"), meter("meter-new")],
    )
    page = open_all_tab(sheet_browser, html)

    enter_and_confirm(page, "old-slot", "900")

    expect(card(page, "new-slot")).to_have_attribute("data-previous-kind", "replacement_initial")
    expect(card(page, "new-slot")).to_have_attribute("data-previous", "5")


def test_replacement_without_initial_value_switches_to_difference_after_carry(
    sheet_browser: SheetBrowser,
) -> None:
    html = sheet_html(
        [
            slot(
                "first",
                "def-1",
                "meter-new",
                session="08:00",
                previous_source="replacement_no_initial",
            ),
            slot(
                "second",
                "def-1",
                "meter-new",
                session="14:00",
                previous_source="replacement_no_initial",
            ),
        ],
        [definition("def-1")],
        [meter("meter-new")],
    )
    page = open_all_tab(sheet_browser, html)

    enter_and_confirm(page, "first", "40")
    card(page, "second").locator(".raw").fill("55")

    expect(card(page, "first")).to_have_attribute("data-usage", "0")
    expect(card(page, "second")).to_have_attribute("data-previous-kind", "carried")
    expect(card(page, "second")).to_have_attribute("data-usage", "15")


def test_next_button_moves_to_the_next_unentered_card_in_the_same_location(
    sheet_browser: SheetBrowser,
) -> None:
    page = sheet_browser.open_html(location_sheet_html())

    page.locator('.tab[data-tab="all"]').click()
    enter_and_confirm(page, "s-elec", "1")
    card(page, "s-elec").locator(".next").click()
    expect(card(page, "s-gas").locator(".raw")).to_be_focused()

    enter_and_confirm(page, "s-gas", "1")
    enter_and_confirm(page, "s-water-a", "1")
    enter_and_confirm(page, "s-water-b", "1")
    card(page, "s-water-b").locator(".next").click()
    expect(card(page, "s-misc").locator(".raw")).to_be_focused()

    enter_and_confirm(page, "s-misc", "1")
    card(page, "s-misc").locator(".next").click()
    expect(card(page, "s-misc").locator(".next-message")).to_have_text(
        "この場所に未入力の枠はありません。"
    )
    expect(card(page, "s-a").locator(".raw")).not_to_be_focused()


@pytest.mark.parametrize("viewport", VIEWPORTS.values(), ids=VIEWPORTS.keys())
def test_all_operations_are_reachable_at_each_viewport(
    sheet_browser: SheetBrowser, viewport: tuple[int, int]
) -> None:
    page = sheet_browser.open_html(
        sheet_html(
            [
                slot("a-1", "def-a", "meter-a", session="08:00"),
                slot("a-2", "def-b", "meter-a", session="08:00"),
                slot("b-1", "def-c", "meter-b", session="14:00"),
                slot("pf-1", "def-pf", "meter-a", session="08:00"),
                slot("w-1", "def-a", "meter-b", session="14:00", cycle="weekly"),
            ],
            [
                definition("def-a", location_id="loc-a", display_name="A"),
                definition("def-b", location_id="loc-a", display_name="B"),
                definition("def-c", location_id="loc-c", display_name="C"),
                definition(
                    "def-pf",
                    location_id="loc-a",
                    display_name="力率",
                    value_schema="power_factor_percent_phase",
                    unit="%",
                ),
            ],
            [
                meter("meter-a", location_id="loc-a", location_name="場所 A"),
                meter("meter-b", location_id="loc-c", location_name="場所 C"),
            ],
        ),
        viewport=viewport,
    )

    def assert_fits() -> None:
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0

    assert_fits()
    for tab in page.locator(".tab").all():
        tab.click()
    page.locator('.tab[data-tab="s:08:00"]').click()
    page.locator(".location-select").select_option("loc-a")
    page.locator(".unentered-toggle").check()
    page.locator(".unentered-toggle").uncheck()

    enter_and_confirm(page, "a-1", "10")
    card(page, "a-1").locator(".note").fill("メモ")
    card(page, "a-1").locator(".confirm").click()
    card(page, "a-1").locator(".confirm").click()
    card(page, "a-1").locator(".next").click()
    expect(card(page, "a-2").locator(".raw")).to_be_focused()
    card(page, "a-2").locator(".status").select_option("skipped")
    card(page, "a-2").locator(".confirm").click()

    pf = card(page, "pf-1")
    pf.locator(".pf-percent").fill("90")
    pf.locator(".pf-phase").select_option("lag")
    pf.locator(".confirm").click()
    expect(pf).to_have_attribute("data-confirmed", "true")

    page.locator(".location-toggle").first.click()
    page.locator(".location-toggle").first.click()
    page.locator('.tab[data-tab="all"]').click()
    page.locator(".location-select").select_option("")
    for slot_id in ("a-1", "b-1", "w-1"):
        card(page, slot_id).locator(".confirm").scroll_into_view_if_needed()
        box = card(page, slot_id).bounding_box()
        assert box is not None
        assert box["x"] >= 0
        assert box["x"] + box["width"] <= viewport[0]
    page.locator("#result-copy").scroll_into_view_if_needed()
    expect(page.locator("#result-copy")).to_be_enabled()
    expect(page.locator("#result-download")).to_be_enabled()
    output_box = page.locator("#result-copy").bounding_box()
    assert output_box is not None
    assert output_box["x"] >= 0
    assert output_box["x"] + output_box["width"] <= viewport[0]
    assert_fits()
