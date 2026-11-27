from __future__ import annotations

import json

from conftest import SheetBrowser
from mobile_draft_helpers import (
    SHORT_TIMEOUT_SCRIPT,
    SLOT_1_RAW,
    STATUS_RECORDER_SCRIPT,
    export_draft,
    get_record,
    import_text,
    oncomplete_patch,
    open_sheet,
    put_record,
    reload_and_wait,
    wait_saved,
)
from mobile_sheet_helpers import SHEET_ID, definition, meter, sheet_data, slot
from playwright.sync_api import Locator, Page, expect

from meter_reading_mini.core.mobile_reading import render_sheet_html


def status(page: Page) -> Locator:
    return page.locator("#draft-storage-status")


def assert_input_and_json_survive(page: Page, value: str) -> None:
    expect(page.locator(SLOT_1_RAW)).to_have_value(value)
    assert export_draft(page)["slots"][0]["raw"] == value


def test_blocked_open_reports_failure_and_offers_manual_backup(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(
        """
        IDBFactory.prototype.open = function () {
          const request = {};
          setTimeout(() => request.onblocked && request.onblocked(new Event("blocked")), 0);
          return request;
        };
        """
    )
    page = open_sheet(sheet_browser, context=context)

    expect(status(page)).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-storage-message")).to_contain_text("別のページが保存先")
    expect(page.locator("#draft-retry")).to_be_visible()
    page.locator("#draft-manual-continue").click()
    page.locator(SLOT_1_RAW).fill("3")
    assert_input_and_json_survive(page, "3")


def test_connection_timeout_reports_failure(sheet_browser: SheetBrowser) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(SHORT_TIMEOUT_SCRIPT)
    context.add_init_script("IDBFactory.prototype.open = function () { return {}; };")
    page = open_sheet(sheet_browser, context=context)

    expect(status(page)).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-storage-message")).to_contain_text("タイムアウト")
    expect(page.locator("#draft-manual-continue")).to_be_visible()


def test_read_timeout_does_not_replace_unreadable_storage(sheet_browser: SheetBrowser) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(SHORT_TIMEOUT_SCRIPT)
    context.add_init_script(oncomplete_patch("readonly", "null"))
    page = open_sheet(sheet_browser, context=context)

    expect(status(page)).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-storage-message")).to_contain_text("タイムアウト")
    expect(page.locator("#draft-retry")).to_be_visible()
    assert get_record(page, SHEET_ID) is None


def test_write_timeout_keeps_input_and_json_backup(sheet_browser: SheetBrowser) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(SHORT_TIMEOUT_SCRIPT)
    context.add_init_script(oncomplete_patch("readwrite", "null"))
    page = open_sheet(sheet_browser, context=context)

    page.locator(SLOT_1_RAW).fill("8")

    expect(status(page)).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-storage-message")).to_contain_text("タイムアウト")
    assert_input_and_json_survive(page, "8")


def test_version_change_closes_the_connection_and_keeps_input(
    sheet_browser: SheetBrowser,
) -> None:
    page = open_sheet(sheet_browser)
    page.evaluate(
        """
        () => new Promise((resolve, reject) => {
          const request = indexedDB.open("mrm_meter_reading", 2);
          request.onerror = () => reject(request.error);
          request.onsuccess = () => { request.result.close(); resolve(); };
        })
        """
    )

    expect(status(page)).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-storage-message")).to_contain_text("版数が変更")
    page.locator(SLOT_1_RAW).fill("5")
    assert_input_and_json_survive(page, "5")

    page.locator("#draft-retry").click()
    expect(status(page)).to_have_attribute("data-status", "error")
    expect(page.locator(SLOT_1_RAW)).to_have_value("5")


def test_connection_close_event_stops_autosave_and_keeps_input(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(
        """
        (() => {
          const open = IDBFactory.prototype.open;
          IDBFactory.prototype.open = function (...args) {
            const request = open.apply(this, args);
            request.addEventListener("success", () => { window.__mrmDatabase = request.result; });
            return request;
          };
        })();
        """
    )
    page = open_sheet(sheet_browser, context=context)

    page.evaluate("window.__mrmDatabase.dispatchEvent(new Event('close'))")

    expect(status(page)).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-storage-message")).to_contain_text("接続が終了")
    page.locator(SLOT_1_RAW).fill("6")
    assert_input_and_json_survive(page, "6")


def test_aborted_write_transaction_reports_failure(sheet_browser: SheetBrowser) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(
        """
        (() => {
          const put = IDBObjectStore.prototype.put;
          IDBObjectStore.prototype.put = function (...args) {
            const result = put.apply(this, args);
            this.transaction.abort();
            return result;
          };
        })();
        """
    )
    page = open_sheet(sheet_browser, context=context)

    page.locator(SLOT_1_RAW).fill("9")

    expect(status(page)).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-storage-message")).to_contain_text("保存トランザクションに失敗")
    assert_input_and_json_survive(page, "9")
    assert get_record(page, SHEET_ID) is None


_FAIL_SWITCH_SCRIPT = """
(() => {
  const put = IDBObjectStore.prototype.put;
  window.__failPut = false;
  window.__failOpen = false;
  IDBObjectStore.prototype.put = function (...args) {
    if (window.__failPut) {
      throw new DOMException("テスト用の書込失敗", "QuotaExceededError");
    }
    return put.apply(this, args);
  };
  const open = IDBFactory.prototype.open;
  IDBFactory.prototype.open = function (...args) {
    if (window.__failOpen) {
      throw new DOMException("テスト用の接続失敗", "SecurityError");
    }
    return open.apply(this, args);
  };
})();
"""


def test_retry_after_open_failure_enables_input_and_saves(sheet_browser: SheetBrowser) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(_FAIL_SWITCH_SCRIPT)
    context.add_init_script("window.__failOpen = true;")
    page = open_sheet(sheet_browser, context=context)
    expect(status(page)).to_have_attribute("data-status", "error")
    assert page.locator(SLOT_1_RAW).evaluate("node => node.closest('[inert]') !== null")

    page.evaluate("window.__failOpen = false")
    page.locator("#draft-retry").click()
    expect(status(page)).to_have_attribute("data-status", "ready")
    expect(status(page)).to_contain_text("入力待ち")
    page.locator(SLOT_1_RAW).fill("4")
    wait_saved(page)

    stored = get_record(page, SHEET_ID)
    assert stored is not None
    assert stored["draft"]["slots"][0]["raw"] == "4"


def test_retry_after_write_failure_saves_the_unsaved_input(sheet_browser: SheetBrowser) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(_FAIL_SWITCH_SCRIPT)
    page = open_sheet(sheet_browser, context=context)
    page.evaluate("window.__failPut = true")
    page.locator(SLOT_1_RAW).fill("10")
    expect(status(page)).to_have_attribute("data-status", "error")

    page.evaluate("window.__failPut = false")
    page.locator("#draft-retry").click()
    wait_saved(page)

    reload_and_wait(page)
    expect(page.locator(SLOT_1_RAW)).to_have_value("10")


def test_retry_after_write_failure_detects_a_conflicting_save(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(_FAIL_SWITCH_SCRIPT)
    first = open_sheet(sheet_browser, context=context, name="first.html")
    second = open_sheet(sheet_browser, context=context, name="second.html")
    first.evaluate("window.__failPut = true")
    first.locator(SLOT_1_RAW).fill("10")
    expect(status(first)).to_have_attribute("data-status", "error")
    second.locator(SLOT_1_RAW).fill("20")
    wait_saved(second)

    first.evaluate("window.__failPut = false")
    first.locator("#draft-retry").click()

    expect(first.locator("#draft-conflict")).to_be_visible()
    expect(status(first)).to_have_attribute("data-status", "error")
    expect(first.locator(SLOT_1_RAW)).to_have_value("10")
    first.locator("#draft-use-stored").click()
    expect(first.locator(SLOT_1_RAW)).to_have_value("20")


def test_loading_blocks_input_until_storage_is_confirmed(sheet_browser: SheetBrowser) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(oncomplete_patch("readonly", "800"))
    page = sheet_browser.open_html(sheet_html_for(SHEET_ID), name="loading.html", context=context)

    expect(status(page)).to_have_attribute("data-status", "loading")
    assert page.locator(SLOT_1_RAW).evaluate("node => node.closest('[inert]') !== null")
    expect(status(page)).to_have_attribute("data-status", "ready")
    expect(status(page)).to_contain_text("入力待ち")
    assert page.locator(SLOT_1_RAW).evaluate("node => node.closest('[inert]') === null")


def sheet_html_for(sheet_id: str, *, generated_at: str = "2026-02-04 03:00:00") -> str:
    data = sheet_data(
        [slot("slot-1", "def-1", "meter-1")], [definition("def-1")], [meter("meter-1")]
    )
    data["sheet_id"] = sheet_id
    data["generated_at"] = generated_at
    return render_sheet_html(data)


def test_newer_conflict_survives_the_completion_of_an_older_write(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(STATUS_RECORDER_SCRIPT)
    context.add_init_script(oncomplete_patch("readwrite", "number === 1 ? 1500 : 0"))
    page = open_sheet(sheet_browser, context=context)
    raw = page.locator(SLOT_1_RAW)
    page.evaluate("window.__mrmStatuses.length = 0")

    raw.fill("10")
    raw.fill("20")
    other = export_draft(page)
    other["slots"][0]["raw"] = "99"
    put_record(page, {"sheet_id": SHEET_ID, "storage_revision": 2, "draft": other})

    expect(page.locator("#draft-conflict")).to_be_visible()
    expect(status(page)).to_have_attribute("data-status", "error")
    statuses = page.evaluate("window.__mrmStatuses")
    assert "saved" not in statuses
    assert statuses[-1] == "error"
    assert_input_and_json_survive(page, "20")

    page.locator("#draft-use-stored").click()
    expect(raw).to_have_value("99")


def test_conflict_stops_autosave_until_the_user_chooses(sheet_browser: SheetBrowser) -> None:
    context = sheet_browser.new_context()
    first = open_sheet(sheet_browser, context=context, name="first.html")
    second = open_sheet(sheet_browser, context=context, name="second.html")
    first.locator(SLOT_1_RAW).fill("10")
    wait_saved(first)

    second.locator(SLOT_1_RAW).fill("20")
    expect(second.locator("#draft-conflict")).to_be_visible()
    second.locator(SLOT_1_RAW).fill("30")
    expect(status(second)).to_have_attribute("data-status", "error")
    expect(second.locator("#draft-conflict")).to_be_visible()
    assert_input_and_json_survive(second, "30")
    stored = get_record(second, SHEET_ID)
    assert stored is not None
    assert stored["draft"]["slots"][0]["raw"] == "10"

    second.locator("#draft-use-stored").click()
    expect(second.locator(SLOT_1_RAW)).to_have_value("10")
    second.locator(SLOT_1_RAW).fill("40")
    wait_saved(second)

    first.locator(SLOT_1_RAW).fill("50")
    expect(first.locator("#draft-conflict")).to_be_visible()
    expect(first.locator(SLOT_1_RAW)).to_have_value("50")


def test_unreadable_record_written_by_another_tab_hides_the_stored_choice(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    page = open_sheet(sheet_browser, context=context)
    put_record(page, {"sheet_id": SHEET_ID, "storage_revision": 5, "draft": {"kind": "corrupt"}})

    page.locator(SLOT_1_RAW).fill("12")

    expect(page.locator("#draft-conflict")).to_be_visible()
    expect(page.locator("#draft-use-stored")).to_be_hidden()
    expect(page.locator("#draft-use-current")).to_be_visible()
    assert_input_and_json_survive(page, "12")
    stored = get_record(page, SHEET_ID)
    assert stored is not None and stored["draft"] == {"kind": "corrupt"}

    page.locator("#draft-use-current").click()
    page.locator("#draft-confirm-replace").click()
    wait_saved(page)
    reload_and_wait(page)
    expect(page.locator(SLOT_1_RAW)).to_have_value("12")


def test_other_sheets_do_not_interfere_and_a_changed_generation_is_not_overwritten(
    sheet_browser: SheetBrowser,
) -> None:
    other_id = "d" * 32
    context = sheet_browser.new_context()
    first = open_sheet(sheet_browser, html=sheet_html_for(SHEET_ID), name="a.html", context=context)
    second = open_sheet(
        sheet_browser, html=sheet_html_for(other_id), name="b.html", context=context
    )
    first.locator(SLOT_1_RAW).fill("1")
    second.locator(SLOT_1_RAW).fill("2")
    wait_saved(first)
    wait_saved(second)

    reload_and_wait(first)
    reload_and_wait(second)
    expect(first.locator(SLOT_1_RAW)).to_have_value("1")
    expect(second.locator(SLOT_1_RAW)).to_have_value("2")
    expect(status(first)).to_have_attribute("data-status", "saved")
    expect(status(second)).to_have_attribute("data-status", "saved")

    regenerated = open_sheet(
        sheet_browser,
        html=sheet_html_for(SHEET_ID, generated_at="2026-02-04 04:00:00"),
        name="c.html",
        context=context,
    )
    expect(status(regenerated)).to_have_attribute("data-status", "error")
    expect(regenerated.locator("#draft-replace")).to_be_visible()
    stored = get_record(regenerated, SHEET_ID)
    assert stored is not None
    assert stored["draft"]["generated_at"] == "2026-02-04 03:00:00"
    assert stored["draft"]["slots"][0]["raw"] == "1"


def test_import_while_conflicted_replaces_the_screen_but_not_the_storage(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    first = open_sheet(sheet_browser, context=context, name="first.html")
    second = open_sheet(sheet_browser, context=context, name="second.html")
    first.locator(SLOT_1_RAW).fill("10")
    wait_saved(first)
    second.locator(SLOT_1_RAW).fill("20")
    expect(second.locator("#draft-conflict")).to_be_visible()
    draft = export_draft(second)
    draft["slots"][0]["raw"] = "77"

    import_text(second, json.dumps(draft))
    second.locator("#draft-confirm-import").click()

    expect(second.locator(SLOT_1_RAW)).to_have_value("77")
    expect(status(second)).to_have_attribute("data-status", "error")
    expect(second.locator("#draft-conflict")).to_be_visible()
    stored = get_record(second, SHEET_ID)
    assert stored is not None
    assert stored["storage_revision"] == 1
    assert stored["draft"]["slots"][0]["raw"] == "10"


def test_import_without_indexeddb_keeps_reporting_unsaved_state(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(
        "Object.defineProperty(window, 'indexedDB', {configurable: true, value: undefined});"
    )
    page = open_sheet(sheet_browser, context=context)
    page.locator("#draft-manual-continue").click()
    draft = export_draft(page)
    draft["slots"][0]["raw"] = "7"

    import_text(page, json.dumps(draft))
    page.locator("#draft-confirm-import").click()

    expect(page.locator(SLOT_1_RAW)).to_have_value("7")
    expect(status(page)).to_have_attribute("data-status", "error")
    expect(status(page)).to_contain_text("保存失敗")
