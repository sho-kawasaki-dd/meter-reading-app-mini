"""途中経過の保存・復元ブラウザテスト用の補助。"""

from __future__ import annotations

import json
from typing import Any

from conftest import SheetBrowser
from mobile_sheet_helpers import simple_sheet_html
from playwright.sync_api import BrowserContext, Page, expect

SLOT_1_RAW = ".card[data-slot-id='slot-1'] .raw"

STATUS_RECORDER_SCRIPT = """
window.__mrmStatuses = [];
new MutationObserver(records => {
  for (const record of records) {
    if (record.target.id === "draft-storage-status") {
      window.__mrmStatuses.push(record.target.dataset.status);
    }
  }
}).observe(document, {
  attributes: true,
  attributeFilter: ["data-status"],
  subtree: true,
});
"""

SHORT_TIMEOUT_SCRIPT = """
(() => {
  const originalSetTimeout = window.setTimeout;
  window.setTimeout = function (handler, delay, ...args) {
    return originalSetTimeout.call(this, handler, delay === 5000 ? 150 : delay, ...args);
  };
})();
"""


_ONCOMPLETE_PATCH = """
(() => {
  const descriptor = Object.getOwnPropertyDescriptor(IDBTransaction.prototype, "oncomplete");
  let number = 0;
  Object.defineProperty(IDBTransaction.prototype, "oncomplete", {
    configurable: true,
    get() { return descriptor.get.call(this); },
    set(handler) {
      if (this.mode !== "__MODE__" || typeof handler !== "function") {
        descriptor.set.call(this, handler);
        return;
      }
      number += 1;
      const delay = (__DELAY__);
      if (delay === null) { return; }
      const transaction = this;
      descriptor.set.call(this, event => {
        setTimeout(() => handler.call(transaction, event), delay);
      });
    },
  });
})();
"""


def oncomplete_patch(mode: str, delay_expression: str) -> str:
    """指定モードのトランザクション完了通知を遅延する。式が null なら完了を通知しない。

    式では `number` (そのモードで何番目のトランザクションか) を参照できる。
    """

    return _ONCOMPLETE_PATCH.replace("__MODE__", mode).replace("__DELAY__", delay_expression)


def wait_settled(page: Page) -> None:
    page.wait_for_function(
        "() => { const node = document.querySelector('#draft-storage-status'); "
        "return node && node.dataset.status !== 'loading'; }"
    )


def open_sheet(
    sheet_browser: SheetBrowser,
    *,
    html: str | None = None,
    name: str = "draft-sheet.html",
    context: BrowserContext | None = None,
    viewport: tuple[int, int] = (360, 640),
) -> Page:
    page = sheet_browser.open_html(
        html if html is not None else simple_sheet_html(),
        name=name,
        context=context,
        viewport=viewport,
    )
    wait_settled(page)
    return page


def wait_saved(page: Page) -> None:
    expect(page.locator("#draft-storage-status")).to_have_attribute("data-status", "saved")


def reload_and_wait(page: Page) -> None:
    page.reload()
    wait_settled(page)


def export_draft(page: Page) -> dict[str, Any]:
    page.locator("#draft-show").click()
    return json.loads(page.locator("#draft-export-text").input_value())


def import_text(page: Page, text: str) -> None:
    page.locator("#draft-import-text").fill(text)
    page.locator("#draft-import").click()


_PUT_RECORD_SCRIPT = """
record => new Promise((resolve, reject) => {
  const request = indexedDB.open("mrm_meter_reading", 1);
  request.onerror = () => reject(request.error);
  request.onsuccess = () => {
    const database = request.result;
    const transaction = database.transaction("drafts", "readwrite");
    transaction.objectStore("drafts").put(record);
    transaction.oncomplete = () => { database.close(); resolve(); };
    transaction.onerror = () => reject(transaction.error);
  };
})
"""

_GET_RECORD_SCRIPT = """
sheetId => new Promise((resolve, reject) => {
  const request = indexedDB.open("mrm_meter_reading", 1);
  request.onerror = () => reject(request.error);
  request.onsuccess = () => {
    const database = request.result;
    const read = database.transaction("drafts", "readonly").objectStore("drafts").get(sheetId);
    read.onsuccess = () => { database.close(); resolve(read.result ?? null); };
    read.onerror = () => reject(read.error);
  };
})
"""


def put_record(page: Page, record: dict[str, Any]) -> None:
    page.evaluate(_PUT_RECORD_SCRIPT, record)


def get_record(page: Page, sheet_id: str) -> dict[str, Any] | None:
    return page.evaluate(_GET_RECORD_SCRIPT, sheet_id)
