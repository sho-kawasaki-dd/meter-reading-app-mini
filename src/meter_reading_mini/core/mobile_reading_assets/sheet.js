(function () {
  "use strict";

  var app = document.getElementById("app");
  var sheet;
  try {
    sheet = JSON.parse(document.getElementById("mrm-sheet-data").textContent);
  } catch (error) {
    app.textContent = "検針シートのデータを読み込めませんでした。";
    return;
  }
  if (!sheet || sheet.kind !== "mrm-meter-reading-sheet" || sheet.v !== 1) {
    app.textContent = "この検針シートの版数には対応していません。";
    return;
  }

  var header = document.createElement("header");
  header.className = "sheet-header";
  var title = document.createElement("h1");
  title.textContent = "検針シート " + sheet.business_date;
  var meta = document.createElement("p");
  meta.className = "sheet-meta";
  meta.textContent =
    "出力日時 " + sheet.generated_at_local + " / 検針枠 " + sheet.slots.length + " 件";
  header.appendChild(title);
  header.appendChild(meta);
  app.insertBefore(header, app.firstChild);
})();
