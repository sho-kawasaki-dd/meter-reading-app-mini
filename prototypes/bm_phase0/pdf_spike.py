from __future__ import annotations

import argparse
import base64
import json
import random
import re
import sys
import time
import unicodedata
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "out" / "pdf"
FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansjp/NotoSansJP%5Bwght%5D.ttf"
)
LICENSE_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansjp/OFL.txt"
MAX_PAGE_HEIGHT_MM = 5080
PAGE_WIDTH_MM = 210
HTML_LIMIT = 2 * 1024 * 1024


class TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


@dataclass
class PdfResult:
    engine: str
    sample: str
    html_bytes: int
    pdf_bytes: int
    pages: int
    maximum_page_height_mm: float
    maximum_page_width_mm: float
    pages_within_200_inch_limit: bool
    page_count_as_expected: bool
    all_pages_have_text: bool
    all_pages_have_header_and_number: bool
    content_matches_samples: bool
    japanese_kinsoku_check: bool
    all_monthly_days_start_on_new_pages: bool
    continued_marker_on_later_pages: bool
    boundary_images_kept_together: bool
    boundary_table_rows_kept_together: bool
    has_embedded_font: bool
    has_embedded_font_program: bool
    has_type3_glyph_outlines: bool
    embedded_images: int
    render_seconds: float
    has_annotations: bool
    has_attachments: bool
    has_javascript: bool
    metadata: dict[str, str]


def _download(url: str, destination: Path) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "meter-reading-mini-bm-phase0-pdf-spike/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} while downloading {url}")
        data = response.read()
    destination.write_bytes(data)
    return data


def _image_data_uri(image_number: int) -> str:
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1200, 900), (222, 231, 234))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 570, 1200, 900), fill=(117, 143, 126))
    draw.rectangle((90, 150, 720, 680), fill=(183, 197, 200))
    draw.rectangle((170, 260, 340, 470), fill=(96, 123, 137))
    draw.rectangle((420, 260, 590, 470), fill=(96, 123, 137))
    draw.ellipse((800, 170, 1030, 400), fill=(232, 193, 110))
    draw.text((40, 35), f"架空の点検写真 {image_number:02}", fill=(25, 35, 40))
    noise = random.Random(image_number)
    for _ in range(2500):
        x = noise.randrange(1200)
        y = noise.randrange(900)
        shade = noise.randrange(-18, 19)
        color = tuple(max(0, min(255, channel + shade)) for channel in (150, 160, 155))
        draw.rectangle((x, y, x + 7, y + 7), fill=color)
    image.thumbnail((800, 800))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=75, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _font_data_uri() -> str:
    return "data:font/ttf;base64," + base64.b64encode(
        (OUTPUT / "NotoSansJP-subset.ttf").read_bytes()
    ).decode("ascii")


def _font_subset(source: Path, destination: Path, text: str) -> None:
    from fontTools.subset import Options, Subsetter
    from fontTools.ttLib import TTFont

    font = TTFont(source)
    subsetter = Subsetter(options=Options())
    subsetter.populate(text=text)
    subsetter.subset(font)
    font.save(destination)


def _css(font_uri: str) -> str:
    monthly_page_rules = "".join(
        f"""
@page monthly-day-{day:02} {{
  size: {PAGE_WIDTH_MM}mm auto;
  margin: 16mm 12mm 17mm;
  @top-left {{ content: "架空の青葉管理株式会社"; font-size: 8pt; }}
  @top-center {{ content: "2026年11月{day:02}日  版 1.0"; font-size: 8pt; }}
  @top-right {{ content: "ページ " counter(page) " / " counter(pages); font-size: 8pt; }}
}}
"""
        for day in range(1, 32)
    )
    return f"""
@font-face {{
  font-family: "Noto Sans JP";
  src: url("{font_uri}") format("truetype");
  font-weight: 100 900;
}}
* {{ box-sizing: border-box; }}
body {{ font: 10pt/1.7 "Noto Sans JP", sans-serif; color: #202b30; margin: 0; }}
h1 {{ font-size: 19pt; margin: 0 0 5mm; }}
h2 {{ font-size: 13pt; margin: 6mm 0 2mm; }}
h3 {{ font-size: 11pt; margin: 3mm 0 1mm; }}
p {{ margin: 0 0 2mm; }}
table {{ border-collapse: collapse; width: 100%; margin: 2mm 0 4mm; }}
th, td {{ border: 0.3mm solid #718087; padding: 1.5mm 2mm; text-align: left; }}
th {{ background: #e8eef0; }}
.photos {{ display: grid; grid-template-columns: repeat(2, 80mm); gap: 4mm; }}
.photos figure {{ margin: 0; width: 80mm; break-inside: avoid; }}
.photos img {{ display: block; width: 80mm; height: auto; }}
.photos figcaption {{ font-size: 8pt; }}
.daily table, .monthly-day table, .daily tr, .monthly-day tr {{
  break-inside: avoid;
}}
table.boundary-table td:first-child {{ width: 26mm; }}
.daily {{ break-before: page; page: daily; }}
.daily:first-child {{ break-before: auto; }}
.monthly-day {{ break-before: page; }}
.monthly-day:first-child {{ break-before: auto; }}
.long-text p {{ margin-bottom: 4mm; orphans: 3; widows: 3; }}
@page {{
  size: {PAGE_WIDTH_MM}mm auto;
  margin: 16mm 12mm 17mm;
  @top-right {{ content: "ページ " counter(page) " / " counter(pages); font-size: 8pt; }}
}}
@page daily {{
  size: {PAGE_WIDTH_MM}mm auto;
  margin: 16mm 12mm 17mm;
  @top-left {{ content: "架空の青葉管理株式会社"; font-size: 8pt; }}
  @top-center {{ content: "2026年11月28日  版 1.0"; font-size: 8pt; }}
  @top-right {{ content: "ページ " counter(page) " / " counter(pages); font-size: 8pt; }}
}}
@page daily:first {{
  @bottom-left {{ content: none; }}
}}
@page daily {{
  @bottom-left {{ content: "前ページからの続き"; font-size: 8pt; }}
}}
{monthly_page_rules}
"""


def _photos_html(photos: list[str], caption_prefix: str = "設備の確認") -> str:
    return "".join(
        f'<figure><img src="{uri}" alt="架空の点検写真 {index + 1}">'
        f"<figcaption>{caption_prefix} {index + 1}</figcaption></figure>"
        for index, uri in enumerate(photos)
    )


def _day_content(day: int, photos: list[str], long: bool = False, monthly: bool = False) -> str:
    date = f"2026年11月{day:02}日"
    article_class = "monthly-day" if monthly else "daily"
    page_name = f' style="page: monthly-day-{day:02}"' if monthly else ""
    photo_html = _photos_html(photos)
    long_text = ""
    if long:
        paragraph = (
            "共用部の設備を確認し、異音や漏水の有無、表示値、周囲の状態を記録した。"
            "担当者は安全を確認してから作業し、異常が見つかった場合は関係先へ連絡する。"
        )
        entries = ['<section class="long-text"><h2>長文の個別対応記録</h2>']
        for index in range(340):
            entries.append(f"<p>{paragraph} 記録番号 {index + 1:02}</p>")
            if index == 279:
                entries.append(
                    f'<div class="photos">{_photos_html(photos[:2], "写真境界確認")}</div>'
                )
                row_text = "境界で分割せずに配置する架空の表データ。" * 30
                entries.append(
                    '<table class="boundary-table"><tbody>'
                    "<tr><td>境界表A開始</td>"
                    f"<td>{row_text}境界表A終了</td></tr>"
                    "<tr><td>境界表B開始</td><td>表の次の行</td></tr>"
                    "</tbody></table>"
                )
        long_text = "".join((*entries, "</section>"))
    return f"""
<article class="{article_class}"{page_name}>
  <h1>{date} 業務日誌</h1>
  <p>クライアント: 架空の青葉管理株式会社　対象: 青葉サンプル棟</p>
  <h2>検針・設備の値</h2>
  <table><thead><tr><th>項目</th><th>使用量</th><th>最大値</th><th>最小値</th><th>品質</th></tr></thead>
  <tbody>
    <tr><td>給水メーター</td><td>12.50 m³</td><td>0.82 m³/h</td><td>0.01 m³/h</td><td>正常</td></tr>
    <tr><td>電力量</td><td>148.75 kWh</td><td>9.20 kW</td><td>0.30 kW</td><td>準正常値 )</td></tr>
  </tbody></table>
  <h2>天気・現場状況</h2>
  <p>天気: 晴れのち曇り　気温: 14.2 ℃ / 7.1 ℃　湿度: 58 %</p>
  <h2>複数日にまたがる個別対応事項</h2>
  <p>11月26日から11月29日まで、受水槽まわりの振動を継続確認する。初日は現況を撮影し、翌日に固定部を点検した。</p>
  <p>長い日本語の文章でも、句読点や括弧を含む段落が適切に折り返され、表と写真が読みやすく配置されることを
  確認するための架空記録です。品質記号 ] や欠測 × も値と区別して表示します。</p>
  <section><h2>写真</h2><div class="photos">{photo_html}</div></section>
  {long_text}
</article>"""


def _build_html(samples: dict[str, str], font_uri: str) -> None:
    css = _css(font_uri)
    csp = (
        "default-src 'none'; img-src data:; font-src data:; "
        "style-src 'unsafe-inline'; script-src 'none'; connect-src 'none';"
    )
    for name, body in samples.items():
        document = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>架空の業務日誌 技術検証</title><style>{css}</style></head>
<body><main>
{body}
</main></body></html>"""
        (OUTPUT / f"{name}.html").write_text(document, encoding="utf-8")


def prepare() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    font_path = OUTPUT / "NotoSansJP[wght].ttf"
    license_path = OUTPUT / "OFL.txt"
    if not font_path.exists():
        _download(FONT_URL, font_path)
    if not license_path.exists():
        _download(LICENSE_URL, license_path)

    photos = [_image_data_uri(index + 1) for index in range(10)]
    html_parts = [
        _day_content(28, photos),
        _day_content(28, photos, long=True),
        "".join(
            _day_content(
                day,
                photos[(day % 10) : (day % 10) + 2] or photos[:2],
                monthly=True,
            )
            for day in range(1, 32)
        ),
    ]
    collector = TextCollector()
    for part in html_parts:
        collector.feed(part)
    text = "".join(collector.parts) + "ページ前ページからの続き"
    subset_path = OUTPUT / "NotoSansJP-subset.ttf"
    if not subset_path.exists():
        _font_subset(font_path, subset_path, text)
    font_uri = _font_data_uri()
    _build_html(
        dict(zip(("daily", "long_daily", "monthly"), html_parts, strict=True)),
        font_uri,
    )

    sizes = {path.name: path.stat().st_size for path in sorted(OUTPUT.glob("*.html"))}
    report = {
        "font_source": FONT_URL,
        "font_license": "SIL Open Font License 1.1",
        "font_bytes": font_path.stat().st_size,
        "subset_font_bytes": subset_path.stat().st_size,
        "photo_count": len(photos),
        "photo_dimensions": [800, 600],
        "jpeg_quality": 75,
        "html_bytes": sizes,
        "setHtml_limit_bytes": HTML_LIMIT,
        "html_exceeds_setHtml_limit": {name: size > HTML_LIMIT for name, size in sizes.items()},
    }
    (OUTPUT / "samples.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _render_weasyprint() -> list[PdfResult]:
    from pypdf import PdfReader, PdfWriter
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    data_only_fetcher = URLFetcher(allowed_protocols=("data",), fail_on_errors=True)
    results = []
    for name in ("daily", "long_daily", "monthly"):
        sample_started = time.perf_counter()
        html_path = OUTPUT / f"{name}.html"
        pdf_path = OUTPUT / f"weasyprint-{name}.pdf"
        source = html_path.read_text(encoding="utf-8")
        if name == "daily":
            measure_source = source.replace(
                "size: 210mm auto;", f"size: 210mm {MAX_PAGE_HEIGHT_MM}mm;"
            )
            measured = HTML(string=measure_source, url_fetcher=data_only_fetcher).render()
            main_box = _find_box(measured.pages[0]._page_box, "main")
            height_mm = main_box.height * 25.4 / 96 + 16 + 17 + 2
            page_size = f"{PAGE_WIDTH_MM}mm {height_mm:.0f}mm"
        elif name == "long_daily":
            page_size = f"{PAGE_WIDTH_MM}mm {MAX_PAGE_HEIGHT_MM}mm"
        else:
            page_size = f"{PAGE_WIDTH_MM}mm 297mm"
        source = source.replace("size: 210mm auto;", f"size: {page_size};")
        document = HTML(string=source, url_fetcher=data_only_fetcher).render()
        document.write_pdf(str(pdf_path))
        _sanitize_metadata(pdf_path, PdfReader, PdfWriter)
        result = _inspect_pdf("WeasyPrint", name, html_path, pdf_path)
        result.render_seconds = round(time.perf_counter() - sample_started, 3)
        results.append(result)
    return results


def _find_box(box: Any, tag: str) -> Any:
    if getattr(box, "element_tag", None) == tag:
        return box
    for child in getattr(box, "children", []):
        found = _find_box(child, tag)
        if found is not None:
            return found
    raise RuntimeError(f"Could not measure the {tag} element.")


def _render_qt() -> list[PdfResult]:
    from pypdf import PdfReader, PdfWriter
    from PySide6.QtCore import QEventLoop, QMarginsF, QSizeF, Qt, QTimer, QUrl
    from PySide6.QtGui import QPageLayout, QPageSize
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    results = []
    view = QWebEngineView()
    view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    view.resize(round((PAGE_WIDTH_MM - 24) * 96 / 25.4), 900)
    view.show()
    page = view.page()
    settings = page.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
    for name in ("daily", "long_daily", "monthly"):
        sample_started = time.perf_counter()
        html_path = OUTPUT / f"{name}.html"
        pdf_path = OUTPUT / f"qtwebengine-{name}.pdf"
        loop = QEventLoop()
        load_results: list[bool] = []

        def load_finished(
            ok: bool, loop: QEventLoop = loop, results: list[bool] = load_results
        ) -> None:
            results.append(ok)
            loop.quit()

        page.loadFinished.connect(load_finished)
        page.load(QUrl.fromLocalFile(str(html_path)))
        loop.exec()
        page.loadFinished.disconnect(load_finished)
        QTimer.singleShot(200, loop.quit)
        loop.exec()
        if not load_results or not load_results[0]:
            raise RuntimeError(f"Qt WebEngine failed to load {html_path}")
        content_height_px = page.contentsSize().height()
        if content_height_px <= 0:
            raise RuntimeError(f"Qt WebEngine could not measure {html_path}")
        if name == "daily":
            height_mm = content_height_px * 25.4 / 96 + 33 + 5
        elif name == "long_daily":
            height_mm = MAX_PAGE_HEIGHT_MM
        else:
            height_mm = 297
        attempts = 4 if name == "daily" else 1
        for attempt in range(attempts):
            output_path = (
                OUTPUT / f"qtwebengine-{name}-attempt-{attempt + 1}.pdf"
                if attempts > 1
                else pdf_path
            )
            page_size = QPageSize(
                QSizeF(PAGE_WIDTH_MM, height_mm),
                QPageSize.Unit.Millimeter,
                f"BM Phase 0 {name}",
            )
            layout = QPageLayout(
                page_size,
                QPageLayout.Orientation.Portrait,
                QMarginsF(12, 16, 12, 17),
                QPageLayout.Unit.Millimeter,
            )
            loop = QEventLoop()
            print_results: list[bool] = []

            def print_finished(
                _file: str,
                ok: bool,
                loop: QEventLoop = loop,
                results: list[bool] = print_results,
            ) -> None:
                results.append(ok)
                loop.quit()

            page.pdfPrintingFinished.connect(print_finished)
            page.printToPdf(str(output_path), layout)
            loop.exec()
            page.pdfPrintingFinished.disconnect(print_finished)
            app.processEvents()
            if not print_results or not print_results[0] or not output_path.exists():
                raise RuntimeError(f"Qt WebEngine did not create {output_path}")
            if name != "daily" or len(PdfReader(str(output_path)).pages) == 1:
                if output_path != pdf_path:
                    output_path.replace(pdf_path)
                break
            output_path.unlink()
            height_mm = min(MAX_PAGE_HEIGHT_MM, height_mm * 1.25)
        else:
            raise RuntimeError("Qt WebEngine could not fit the daily sample on one page.")
        _sanitize_metadata(pdf_path, PdfReader, PdfWriter)
        result = _inspect_pdf("Qt WebEngine", name, html_path, pdf_path)
        result.render_seconds = round(time.perf_counter() - sample_started, 3)
        results.append(result)
    view.close()
    return results


def _sanitize_metadata(pdf_path: Path, reader_type: Any, writer_type: Any) -> None:
    reader = reader_type(str(pdf_path))
    writer = writer_type()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata(
        {
            "/Title": "BM Phase 0 PDF Test",
            "/Creator": "BM Phase 0",
            "/Producer": "BM Phase 0",
        }
    )
    temporary_path = pdf_path.with_suffix(".sanitized.pdf")
    with temporary_path.open("wb") as stream:
        writer.write(stream)
    temporary_path.replace(pdf_path)


def _inspect_pdf(engine: str, sample: str, html_path: Path, pdf_path: Path) -> PdfResult:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    page_text = [page.extract_text() or "" for page in reader.pages]
    has_embedded_font = False
    has_embedded_font_program = False
    has_type3_glyph_outlines = False
    has_javascript = "/JavaScript" in str(reader.trailer["/Root"])
    has_attachments = "/EmbeddedFiles" in str(reader.trailer["/Root"])
    has_annotations = False
    max_height_mm = 0.0
    max_width_mm = 0.0
    image_count = 0

    def contains_embedded_font(font_object: Any) -> bool:
        font_object = font_object.get_object()
        descriptor = font_object.get("/FontDescriptor")
        if descriptor:
            descriptor = descriptor.get_object()
            if any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                return True
        return any(
            contains_embedded_font(descendant)
            for descendant in font_object.get("/DescendantFonts", [])
        )

    def contains_type3_glyphs(font_object: Any) -> bool:
        font_object = font_object.get_object()
        if font_object.get("/Subtype") == "/Type3" and font_object.get("/CharProcs"):
            return True
        return any(
            contains_type3_glyphs(descendant)
            for descendant in font_object.get("/DescendantFonts", [])
        )

    for page in reader.pages:
        height_mm = float(page.mediabox.height) * 25.4 / 72
        width_mm = float(page.mediabox.width) * 25.4 / 72
        max_height_mm = max(max_height_mm, height_mm)
        max_width_mm = max(max_width_mm, width_mm)
        has_annotations |= bool(page.get("/Annots"))
        for font in page.get("/Resources", {}).get("/Font", {}).values():
            has_embedded_font_program |= contains_embedded_font(font)
            has_type3_glyph_outlines |= contains_type3_glyphs(font)
        has_embedded_font = has_embedded_font_program or has_type3_glyph_outlines
        for item in page.get("/Resources", {}).get("/XObject", {}).values():
            image_count += item.get_object().get("/Subtype") == "/Image"
    metadata = {str(key): str(value) for key, value in (reader.metadata or {}).items()}
    expected_page_counts = {"daily": 1, "long_daily": 2, "monthly": 31}
    expected_dates = (
        [f"2026年11月{day:02}日" for day in range(1, 32)]
        if sample == "monthly"
        else ["2026年11月28日"] * len(page_text)
    )
    required_content = ("使用量", "晴れのち曇り", "複数日にまたがる", "準正常値")
    normalized_text = [
        unicodedata.normalize("NFKC", text).replace("\u2ed8", "青") for text in page_text
    ]
    compact_text = [re.sub(r"\s+", "", text) for text in normalized_text]
    normalized_all_text = "\n".join(normalized_text)
    return PdfResult(
        engine=engine,
        sample=sample,
        html_bytes=html_path.stat().st_size,
        pdf_bytes=pdf_path.stat().st_size,
        pages=len(reader.pages),
        maximum_page_height_mm=round(max_height_mm, 2),
        maximum_page_width_mm=round(max_width_mm, 2),
        pages_within_200_inch_limit=max_height_mm <= MAX_PAGE_HEIGHT_MM
        and max_width_mm <= MAX_PAGE_HEIGHT_MM,
        page_count_as_expected=len(reader.pages) == expected_page_counts[sample],
        all_pages_have_text=all(bool(text.strip()) for text in page_text),
        all_pages_have_header_and_number=all(
            "架空の青葉管理株式会社" in normalized_text[index]
            and expected_dates[index] in normalized_text[index]
            and "版 1.0" in normalized_text[index]
            and f"ページ {index + 1} / {len(page_text)}" in normalized_text[index]
            for index in range(len(page_text))
        ),
        content_matches_samples=all(phrase in normalized_all_text for phrase in required_content),
        japanese_kinsoku_check=not any(
            re.match(r"[、。，．・：；？！ー〜…‥）］｝」』】〕〉》]", line.strip())
            for text in normalized_text
            for line in text.splitlines()
        ),
        all_monthly_days_start_on_new_pages=sample != "monthly"
        or (
            len(page_text) == 31
            and all(f"2026年11月{day:02}日" in page_text[day - 1] for day in range(1, 32))
        ),
        continued_marker_on_later_pages=sample != "long_daily"
        or (
            len(page_text) > 1
            and "前ページからの続き" not in normalized_text[0]
            and all("前ページからの続き" in text for text in normalized_text[1:])
        ),
        boundary_images_kept_together=sample != "long_daily"
        or any("写真境界確認 1" in text and "写真境界確認 2" in text for text in normalized_text),
        boundary_table_rows_kept_together=sample != "long_daily"
        or any(
            all(marker in text for marker in ("境界表A開始", "境界表A終了", "境界表B開始"))
            for text in compact_text
        ),
        has_embedded_font=has_embedded_font,
        has_embedded_font_program=has_embedded_font_program,
        has_type3_glyph_outlines=has_type3_glyph_outlines,
        embedded_images=image_count,
        render_seconds=0.0,
        has_annotations=has_annotations,
        has_attachments=has_attachments,
        has_javascript=has_javascript,
        metadata=metadata,
    )


def render(engine: str) -> None:
    if not (OUTPUT / "daily.html").exists():
        raise FileNotFoundError("Run the prepare command before rendering.")
    results = _render_weasyprint() if engine == "weasyprint" else _render_qt()
    report_path = OUTPUT / f"{engine}-results-{sys.platform}.json"
    report_path.write_text(
        json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(report_path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "render"))
    parser.add_argument("--engine", choices=("weasyprint", "qtwebengine"), default="weasyprint")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    else:
        render(args.engine)


if __name__ == "__main__":
    main()
