"""Server side of the Edit PDF page.

The browser keeps a list of edits (pages and the objects placed on them) and
sends it here to build the finished PDF. An uploaded PDF lives in a private
workspace folder until it expires, so the editor can render pages and read
their text while someone works on it.

All coordinates from the browser are PDF points in the page's *displayed*
orientation (origin top-left, as the page looks in a viewer). PyMuPDF draws in
unrotated page space, so every shape goes through page.derotation_matrix.
"""

import base64
import html
import json
import math
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from converters import ConversionError, open_pdf

WORKSPACE_ROOT = Path(tempfile.gettempdir()) / "network-files-pdf-edit"
WORKSPACE_HOURS = 12
MAX_PAGES = 500
MAX_STATE_BYTES = 60 * 1024 * 1024
MAX_RENDER_WIDTH = 2400
FONT_FAMILIES = {"sans": "sans-serif", "serif": "serif", "mono": "monospace"}
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
WORKSPACE_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")


# ---------------------------------------------------------------- workspaces

def _cleanup():
    if not WORKSPACE_ROOT.exists():
        return
    cutoff = time.time() - WORKSPACE_HOURS * 3600
    for folder in WORKSPACE_ROOT.iterdir():
        try:
            if folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def workspace(doc_id):
    """Return the folder of an open document, or None if it is unknown or expired."""
    if not WORKSPACE_ID.fullmatch(doc_id or ""):
        return None
    folder = WORKSPACE_ROOT / doc_id
    return folder if (folder / "source.pdf").is_file() else None


def create_workspace(upload, name):
    """Save an uploaded PDF and return (doc_id, info)."""
    _cleanup()
    doc_id = secrets.token_urlsafe(18)
    folder = WORKSPACE_ROOT / doc_id
    folder.mkdir(parents=True)
    source = folder / "source.pdf"
    upload.save(source)
    try:
        info = document_info(source, name)
    except ConversionError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    (folder / "meta.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    return doc_id, info


def document_info(source, name):
    document = open_pdf(source)
    try:
        if document.page_count > MAX_PAGES:
            raise ConversionError(f"PDFs with more than {MAX_PAGES} pages can't be edited here.")
        pages = [
            {"width": round(page.rect.width, 2), "height": round(page.rect.height, 2)}
            for page in document
        ]
    finally:
        document.close()
    return {"name": name, "pages": pages}


def load_info(folder):
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    info = document_info(folder / "source.pdf", meta["name"])
    state_file = folder / "state.json"
    if state_file.is_file():
        info["state"] = json.loads(state_file.read_text(encoding="utf-8"))
    return info


def save_state(folder, raw):
    if len(raw) > MAX_STATE_BYTES:
        raise ConversionError("These edits are too large to keep. Download the PDF to save your work.")
    json.loads(raw)  # Only keep valid JSON.
    (folder / "state.json").write_bytes(raw)
    (folder / "meta.json").touch()  # Keeps the workspace from expiring while in use.


def render_page(folder, number, width):
    import pymupdf

    document = pymupdf.open(folder / "source.pdf")
    try:
        if not 0 <= number < document.page_count:
            return None
        page = document[number]
        width = max(80, min(int(width or 1000), MAX_RENDER_WIDTH))
        zoom = width / page.rect.width
        return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
    finally:
        document.close()


def page_text(folder, number):
    """Lines of text on a page, in displayed coordinates, for editing existing text."""
    import pymupdf

    document = pymupdf.open(folder / "source.pdf")
    try:
        if not 0 <= number < document.page_count:
            return None
        page = document[number]
        to_display = page.rotation_matrix
        turn = pymupdf.Matrix(to_display.a, to_display.b, to_display.c, to_display.d, 0, 0)
        lines = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = [span for span in line["spans"] if span["text"].strip()]
                if not spans:
                    continue
                direction = pymupdf.Point(line["dir"]) * turn
                if direction.x < 0.99:
                    continue  # Only text that reads left to right on screen is editable here.
                text = "".join(span["text"] for span in line["spans"]).strip()
                bbox = pymupdf.Rect(line["bbox"]) * to_display
                main = max(spans, key=lambda span: len(span["text"]))
                font = main["font"].lower()
                lines.append({
                    "x": round(bbox.x0, 2), "y": round(bbox.y0, 2),
                    "w": round(bbox.width, 2), "h": round(bbox.height, 2),
                    "text": text,
                    "size": round(main["size"], 1),
                    "color": "#%06x" % main["color"],
                    "bold": bool(main["flags"] & 16) or "bold" in font or "black" in font,
                    "italic": bool(main["flags"] & 2) or "italic" in font or "oblique" in font,
                    "font": "mono" if main["flags"] & 8 or "mono" in font or "courier" in font
                    else "serif" if main["flags"] & 4 or "times" in font or "serif" in font and "sans" not in font
                    else "sans",
                })
        return lines
    finally:
        document.close()


# ------------------------------------------------------------------ building

def _number(value, low=-1e5, high=1e5, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def _color(value, default="#000000"):
    value = value if isinstance(value, str) and COLOR.fullmatch(value) else default
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _hex(value, default="#000000"):
    return value if isinstance(value, str) and COLOR.fullmatch(value) else default


def _rect(item):
    import pymupdf

    x, y = _number(item.get("x")), _number(item.get("y"))
    w, h = _number(item.get("w"), 0, 1e5), _number(item.get("h"), 0, 1e5)
    return pymupdf.Rect(x, y, x + w, y + h)


def _image_bytes(data):
    if not isinstance(data, str):
        raise ConversionError("An image in these edits is missing.")
    match = re.fullmatch(r"data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=\s]+)", data)
    if not match:
        raise ConversionError("An image in these edits is not a supported picture.")
    return base64.b64decode(match.group(2))


def _text_html(text):
    return "<br>".join(html.escape(line) or "&nbsp;" for line in str(text).split("\n"))


def _text_css(item, size):
    align = item.get("align") if item.get("align") in ("left", "center", "right") else "left"
    return (
        "* { margin: 0; padding: 0; }"
        f" body {{ font-family: {FONT_FAMILIES.get(item.get('font'), 'sans-serif')};"
        f" font-size: {size:.2f}px; line-height: 1.2; color: {_hex(item.get('color'))};"
        f" font-weight: {'bold' if item.get('bold') else 'normal'};"
        f" font-style: {'italic' if item.get('italic') else 'normal'};"
        f" text-align: {align}; }}"
    )


def _insert_text(page, rect, item):
    """Write text into a displayed-space rectangle, shrinking it only if it can't fit."""
    size = _number(item.get("size"), 4, 400, 12)
    opacity = _number(item.get("opacity"), 0.05, 1, 1)
    # Leave room so the text keeps its size and line breaks: the browser measured the box
    # with its own fonts, which can be a little narrower than the PDF's.
    extra_width = size * 0.3
    if item.get("autoWidth") and item.get("align", "left") == "left":
        extra_width += rect.width * 0.15 + size
    room = rect + (0, 0, extra_width, size * 0.6)
    page.insert_htmlbox(
        room * page.derotation_matrix,
        _text_html(item.get("text", "")),
        css=_text_css(item, size),
        scale_low=0.3,
        rotate=page.rotation,
        opacity=opacity,
    )


def _draw(page, item):
    import pymupdf

    kind = item.get("type")
    derotate = page.derotation_matrix
    stroke = _color(item.get("color"), "#e5484d")
    fill_value = item.get("fill")
    fill = _color(fill_value) if isinstance(fill_value, str) and COLOR.fullmatch(fill_value) else None
    width = _number(item.get("width"), 0.25, 50, 2)
    opacity = _number(item.get("opacity"), 0.05, 1, 1)
    shape = page.new_shape()
    if kind == "ink":
        for stroke_points in item.get("points", [])[:500]:
            points = [
                pymupdf.Point(_number(point[0]), _number(point[1])) * derotate
                for point in stroke_points[:5000]
                if isinstance(point, (list, tuple)) and len(point) == 2
            ]
            if len(points) == 1:
                points.append(points[0] + (0.01, 0.01))
            if points:
                shape.draw_polyline(points)
        shape.finish(color=stroke, width=width, closePath=False, lineCap=1, lineJoin=1, stroke_opacity=opacity)
    elif kind == "line":
        start = pymupdf.Point(_number(item.get("x1")), _number(item.get("y1"))) * derotate
        end = pymupdf.Point(_number(item.get("x2")), _number(item.get("y2"))) * derotate
        shape.draw_line(start, end)
        if item.get("arrow"):
            # Two short strokes at the end make the arrowhead.
            vector = end - start
            length = abs(vector) or 1
            unit = vector / length
            head = max(6, width * 4)
            normal = pymupdf.Point(-unit.y, unit.x)
            shape.draw_line(end, end - unit * head + normal * head * 0.6)
            shape.draw_line(end, end - unit * head - normal * head * 0.6)
        shape.finish(color=stroke, width=width, lineCap=1, stroke_opacity=opacity)
    else:
        rect = _rect(item) * derotate
        if kind == "ellipse":
            shape.draw_oval(rect)
        else:
            shape.draw_rect(rect)
        border = item.get("border", True)
        shape.finish(
            color=stroke if border else None,
            fill=fill,
            width=width if border else 0,
            stroke_opacity=opacity,
            fill_opacity=_number(item.get("fillOpacity"), 0, 1, opacity),
        )
    shape.commit()


def _apply_objects(page, objects):
    import pymupdf

    derotate = page.derotation_matrix
    edits = [item for item in objects if item.get("type") == "textedit"]
    redactions = [item for item in objects if item.get("type") == "redact"]

    # 1. Remove the original text under edited lines, keeping pictures and drawings.
    for item in edits:
        original = item.get("orig") or item
        box = _rect(original)
        inset = box.height * 0.18
        page.add_redact_annot(pymupdf.Rect(box.x0, box.y0 + inset, box.x1, box.y1 - inset) * derotate)
    if edits:
        page.apply_redactions(images=0, graphics=0, text=0)

    # 2. True redaction: everything under a black box is removed for good.
    for item in redactions:
        page.add_redact_annot(_rect(item) * derotate, fill=_color(item.get("fill"), "#000000"))
    if redactions:
        page.apply_redactions(images=2, graphics=1, text=0)

    # 3. Everything else, in the order it was added.
    for item in objects:
        kind = item.get("type")
        if kind in ("text", "textedit"):
            if str(item.get("text", "")).strip():
                _insert_text(page, _rect(item), item)
        elif kind in ("image", "signature"):
            rect = _rect(item)
            if rect.width > 0 and rect.height > 0:
                page.insert_image(
                    rect * derotate,
                    stream=_image_bytes(item.get("data")),
                    keep_proportion=False,
                    rotate=page.rotation,
                    overlay=True,
                )
        elif kind == "whiteout":
            shape = page.new_shape()
            shape.draw_rect(_rect(item) * derotate)
            shape.finish(color=None, fill=_color(item.get("fill"), "#ffffff"), width=0)
            shape.commit()
        elif kind == "highlight":
            annot = page.add_highlight_annot(_rect(item) * derotate)
            annot.set_colors(stroke=_color(item.get("color"), "#ffd60a"))
            annot.update()
        elif kind == "note":
            point = pymupdf.Point(_number(item.get("x")), _number(item.get("y"))) * derotate
            annot = page.add_text_annot(point, str(item.get("text", ""))[:5000], icon="Comment")
            annot.set_colors(stroke=_color(item.get("color"), "#ffd60a"))
            annot.update()
        elif kind in ("rect", "ellipse", "line", "ink"):
            _draw(page, item)


def _watermark(page, settings, cache):
    import pymupdf

    text = str(settings.get("text", "")).strip()[:200]
    if not text:
        return
    size = _number(settings.get("size"), 8, 300, 60)
    angle = _number(settings.get("angle"), -90, 90, 45)
    if "stamp" not in cache:
        # Lay the text out once on a scratch page, then stamp that page onto every page.
        stamp = pymupdf.open()
        sheet = stamp.new_page(width=size * max(4, len(text)) * 0.8 + 40, height=size * 1.6 + 20)
        sheet.insert_htmlbox(
            sheet.rect + (10, 10, -10, -10),
            _text_html(text),
            css=_text_css({"font": "sans", "bold": True, "color": settings.get("color"), "align": "center"}, size),
            opacity=_number(settings.get("opacity"), 0.05, 1, 0.25),
        )
        used = pymupdf.Rect()
        for block in sheet.get_text("blocks"):
            used |= pymupdf.Rect(block[:4])
        sheet.set_cropbox((used + (-4, -4, 4, 4)) & sheet.rect if not used.is_empty else sheet.rect)
        cache["stamp"] = stamp
    stamp = cache["stamp"]
    stamp_rect = stamp[0].rect
    # Fit the rotated text inside the page with a margin.
    display = page.rect
    radians = math.radians(abs(angle))
    turned_w = stamp_rect.width * math.cos(radians) + stamp_rect.height * math.sin(radians)
    turned_h = stamp_rect.width * math.sin(radians) + stamp_rect.height * math.cos(radians)
    scale = min(1.0, display.width * 0.9 / turned_w, display.height * 0.9 / turned_h)
    w, h = turned_w * scale, turned_h * scale
    target = pymupdf.Rect(
        (display.width - w) / 2, (display.height - h) / 2, (display.width + w) / 2, (display.height + h) / 2
    )
    page.show_pdf_page(
        target * page.derotation_matrix,
        stamp,
        0,
        rotate=angle + page.rotation,
        overlay=settings.get("layer") != "under",
    )


def _page_number(page, settings, number, total):
    import pymupdf

    pattern = str(settings.get("format") or "{n}")[:80]
    text = pattern.replace("{n}", str(number)).replace("{total}", str(total))
    size = _number(settings.get("size"), 6, 48, 10)
    position = settings.get("position", "bottom-center")
    vertical, _, horizontal = position.partition("-")
    margin = 24
    display = page.rect
    top = margin if vertical == "top" else display.height - margin - size * 1.4
    rect = pymupdf.Rect(margin, top, display.width - margin, top + size * 1.6)
    page.insert_htmlbox(
        rect * page.derotation_matrix,
        _text_html(text),
        css=_text_css({"font": "sans", "color": settings.get("color"), "align": horizontal or "center"}, size),
        rotate=page.rotation,
    )


def build_pdf(folder, state):
    """Apply the browser's edit state to the source PDF and return the new PDF bytes."""
    import pymupdf

    if not isinstance(state, dict) or not isinstance(state.get("pages"), list):
        raise ConversionError("These edits could not be read. Reload the page and try again.")
    pages = state["pages"]
    if not pages:
        raise ConversionError("The PDF needs at least one page.")
    if len(pages) > MAX_PAGES * 2:
        raise ConversionError("Too many pages.")

    source = open_pdf(folder / "source.pdf")
    output = pymupdf.open()
    stamp_cache = {}
    try:
        for entry in pages:
            src = entry.get("src")
            if isinstance(src, int) and 0 <= src < source.page_count:
                output.insert_pdf(source, from_page=src, to_page=src, annots=True, links=True)
            else:
                output.new_page(
                    width=_number(entry.get("w"), 72, 14400, 595),
                    height=_number(entry.get("h"), 72, 14400, 842),
                )
            page = output[-1]
            objects = [item for item in entry.get("objects", []) if isinstance(item, dict)]
            _apply_objects(page, objects)
            extra = int(_number(entry.get("rotate"), -1080, 1080, 0)) // 90 * 90
            if extra % 360:
                page.set_rotation((page.rotation + extra) % 360)

        watermark = state.get("watermark")
        numbers = state.get("pageNumbers")
        total = output.page_count
        for index, page in enumerate(output):
            if isinstance(watermark, dict) and watermark.get("enabled"):
                _watermark(page, watermark, stamp_cache)
            if isinstance(numbers, dict) and numbers.get("enabled"):
                _page_number(page, numbers, index + 1, total)

        metadata = source.metadata or {}
        metadata["producer"] = "Network Files PDF editor"
        output.set_metadata({key: value for key, value in metadata.items() if key != "encryption" and value is not None})
        return output.tobytes(garbage=3, deflate=True)
    except ConversionError:
        raise
    except Exception as error:
        raise ConversionError(f"Could not build the edited PDF ({error}).") from error
    finally:
        source.close()
        output.close()
        if "stamp" in stamp_cache:
            stamp_cache["stamp"].close()
