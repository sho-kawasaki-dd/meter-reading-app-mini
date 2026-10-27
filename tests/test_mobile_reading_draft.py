from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from conftest import SheetBrowser
from mobile_draft_helpers import (
    SLOT_1_RAW,
    export_draft,
    get_record,
    import_text,
    open_sheet,
    put_record,
    reload_and_wait,
    wait_saved,
)
from mobile_sheet_helpers import SHEET_ID, definition, meter, sheet_html, slot
from playwright.sync_api import FilePayload, Locator, Page, expect

VIEWPORTS = {
    "phone-portrait": (360, 640),
    "phone-landscape": (640, 360),
    "pc": (1366, 768),
}


def card(page: Page, slot_id: str) -> Locator:
    return page.locator(f'.card[data-slot-id="{slot_id}"]')


def enter_and_confirm(page: Page, slot_id: str, value: str) -> None:
    card(page, slot_id).locator(".raw").fill(value)
    card(page, slot_id).locator(".confirm").click()


def chain_html() -> str:
    return sheet_html(
        [
            slot("s-0800", "def-1", "meter-1", session="08:00"),
            slot("s-1400", "def-1", "meter-1", session="14:00"),
            slot("s-2200", "def-1", "meter-1", session="22:00"),
        ],
        [definition("def-1", warning_threshold="10")],
        [meter("meter-1")],
    )


def carry_html() -> str:
    return sheet_html(
        [
            slot("s-0800", "def-1", "meter-1", session="08:00"),
            slot(
                "s-2200",
                "def-1",
                "meter-1",
                session="22:00",
                previous_source="measured",
                last_reading="200",
                previous_scheduled_at="2026-02-04 05:00:00",
                previous_planned_reading_id="done-1400",
            ),
        ],
        [definition("def-1")],
        [meter("meter-1")],
    )


def rich_html() -> str:
    return sheet_html(
        [
            slot("a-1", "def-1", "meter-1"),
            slot("a-2", "def-2", "meter-2"),
            slot("a-3", "def-1", "meter-3"),
            slot("pf-1", "def-pf", "meter-4"),
        ],
        [
            definition("def-1"),
            definition("def-2"),
            definition(
                "def-pf",
                value_schema="power_factor_percent_phase",
                unit="%",
            ),
        ],
        [meter("meter-1"), meter("meter-2"), meter("meter-3"), meter("meter-4")],
    )


def assert_chain_recomputed(page: Page) -> None:
    page.locator('.tab[data-tab="all"]').click()
    afternoon, night = card(page, "s-1400"), card(page, "s-2200")
    for slot_id in ("s-0800", "s-1400", "s-2200"):
        expect(card(page, slot_id)).to_have_attribute("data-confirmed", "true")
    expect(afternoon).to_have_attribute("data-previous", "100")
    expect(afternoon).to_have_attribute("data-previous-kind", "carried")
    expect(afternoon).to_have_attribute("data-usage", "30")
    expect(afternoon.locator(".warning")).to_contain_text("警告閾値 10 を超えています")
    expect(night).to_have_attribute("data-previous", "130")
    expect(night).to_have_attribute("data-usage", "15")
    expect(night.locator(".warning")).to_contain_text("警告閾値 10 を超えています")

    card(page, "s-0800").locator(".raw").fill("101")
    expect(afternoon).to_have_attribute("data-confirmed", "false")
    expect(night).to_have_attribute("data-confirmed", "false")
    expect(afternoon).to_have_attribute("data-previous-kind", "none")
    expect(afternoon.locator(".warning")).to_have_count(0)


def build_confirmed_chain(sheet_browser: SheetBrowser) -> tuple[Page, str]:
    source = open_sheet(sheet_browser, html=chain_html(), name="source.html")
    source.locator('.tab[data-tab="all"]').click()
    for slot_id, value in (("s-0800", "100"), ("s-1400", "130"), ("s-2200", "145")):
        enter_and_confirm(source, slot_id, value)
    wait_saved(source)
    return source, json.dumps(export_draft(source))


def test_import_recomputes_carry_usage_warnings_and_dependent_confirmations(
    sheet_browser: SheetBrowser,
) -> None:
    _, backup = build_confirmed_chain(sheet_browser)
    target = open_sheet(sheet_browser, html=chain_html(), name="target.html")
    target.locator('.tab[data-tab="all"]').click()
    card(target, "s-0800").locator(".raw").fill("500")

    import_text(target, backup)
    target.locator("#draft-confirm-import").click()

    expect(card(target, "s-0800").locator(".raw")).to_have_value("100")
    assert_chain_recomputed(target)


def test_reload_restores_carry_usage_warnings_and_dependent_confirmations(
    sheet_browser: SheetBrowser,
) -> None:
    source, _ = build_confirmed_chain(sheet_browser)

    reload_and_wait(source)

    assert_chain_recomputed(source)


def test_all_input_states_survive_reload_and_json_round_trip_with_mr1_note(
    sheet_browser: SheetBrowser,
) -> None:
    page = open_sheet(sheet_browser, html=rich_html())
    tricky_note = 'MR1:YWJj.00000000:END 😀\n二行目 "引用" </script>'
    card(page, "a-1").locator(".raw").fill("12.50")
    card(page, "a-1").locator(".note").fill(tricky_note)
    card(page, "a-1").locator(".confirm").click()
    card(page, "a-2").locator(".status").select_option("skipped")
    card(page, "a-2").locator(".note").fill("メモ")
    card(page, "a-2").locator(".confirm").click()
    card(page, "a-3").locator(".raw").fill("12..3")
    card(page, "a-3").locator(".note").fill("あ" * 500)
    card(page, "pf-1").locator(".pf-percent").fill("95.5")
    card(page, "pf-1").locator(".pf-phase").select_option("lag")
    card(page, "pf-1").locator(".confirm").click()
    wait_saved(page)
    before = export_draft(page)
    by_id = {entry["planned_reading_id"]: entry for entry in before["slots"]}
    assert by_id["a-1"]["note"] == tricky_note
    assert by_id["a-1"]["confirmed"] is True
    assert by_id["a-2"]["result_status"] == "skipped"
    assert by_id["a-3"]["bad_input"] is True
    assert by_id["pf-1"]["pf_percent"] == "95.5"
    assert by_id["pf-1"]["pf_phase"] == "lag"

    reload_and_wait(page)
    assert export_draft(page) == before
    for slot_id, confirmed in (("a-1", "true"), ("a-2", "true"), ("a-3", "false")):
        expect(card(page, slot_id)).to_have_attribute("data-confirmed", confirmed)
    expect(card(page, "pf-1")).to_have_attribute("data-confirmed", "true")

    restored = open_sheet(sheet_browser, html=rich_html(), name="restored.html")
    import_text(restored, json.dumps(before, ensure_ascii=False))
    expect(restored.locator("#draft-preview")).to_be_visible()
    restored.locator("#draft-confirm-import").click()
    assert export_draft(restored) == before


def two_slot_draft(page: Page) -> dict[str, Any]:
    page.locator(".card[data-slot-id='s-0800'] .raw").fill("7")
    wait_saved(page)
    return export_draft(page)


def _slot(draft: dict[str, Any], index: int = 0) -> dict[str, Any]:
    return draft["slots"][index]


def _mutate_slot(**changes: Any) -> Callable[[dict[str, Any]], None]:
    return lambda draft: _slot(draft, 1).update(changes)


def _mutate_top(**changes: Any) -> Callable[[dict[str, Any]], None]:
    return lambda draft: draft.update(changes)


def _drop_top(key: str) -> Callable[[dict[str, Any]], None]:
    return lambda draft: draft.pop(key)


def _drop_slot_key(key: str) -> Callable[[dict[str, Any]], None]:
    return lambda draft: _slot(draft, 1).pop(key)


MUTATIONS: dict[str, Callable[[dict[str, Any]], None]] = {
    "kind": _mutate_top(kind="mrm-meter-reading-sheet"),
    "version": _mutate_top(v=2),
    "sheet_id": _mutate_top(sheet_id="c" * 32),
    "business_date": _mutate_top(business_date="2026-02-05"),
    "generated_at": _mutate_top(generated_at="2026-02-04 04:00:00"),
    "filter_sessions": _mutate_top(filter={"reading_sessions": ["08:00"], "cycle_groups": None}),
    "filter_extra_key": _mutate_top(
        filter={"reading_sessions": None, "cycle_groups": None, "extra": 1}
    ),
    "unknown_top_key": _mutate_top(extra=1),
    "missing_top_key": _drop_top("filter"),
    "slot_count": lambda draft: draft["slots"].pop(),
    "slot_order": lambda draft: draft["slots"].reverse(),
    "slot_id": _mutate_slot(planned_reading_id="other"),
    "definition_id": _mutate_slot(measurement_definition_id="other"),
    "meter_id": _mutate_slot(meter_id="other"),
    "initial_last_reading": _mutate_slot(initial_last_reading="201"),
    "previous_source": _mutate_slot(previous_source="none"),
    "previous_scheduled_at": _mutate_slot(initial_previous_scheduled_at="2026-02-04 04:00:00"),
    "previous_planned_id": _mutate_slot(initial_previous_planned_reading_id="other"),
    "unknown_slot_key": _mutate_slot(extra=1),
    "missing_slot_key": _drop_slot_key("note"),
    "result_status": _mutate_slot(result_status="replaced"),
    "raw_type": _mutate_slot(raw=5),
    "note_type": _mutate_slot(note=None),
    "confirmed_type": _mutate_slot(confirmed="yes"),
    "bad_input_type": _mutate_slot(bad_input=1),
    "recorded_at_format": _mutate_slot(recorded_at="2026/02/04 12:00"),
    "recorded_at_date": _mutate_slot(recorded_at="2026-13-45T00:00:00Z"),
    "client_updated_at": _mutate_slot(client_updated_at="x"),
    "confirmed_incomplete": _mutate_slot(confirmed=True, raw=""),
    "confirmed_without_recorded_at": _mutate_slot(confirmed=True, raw="201", recorded_at=None),
}
RESTORE_MUTATIONS = (
    "kind",
    "sheet_id",
    "business_date",
    "generated_at",
    "filter_sessions",
    "slot_count",
    "initial_last_reading",
    "previous_planned_id",
    "result_status",
    "confirmed_incomplete",
)


@pytest.mark.parametrize("name", MUTATIONS)
def test_import_rejects_every_mismatch_and_keeps_the_screen(
    sheet_browser: SheetBrowser, name: str
) -> None:
    page = open_sheet(sheet_browser, html=carry_html())
    draft = two_slot_draft(page)
    MUTATIONS[name](draft)

    import_text(page, json.dumps(draft))

    expect(page.locator("#draft-feedback")).to_contain_text("途中経過 JSON を読み込めません")
    expect(page.locator("#draft-preview")).to_be_hidden()
    page.evaluate("document.querySelector('#draft-confirm-import').click()")
    expect(page.locator(".card[data-slot-id='s-0800'] .raw")).to_have_value("7")


@pytest.mark.parametrize("name", RESTORE_MUTATIONS)
def test_restore_applies_the_same_checks_and_never_overwrites(
    sheet_browser: SheetBrowser, name: str
) -> None:
    context = sheet_browser.new_context()
    page = open_sheet(sheet_browser, html=carry_html(), context=context)
    draft = two_slot_draft(page)
    MUTATIONS[name](draft)
    record = {"sheet_id": SHEET_ID, "storage_revision": 3, "draft": draft}
    put_record(page, record)

    reload_and_wait(page)

    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-retry")).to_be_visible()
    expect(page.locator("#draft-replace")).to_be_visible()
    assert get_record(page, SHEET_ID) == record
    expect(page.locator(".card[data-slot-id='s-0800'] .raw")).to_have_value("")


def test_old_preview_is_discarded_when_a_new_read_fails(sheet_browser: SheetBrowser) -> None:
    page = open_sheet(sheet_browser)
    raw = page.locator(SLOT_1_RAW)
    raw.fill("1")
    wait_saved(page)
    good = json.dumps(export_draft(page))
    raw.fill("2")

    import_text(page, good)
    expect(page.locator("#draft-preview")).to_be_visible()
    import_text(page, "{broken")
    expect(page.locator("#draft-preview")).to_be_hidden()
    page.evaluate("document.querySelector('#draft-confirm-import').click()")
    expect(raw).to_have_value("2")

    import_text(page, good)
    expect(page.locator("#draft-preview")).to_be_visible()
    page.locator("#draft-cancel-import").click()
    expect(page.locator("#draft-preview")).to_be_hidden()
    page.evaluate("document.querySelector('#draft-confirm-import').click()")
    expect(raw).to_have_value("2")
    wait_saved(page)
    reload_and_wait(page)
    expect(raw).to_have_value("2")


def _file_payload(name: str, content: bytes) -> FilePayload:
    return {"name": name, "mimeType": "application/json", "buffer": content}


@pytest.mark.parametrize(
    ("label", "content", "message"),
    [
        ("broken", b"{broken", "途中経過 JSON を読み込めません"),
        ("empty", b"", "JSON が空です"),
        ("result", b'{"kind":"mrm-meter-reading-result","v":1}', "結果 JSON"),
        ("mr1", b"MR1:YWJj.00000000:END", "MR1 は結果コード"),
        ("mail", "> お疲れさまです\n> MR1:YWJj.00000000:END".encode(), "MR1 は結果コード"),
        ("array", b"[]", "項目構成が不正"),
    ],
)
def test_file_import_rejects_unusable_files_and_discards_the_old_preview(
    sheet_browser: SheetBrowser, label: str, content: bytes, message: str
) -> None:
    page = open_sheet(sheet_browser)
    raw = page.locator(SLOT_1_RAW)
    raw.fill("1")
    wait_saved(page)
    good = export_draft(page)
    raw.fill("2")
    page.locator("#draft-file").set_input_files(
        _file_payload("good.json", json.dumps(good).encode())
    )
    expect(page.locator("#draft-preview")).to_be_visible()

    page.locator("#draft-file").set_input_files(_file_payload(f"{label}.json", content))

    expect(page.locator("#draft-feedback")).to_contain_text(message)
    expect(page.locator("#draft-preview")).to_be_hidden()
    expect(raw).to_have_value("2")


def test_file_import_of_another_sheet_is_rejected(sheet_browser: SheetBrowser) -> None:
    page = open_sheet(sheet_browser)
    other = export_draft(page)
    other["sheet_id"] = "c" * 32

    page.locator("#draft-file").set_input_files(
        _file_payload("other.json", json.dumps(other).encode())
    )

    expect(page.locator("#draft-feedback")).to_contain_text("別の検針シート")
    expect(page.locator("#draft-preview")).to_be_hidden()


def test_copy_succeeds_with_the_exported_json(sheet_browser: SheetBrowser) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(
        """
        Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: { writeText: async text => { window.__copied = text; } },
        });
        """
    )
    page = open_sheet(sheet_browser, context=context)
    page.locator(SLOT_1_RAW).fill("5.5")
    wait_saved(page)

    page.locator("#draft-copy").click()

    expect(page.locator("#draft-feedback")).to_contain_text("コピーしました")
    copied = json.loads(page.evaluate("window.__copied"))
    assert copied["slots"][0]["raw"] == "5.5"


@pytest.mark.parametrize(
    "script",
    [
        "Object.defineProperty(navigator, 'clipboard', {configurable: true, value: undefined});",
        "Object.defineProperty(navigator, 'clipboard', {configurable: true, value: "
        "{ writeText: async () => { throw new DOMException('拒否', 'NotAllowedError'); } }});",
    ],
    ids=["unavailable", "rejected"],
)
def test_copy_failure_selects_the_json_for_manual_copy(
    sheet_browser: SheetBrowser, script: str
) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(script)
    page = open_sheet(sheet_browser, context=context)

    page.locator("#draft-copy").click()

    expect(page.locator("#draft-feedback")).to_contain_text("手動でコピー")
    export = page.locator("#draft-export-text")
    expect(export).to_be_visible()
    selected = export.evaluate(
        "node => node.selectionEnd - node.selectionStart === node.value.length "
        "&& node.value.length > 0"
    )
    assert selected is True


def test_download_uses_the_business_date_name_and_does_not_claim_success(
    sheet_browser: SheetBrowser,
) -> None:
    page = open_sheet(sheet_browser)

    with page.expect_download() as download_info:
        page.locator("#draft-download").click()

    assert download_info.value.suggested_filename == "meter_reading_draft_20260204.json"
    feedback = page.locator("#draft-feedback")
    expect(feedback).to_contain_text("要求")
    expect(feedback).not_to_contain_text("保存しました")
    expect(feedback).not_to_contain_text("保存成功")


def test_download_failure_points_to_manual_copy(sheet_browser: SheetBrowser) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(
        """
        URL.createObjectURL = function () {
          throw new DOMException("テスト用のダウンロード失敗", "NotSupportedError");
        };
        """
    )
    page = open_sheet(sheet_browser, context=context)

    page.locator("#draft-download").click()

    feedback = page.locator("#draft-feedback")
    expect(feedback).to_contain_text("ダウンロードできません")
    expect(feedback).to_contain_text("手動でコピー")
    assert export_draft(page)["sheet_id"] == SHEET_ID


def assert_inside_viewport(page: Page, locator: Locator, width: int) -> None:
    locator.scroll_into_view_if_needed()
    expect(locator).to_be_visible()
    box = locator.bounding_box()
    assert box is not None
    assert box["x"] >= 0
    assert box["x"] + box["width"] <= width + 0.5


def assert_no_horizontal_overflow(page: Page) -> None:
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0


@pytest.mark.parametrize("viewport", VIEWPORTS.values(), ids=VIEWPORTS.keys())
def test_draft_tools_are_reachable_at_each_viewport(
    sheet_browser: SheetBrowser, viewport: tuple[int, int]
) -> None:
    width = viewport[0]
    context = sheet_browser.new_context(viewport=viewport)
    context.add_init_script(
        "Object.defineProperty(navigator, 'clipboard', "
        "{configurable: true, value: {writeText: async () => {}}});"
    )
    page = open_sheet(sheet_browser, context=context, name="viewport-1.html", viewport=viewport)
    second = open_sheet(sheet_browser, context=context, name="viewport-2.html", viewport=viewport)

    def check(*selectors: str) -> None:
        for selector in selectors:
            assert_inside_viewport(page, page.locator(selector), width)
        assert_no_horizontal_overflow(page)

    check(
        "#draft-storage-status",
        "#draft-show",
        "#draft-copy",
        "#draft-download",
        "#draft-file",
        "#draft-import-text",
        "#draft-import",
    )
    page.locator(SLOT_1_RAW).fill("10")
    wait_saved(page)
    page.locator("#draft-show").click()
    check("#draft-export-text")
    page.locator("#draft-copy").click()
    check("#draft-feedback")
    backup = page.locator("#draft-export-text").input_value()

    import_text(page, "{broken")
    check("#draft-feedback")
    import_text(page, backup)
    check("#draft-preview", "#draft-confirm-import", "#draft-cancel-import")
    page.locator("#draft-cancel-import").click()
    import_text(page, backup)
    page.locator("#draft-confirm-import").click()
    wait_saved(page)

    second.locator(SLOT_1_RAW).fill("20")
    expect(second.locator("#draft-conflict")).to_be_visible()
    for selector in ("#draft-conflict", "#draft-use-stored", "#draft-use-current"):
        assert_inside_viewport(second, second.locator(selector), width)
    second.locator("#draft-use-current").click()
    for selector in (
        "#draft-replace-confirmation",
        "#draft-confirm-replace",
        "#draft-cancel-replace",
    ):
        assert_inside_viewport(second, second.locator(selector), width)
    second.locator("#draft-cancel-replace").click()
    second.locator("#draft-use-stored").click()
    assert_no_horizontal_overflow(second)

    put_record(page, {"sheet_id": SHEET_ID, "storage_revision": 9, "draft": {"kind": "corrupt"}})
    third = open_sheet(sheet_browser, context=context, name="viewport-3.html", viewport=viewport)
    expect(third.locator("#draft-storage-status")).to_have_attribute("data-status", "error")
    for selector in ("#draft-retry", "#draft-manual-continue", "#draft-replace"):
        assert_inside_viewport(third, third.locator(selector), width)
    third.locator("#draft-replace").click()
    for selector in (
        "#draft-replace-confirmation",
        "#draft-confirm-replace",
        "#draft-cancel-replace",
    ):
        assert_inside_viewport(third, third.locator(selector), width)
    third.locator("#draft-manual-continue").click()
    assert_no_horizontal_overflow(third)
