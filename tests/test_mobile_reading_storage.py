from __future__ import annotations

import json
from pathlib import Path

from conftest import SheetBrowser
from mobile_sheet_helpers import simple_sheet_html
from playwright.sync_api import BrowserContext, Page, expect


def open_sheet(
    sheet_browser: SheetBrowser,
    *,
    name: str = "draft-sheet.html",
    context: BrowserContext | None = None,
) -> Page:
    page = sheet_browser.open_html(simple_sheet_html(), name=name, context=context)
    page.wait_for_function(
        "() => { const node = document.querySelector('#draft-storage-status'); "
        "return node && node.dataset.status !== 'loading'; }"
    )
    return page


def wait_saved(page: Page) -> None:
    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "saved")


def test_autosave_restores_partial_and_invalid_input_after_reload(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    page = open_sheet(sheet_browser, context=context)

    page.locator(".card[data-slot-id='slot-1'] .raw").fill("12..3")
    wait_saved(page)
    expect(page.locator(".card[data-slot-id='slot-1']")).to_have_attribute(
        "data-confirmed", "false"
    )

    page.reload()
    page.wait_for_function(
        "() => document.querySelector('#draft-storage-status')?.dataset.status === 'saved'"
    )
    expect(page.locator(".card[data-slot-id='slot-1'] .raw")).to_have_value("12..3")
    expect(page.locator(".card[data-slot-id='slot-1']")).to_have_attribute(
        "data-confirmed", "false"
    )


def test_write_failure_keeps_input_and_allows_json_backup(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(
        """
        IDBObjectStore.prototype.put = function () {
          throw new DOMException("テスト用の書込失敗", "QuotaExceededError");
        };
        """
    )
    page = open_sheet(sheet_browser, context=context)

    page.locator(".card[data-slot-id='slot-1'] .raw").fill("不正入力")
    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-storage-message")).to_contain_text("保存できませんでした")
    expect(page.locator(".card[data-slot-id='slot-1'] .raw")).to_have_value("不正入力")

    page.locator("#draft-show").click()
    exported = json.loads(page.locator("#draft-export-text").input_value())
    assert exported["slots"][0]["raw"] == "不正入力"


def test_open_failure_can_continue_with_manual_backup(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(
        """
        IDBFactory.prototype.open = function () {
          throw new DOMException("テスト用の接続失敗", "SecurityError");
        };
        """
    )
    page = open_sheet(sheet_browser, context=context)

    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "error")
    page.locator("#draft-manual-continue").click()
    page.locator(".card[data-slot-id='slot-1'] .raw").fill("途中入力")
    page.locator("#draft-show").click()

    exported = json.loads(page.locator("#draft-export-text").input_value())
    assert exported["slots"][0]["raw"] == "途中入力"
    expect(page.locator("#draft-storage-status")).to_contain_text("保存失敗")


def test_conflicting_tabs_require_an_explicit_choice(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    first = open_sheet(sheet_browser, context=context, name="first.html")
    second = open_sheet(sheet_browser, context=context, name="second.html")

    first.locator(".card[data-slot-id='slot-1'] .raw").fill("10")
    wait_saved(first)
    second.locator(".card[data-slot-id='slot-1'] .raw").fill("20")
    expect(second.locator("#draft-storage-status")).to_have_attribute("data-status", "error")
    expect(second.locator("#draft-conflict")).to_be_visible()
    expect(second.locator(".card[data-slot-id='slot-1'] .raw")).to_have_value("20")

    second.locator("#draft-show").click()
    exported = json.loads(second.locator("#draft-export-text").input_value())
    assert exported["slots"][0]["raw"] == "20"

    second.locator("#draft-use-stored").click()
    expect(second.locator(".card[data-slot-id='slot-1'] .raw")).to_have_value("10")
    wait_saved(second)


def test_conflicting_input_can_replace_only_after_confirmation(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    first = open_sheet(sheet_browser, context=context, name="replace-first.html")
    second = open_sheet(sheet_browser, context=context, name="replace-second.html")

    first.locator(".card[data-slot-id='slot-1'] .raw").fill("10")
    wait_saved(first)
    second.locator(".card[data-slot-id='slot-1'] .raw").fill("20")
    expect(second.locator("#draft-conflict")).to_be_visible()
    second.locator("#draft-use-current").click()
    expect(second.locator("#draft-replace-confirmation")).to_be_visible()
    second.locator("#draft-cancel-replace").click()
    expect(second.locator("#draft-replace-confirmation")).to_be_hidden()
    expect(second.locator(".card[data-slot-id='slot-1'] .raw")).to_have_value("20")

    second.locator("#draft-use-current").click()
    second.locator("#draft-confirm-replace").click()
    wait_saved(second)
    first.reload()
    first.wait_for_function(
        "() => document.querySelector('#draft-storage-status')?.dataset.status === 'saved'"
    )
    expect(first.locator(".card[data-slot-id='slot-1'] .raw")).to_have_value("20")


def test_corrupt_storage_is_not_replaced_without_explicit_confirmation(
    sheet_browser: SheetBrowser,
) -> None:
    page = open_sheet(sheet_browser)

    page.evaluate(
        """
        () => new Promise((resolve, reject) => {
          const request = indexedDB.open("mrm_meter_reading", 1);
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            const transaction = request.result.transaction("drafts", "readwrite");
            transaction.objectStore("drafts").put({
              sheet_id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
              storage_revision: 4,
              draft: { kind: "corrupt" },
            });
            transaction.oncomplete = resolve;
            transaction.onerror = () => reject(transaction.error);
          };
        })
        """
    )
    page.reload()
    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-retry")).to_be_visible()
    expect(page.locator("#draft-replace")).to_be_visible()

    page.locator("#draft-retry").click()
    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "error")
    unchanged = page.evaluate(
        """
        () => new Promise((resolve, reject) => {
          const request = indexedDB.open("mrm_meter_reading", 1);
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            const transaction = request.result.transaction("drafts", "readonly");
            const read = transaction.objectStore("drafts").get(
              "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            );
            read.onsuccess = () => resolve(read.result.draft.kind);
            read.onerror = () => reject(read.error);
          };
        })
        """
    )
    assert unchanged == "corrupt"

    page.locator("#draft-replace").click()
    page.locator("#draft-cancel-replace").click()
    assert (
        page.evaluate(
            """
        () => new Promise((resolve, reject) => {
          const request = indexedDB.open("mrm_meter_reading", 1);
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            const transaction = request.result.transaction("drafts", "readonly");
            const read = transaction.objectStore("drafts").get(
              "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            );
            read.onsuccess = () => resolve(read.result.draft.kind);
            read.onerror = () => reject(read.error);
          };
        })
        """
        )
        == "corrupt"
    )

    page.locator("#draft-replace").click()
    page.locator("#draft-confirm-replace").click()
    wait_saved(page)
    page.reload()
    page.wait_for_function(
        "() => document.querySelector('#draft-storage-status')?.dataset.status === 'saved'"
    )
    expect(page.locator(".card[data-slot-id='slot-1'] .raw")).to_have_value("")


def test_latest_edit_stays_saving_until_its_own_transaction_completes(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(
        """
        (() => {
          const descriptor = Object.getOwnPropertyDescriptor(
            IDBTransaction.prototype,
            "oncomplete"
          );
          window.__mrmDelayedWrites = 0;
          let writeNumber = 0;
          Object.defineProperty(IDBTransaction.prototype, "oncomplete", {
            configurable: true,
            get() {
              return descriptor.get.call(this);
            },
            set(handler) {
              if (this.mode !== "readwrite" || typeof handler !== "function") {
                descriptor.set.call(this, handler);
                return;
              }
              const number = ++writeNumber;
              const transaction = this;
              descriptor.set.call(this, event => {
                const delay = number === 1 ? 300 : 700;
                setTimeout(() => {
                  window.__mrmDelayedWrites += 1;
                  handler.call(transaction, event);
                }, delay);
              });
            },
          });
        })();
        """
    )
    page = open_sheet(sheet_browser, context=context)
    raw = page.locator(".card[data-slot-id='slot-1'] .raw")

    raw.fill("10")
    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "saving")
    raw.fill("20")
    page.wait_for_timeout(400)

    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "saving")
    assert page.evaluate("window.__mrmDelayedWrites") == 1
    wait_saved(page)
    assert page.evaluate("window.__mrmDelayedWrites") == 2

    page.reload()
    page.wait_for_function(
        "() => document.querySelector('#draft-storage-status')?.dataset.status === 'saved'"
    )
    expect(page.locator(".card[data-slot-id='slot-1'] .raw")).to_have_value("20")


def test_newer_save_failure_survives_older_write_completion(
    sheet_browser: SheetBrowser,
) -> None:
    context = sheet_browser.new_context()
    context.add_init_script(
        """
        (() => {
          const originalPut = IDBObjectStore.prototype.put;
          let putCount = 0;
          IDBObjectStore.prototype.put = function (...args) {
            putCount += 1;
            if (putCount === 2) {
              throw new DOMException("テスト用の後続書込失敗", "QuotaExceededError");
            }
            return originalPut.apply(this, args);
          };
          const descriptor = Object.getOwnPropertyDescriptor(
            IDBTransaction.prototype,
            "oncomplete"
          );
          let writeNumber = 0;
          Object.defineProperty(IDBTransaction.prototype, "oncomplete", {
            configurable: true,
            get() {
              return descriptor.get.call(this);
            },
            set(handler) {
              if (this.mode !== "readwrite" || typeof handler !== "function") {
                descriptor.set.call(this, handler);
                return;
              }
              const number = ++writeNumber;
              const transaction = this;
              descriptor.set.call(this, event => {
                setTimeout(
                  () => handler.call(transaction, event),
                  number === 1 ? 300 : 0
                );
              });
            },
          });
        })();
        """
    )
    page = open_sheet(sheet_browser, context=context)
    raw = page.locator(".card[data-slot-id='slot-1'] .raw")

    raw.fill("10")
    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "saving")
    raw.fill("20")
    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "error")
    expect(page.locator("#draft-storage-message")).to_contain_text("後続書込失敗")
    expect(raw).to_have_value("20")

    page.locator("#draft-show").click()
    exported = json.loads(page.locator("#draft-export-text").input_value())
    assert exported["slots"][0]["raw"] == "20"


def test_draft_file_and_text_round_trip_require_confirmation(
    sheet_browser: SheetBrowser,
    tmp_path: Path,
) -> None:
    page = open_sheet(sheet_browser)
    raw = page.locator(".card[data-slot-id='slot-1'] .raw")
    raw.fill("1.2")
    wait_saved(page)

    with page.expect_download() as download_info:
        page.locator("#draft-download").click()
    download = download_info.value
    backup_path = tmp_path / download.suggested_filename
    download.save_as(backup_path)
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    assert backup["slots"][0]["raw"] == "1.2"

    raw.fill("2.3")
    page.locator("#draft-file").set_input_files(backup_path)
    expect(page.locator("#draft-preview")).to_be_visible()
    page.locator("#draft-cancel-import").click()
    expect(raw).to_have_value("2.3")

    page.locator("#draft-file").set_input_files(backup_path)
    page.locator("#draft-confirm-import").click()
    expect(raw).to_have_value("1.2")
    wait_saved(page)

    page.locator("#draft-show").click()
    text_backup = page.locator("#draft-export-text").input_value()
    raw.fill("3.4")
    page.locator("#draft-import-text").fill(text_backup)
    page.locator("#draft-import").click()
    expect(page.locator("#draft-preview")).to_be_visible()
    page.locator("#draft-confirm-import").click()
    expect(raw).to_have_value("1.2")
    wait_saved(page)


def test_malformed_result_mr1_and_other_sheet_are_rejected(
    sheet_browser: SheetBrowser,
) -> None:
    page = open_sheet(sheet_browser)
    import_box = page.locator("#draft-import-text")

    for text, message in (
        ("{broken", "途中経過 JSON を読み込めません"),
        ('{"kind":"mrm-meter-reading-result","v":1}', "結果 JSON"),
        ("MR1:YWJj.00000000:END", "MR1 は結果コード"),
    ):
        import_box.fill(text)
        page.locator("#draft-import").click()
        expect(page.locator("#draft-feedback")).to_contain_text(message)
        expect(page.locator("#draft-preview")).to_be_hidden()

    page.locator("#draft-show").click()
    other_sheet = json.loads(page.locator("#draft-export-text").input_value())
    other_sheet["sheet_id"] = "c" * 32
    import_box.fill(json.dumps(other_sheet))
    page.locator("#draft-import").click()
    expect(page.locator("#draft-feedback")).to_contain_text("別の検針シート")
    expect(page.locator("#draft-preview")).to_be_hidden()


def test_without_indexeddb_manual_json_can_be_recovered(
    sheet_browser: SheetBrowser,
) -> None:
    unavailable_context = sheet_browser.new_context()
    unavailable_context.add_init_script(
        """
        Object.defineProperty(window, "indexedDB", {
          configurable: true,
          value: undefined,
        });
        """
    )
    manual = open_sheet(sheet_browser, context=unavailable_context)
    expect(manual.locator("#draft-storage-status")).to_have_attribute("data-status", "error")
    manual.locator("#draft-manual-continue").click()
    manual.locator(".card[data-slot-id='slot-1'] .raw").fill("退避値")
    manual.locator("#draft-show").click()
    backup_text = manual.locator("#draft-export-text").input_value()
    assert json.loads(backup_text)["slots"][0]["raw"] == "退避値"

    restored = open_sheet(sheet_browser, name="restored.html")
    restored.locator("#draft-import-text").fill(backup_text)
    restored.locator("#draft-import").click()
    expect(restored.locator("#draft-preview")).to_be_visible()
    restored.locator("#draft-confirm-import").click()
    expect(restored.locator(".card[data-slot-id='slot-1'] .raw")).to_have_value("退避値")
    wait_saved(restored)
