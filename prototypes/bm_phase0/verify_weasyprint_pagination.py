from __future__ import annotations

import base64
import io
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageChops, ImageStat
from weasyprint import HTML
from weasyprint.urls import URLFetcher

OUTPUT = Path(__file__).resolve().parent / "out" / "pdf"
MAX_POINTS = 200 * 72


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def boxes(box: Any, tag: str, kind: str) -> list[Any]:
    result = []
    if box.element_tag == tag and type(box).__name__ == kind:
        result.append(box)
    for child in getattr(box, "children", ()):
        result.extend(boxes(child, tag, kind))
    return result


def render(source: str) -> tuple[Any, Any]:
    source = source.replace("size: 210mm auto;", "size: 210mm 5080mm;")
    fetcher = URLFetcher(allowed_protocols=("data",), fail_on_errors=True)
    document = HTML(string=source, url_fetcher=fetcher).render()
    return document, pymupdf.open(stream=document.write_pdf(), filetype="pdf")


def check(source: str) -> dict[str, Any]:
    document, pdf = render(source)
    if len(pdf) < 2:
        raise AssertionError("200 インチを超える試料が複数ページになっていません。")

    occurrences: Counter[int] = Counter()
    image_count = 0
    row_count = 0
    max_difference = 0.0
    images_per_page = []
    rows_per_page = []
    for layout_page, pdf_page in zip(document.pages, pdf, strict=True):
        if any(
            bounds.width > MAX_POINTS or bounds.height > MAX_POINTS
            for bounds in (pdf_page.mediabox, pdf_page.rect)
        ):
            raise AssertionError("PDF のページ寸法が 200 インチを超えています。")
        page_text = compact(pdf_page.get_text())
        rows = boxes(layout_page._page_box, "tr", "TableRowBox")
        images = boxes(layout_page._page_box, "img", "BlockReplacedBox")
        images_per_page.append(len(images))
        rows_per_page.append(len(rows))
        pdf_images = pdf_page.get_image_info()
        if len(images) != len(pdf_images):
            raise AssertionError("PDF に描画された画像数が組版結果と一致しません。")
        content_top = layout_page._page_box.content_box_y()
        content_bottom = content_top + layout_page._page_box.height
        for row in rows:
            occurrences[id(row.element)] += 1
            text = compact("".join(row.element.itertext()))
            if text not in page_text:
                raise AssertionError("表の行の内容が同じ PDF ページに収まっていません。")
            top = row.position_y
            bottom = top + row.height
            if top < content_top or bottom > content_bottom:
                raise AssertionError("表の行が本文領域の外にはみ出しています。")
            row_count += 1
        for image, info in zip(images, pdf_images, strict=True):
            occurrences[id(image.element)] += 1
            rect = pymupdf.Rect(info["bbox"])
            expected = pymupdf.Rect(
                image.position_x * 0.75,
                image.position_y * 0.75,
                (image.position_x + image.width) * 0.75,
                (image.position_y + image.height) * 0.75,
            )
            if any(abs(a - b) > 0.1 for a, b in zip(rect, expected, strict=True)):
                raise AssertionError("PDF の画像矩形が組版結果と一致しません。")
            if not pdf_page.rect.contains(rect):
                raise AssertionError("画像がページの外にはみ出しています。")
            if image.position_y < content_top or image.position_y + image.height > content_bottom:
                raise AssertionError("画像が本文領域の外にはみ出しています。")

            data_uri = image.element.get("src")
            original = Image.open(io.BytesIO(base64.b64decode(data_uri.split(",", 1)[1])))
            pixmap = pdf_page.get_pixmap(clip=rect, alpha=False)
            actual = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            expected_image = original.convert("RGB").resize(actual.size)
            difference = max(ImageStat.Stat(ImageChops.difference(actual, expected_image)).mean)
            if difference > 15:
                raise AssertionError(f"PDF の画像が元画像と一致しません: 差 {difference:.2f}")
            max_difference = max(max_difference, difference)
            image_count += 1
    if any(count != 1 for count in occurrences.values()):
        raise AssertionError("画像または表行が複数ページに分割されています。")
    return {
        "pages": len(pdf),
        "page_sizes_pt": [[round(p.rect.width, 3), round(p.rect.height, 3)] for p in pdf],
        "images": image_count,
        "rows": row_count,
        "images_per_page": images_per_page,
        "rows_per_page": rows_per_page,
        "maximum_image_mean_difference": round(max_difference, 2),
        "page_text": [compact(p.get_text()) for p in pdf],
    }


def fixture(source: str, content: str, extra_css: str = "") -> str:
    style = re.search(r"<style>(.*?)</style>", source, re.DOTALL)
    if style is None:
        raise ValueError("試料にスタイルがありません。")
    return (
        f"<html><head><style>{style.group(1)}{extra_css}</style></head>"
        f'<body><article class="daily">{content}</article></body></html>'
    )


def main() -> None:
    source = (OUTPUT / "long_daily.html").read_text(encoding="utf-8")
    image = re.search(r'<img src="(data:image/jpeg;base64,[^"]+)"', source)
    if image is None:
        raise ValueError("試料に写真がありません。")

    original = check(source)
    if original["images"] != 12 or original["rows"] != 5:
        raise AssertionError("元の試料の写真または表行が欠落しています。")

    photo_source = fixture(
        source,
        '<div style="height:5000mm">PHOTO-PREFIX</div>'
        f'<div class="photos"><figure><img src="{image.group(1)}">'
        "<figcaption>PHOTO-END</figcaption></figure></div>",
    )
    photo = check(photo_source)
    if photo["images_per_page"] != [0, 1] or "PHOTO-PREFIX" not in photo["page_text"][0]:
        raise AssertionError("写真境界試料が期待した配置になっていません。")
    if "PHOTO-END" not in photo["page_text"][1]:
        raise AssertionError("境界の写真が次ページへ移動していません。")

    row_source = fixture(
        source,
        '<div style="height:5000mm">ROW-PREFIX</div>'
        "<table><tbody><tr><td>ROW-FIRST</td></tr>"
        '<tr style="height:60mm"><td>ROW-START<br>ROW-END</td></tr></tbody></table>',
        ".daily table { break-inside: auto; }",
    )
    row = check(row_source)
    if row["rows_per_page"] != [1, 1] or "ROW-FIRST" not in row["page_text"][0]:
        raise AssertionError("表境界試料の先行行が 1 ページ目にありません。")
    if not all(marker in row["page_text"][1] for marker in ("ROW-START", "ROW-END")):
        raise AssertionError("境界の表行が次ページへ移動していません。")

    oversized = fixture(
        source,
        '<table><tr style="height:5100mm">'
        '<td style="vertical-align:bottom">OVERSIZED-ROW-END</td></tr></table>',
    )
    oversized_document, oversized_pdf = render(oversized)
    oversized_page = oversized_document.pages[0]
    oversized_row = boxes(oversized_page._page_box, "tr", "TableRowBox")[0]
    content_bottom = oversized_page._page_box.content_box_y() + oversized_page._page_box.height
    if oversized_row.position_y + oversized_row.height <= content_bottom:
        raise AssertionError("上限を超える行のはみ出しを再現できませんでした。")
    marker_in_pdf = any("OVERSIZED-ROW-END" in page.get_text() for page in oversized_pdf)
    if marker_in_pdf:
        raise AssertionError("上限を超える行の末尾が PDF に残っていました。")
    oversized_result = {
        "pages": len(oversized_pdf),
        "row_height_mm": round(oversized_row.height * 25.4 / 96, 1),
        "content_height_mm": round(oversized_page._page_box.height * 25.4 / 96, 1),
        "overflows_content": True,
        "end_marker_in_pdf": marker_in_pdf,
    }

    for result in (original, photo, row):
        del result["page_text"]
    print(
        json.dumps(
            {
                "original": original,
                "photo_boundary": photo,
                "row_boundary": row,
                "oversized_row": oversized_result,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
