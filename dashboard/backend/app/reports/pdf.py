"""Minimal, dependency-free PDF writer for the inspection report.

Only what the report needs: A4 pages, the standard Helvetica fonts (no embedding), filled
rectangles and lines, word-wrapped text, and JPEG photographs embedded as-is (DCTDecode).
Text is encoded as cp1252 (WinAnsi); characters outside it are replaced. Identical images are
embedded once and reused.
"""

from __future__ import annotations

import hashlib
import io
import zlib

PAGE_W, PAGE_H = 595.28, 841.89  # A4 in points

# Helvetica advance widths (1/1000 em) for ASCII 32..126, from the standard AFM.
_HELV = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]
FONTS = {"regular": "F1", "bold": "F2"}


def text_width(text: str, size: float, bold: bool = False) -> float:
    w = sum(_HELV[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in text)
    return w * size / 1000 * (1.06 if bold else 1.0)


def wrap(text: str, width: float, size: float, bold: bool = False) -> list[str]:
    """Greedy word wrap; words longer than the width are hard-split."""
    lines: list[str] = []
    for para in (text or "").split("\n"):
        line = ""
        for word in para.split():
            candidate = f"{line} {word}" if line else word
            if text_width(candidate, size, bold) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            while text_width(word, size, bold) > width:
                cut = len(word)
                while cut > 1 and text_width(word[:cut], size, bold) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines or [""]


def _esc(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\r", b"").replace(b"\n", b" ")


def _rgb(color: tuple[float, float, float]) -> str:
    return " ".join(f"{c:.3f}" for c in color)


def prepare_jpeg(data: bytes) -> tuple[bytes, int, int, str]:
    """Return (jpeg_bytes, width, height, colorspace). RGB/grey JPEGs pass through untouched;
    anything else (PNG, CMYK, palette) is converted in memory. Source files are never written."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        if im.format == "JPEG" and im.mode in ("RGB", "L"):
            return data, im.width, im.height, "DeviceRGB" if im.mode == "RGB" else "DeviceGray"
        rgb = im.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=90)
        return buf.getvalue(), rgb.width, rgb.height, "DeviceRGB"


class PdfDocument:
    def __init__(self, title: str = ""):
        self.title = title
        self.pages: list[list[bytes]] = []
        self.images: dict[str, tuple[str, bytes, int, int, str]] = {}  # sha -> (name, data, w, h, cs)
        self.page_images: list[set[str]] = []

    # ------------------------------------------------------------------ drawing
    def add_page(self) -> None:
        self.pages.append([])
        self.page_images.append(set())

    def _op(self, op: str | bytes) -> None:
        self.pages[-1].append(op.encode("latin-1") if isinstance(op, str) else op)

    def text(self, x: float, y: float, text: str, size: float = 10, bold: bool = False,
             color: tuple[float, float, float] = (0.1, 0.12, 0.16)) -> None:
        """y is measured from the TOP of the page (baseline position)."""
        self._op(f"BT /{FONTS['bold' if bold else 'regular']} {size:.2f} Tf {_rgb(color)} rg "
                 f"{x:.2f} {PAGE_H - y:.2f} Td (".encode("latin-1") + _esc(text) + b") Tj ET")

    def rect(self, x: float, y: float, w: float, h: float, fill: tuple[float, float, float] | None = None,
             stroke: tuple[float, float, float] | None = None, line_width: float = 0.5) -> None:
        ops = []
        if fill:
            ops.append(f"{_rgb(fill)} rg")
        if stroke:
            ops.append(f"{_rgb(stroke)} RG {line_width:.2f} w")
        paint = "B" if fill and stroke else ("f" if fill else "S")
        ops.append(f"{x:.2f} {PAGE_H - y - h:.2f} {w:.2f} {h:.2f} re {paint}")
        self._op("q " + " ".join(ops) + " Q")

    def line(self, x1: float, y1: float, x2: float, y2: float, color=(0.8, 0.83, 0.87), width: float = 0.5) -> None:
        self._op(f"q {_rgb(color)} RG {width:.2f} w {x1:.2f} {PAGE_H - y1:.2f} m {x2:.2f} {PAGE_H - y2:.2f} l S Q")

    def image(self, data: bytes, x: float, y: float, max_w: float, max_h: float) -> tuple[float, float]:
        """Draw an image fitted (aspect preserved) into the box; returns the drawn size."""
        key = hashlib.sha256(data).hexdigest()
        if key not in self.images:
            jpeg, w, h, cs = prepare_jpeg(data)
            self.images[key] = (f"Im{len(self.images) + 1}", jpeg, w, h, cs)
        name, _, w, h, _ = self.images[key]
        scale = min(max_w / w, max_h / h)
        dw, dh = w * scale, h * scale
        self.page_images[-1].add(key)
        self._op(f"q {dw:.2f} 0 0 {dh:.2f} {x:.2f} {PAGE_H - y - dh:.2f} cm /{name} Do Q")
        return dw, dh

    # ------------------------------------------------------------------ output
    def build(self) -> bytes:
        objs: list[bytes] = []

        def add(body: bytes) -> int:
            objs.append(body)
            return len(objs)

        catalog = add(b"")  # placeholder, filled below
        pages_id = add(b"")
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        image_ids: dict[str, int] = {}
        for key, (name, jpeg, w, h, cs) in self.images.items():
            head = (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /{cs} "
                    f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n").encode()
            image_ids[key] = add(head + jpeg + b"\nendstream")
        page_ids = []
        for ops, used in zip(self.pages, self.page_images):
            content = zlib.compress(b"\n".join(ops))
            cid = add(f"<< /Length {len(content)} /Filter /FlateDecode >>\nstream\n".encode() + content + b"\nendstream")
            xobj = " ".join(f"/{self.images[k][0]} {image_ids[k]} 0 R" for k in sorted(used))
            res = f"<< /Font << /F1 {f1} 0 R /F2 {f2} 0 R >>" + (f" /XObject << {xobj} >>" if xobj else "") + " >>"
            page_ids.append(add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Resources {res} /Contents {cid} 0 R >>".encode()
            ))
        objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
        kids = " ".join(f"{p} 0 R" for p in page_ids)
        objs[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
        info = add(b"<< /Title (" + _esc(self.title) + b") /Producer (Synergy Vessel dashboard) >>")

        out = io.BytesIO()
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, start=1):
            offsets.append(out.tell())
            out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
        xref = out.tell()
        out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
        for off in offsets:
            out.write(f"{off:010d} 00000 n \n".encode())
        out.write(f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
                  f"startxref\n{xref}\n%%EOF\n".encode())
        return out.getvalue()
