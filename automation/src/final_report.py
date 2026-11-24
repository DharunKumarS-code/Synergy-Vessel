"""Per-week "Final Official vs AI Location Report" in two independent formats (Excel / JSON).

Authoritative source: the existing ``<Week NN>/Final Official vs AI Location Report.xlsx``. It is
the only place that holds the AI Suggested Location values for every week, so it is read
read-only by both commands (rows + embedded photos, see ``load_report``):

* ``generate_excel`` -- never touches an existing report unless ``overwrite`` is set, in which
  case the current file is backed up first and the report is rebuilt atomically from its parsed
  rows and the original photo bytes. Without ``overwrite`` an existing report is only verified.
  Never writes JSON.
* ``generate_json`` -- writes ``Final Official vs AI Location Report.json`` next to it, one record
  per report row in report order. An existing JSON file with identical content is left alone; a
  different one is replaced only with ``overwrite`` (after a backup). Never writes Excel.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.utils.units import pixels_to_EMU
from PIL import Image as PILImage

from . import report_builder

REPORT_STEM = "Final Official vs AI Location Report"
EXCEL_NAME = REPORT_STEM + ".xlsx"
JSON_NAME = REPORT_STEM + ".json"
SHEET_NAME = "Image Register"
PHOTO_HEADER = "Extracted Photograph"
REQUIRED_HEADERS = ("Serial Number", "Image Filename", "Official Location", "AI Suggested Location")
JSON_SCHEMA_VERSION = 1

# Per-week outcome statuses (CLI summary / exit code).
WRITTEN = "written"          # new file created
REPLACED = "replaced"        # existing file backed up, then replaced (overwrite)
UNCHANGED = "unchanged"      # existing JSON already identical -- not rewritten
VERIFIED = "verified"        # existing Excel report read and validated -- not rewritten
CONFLICT = "conflict"        # existing JSON differs; kept as-is (needs overwrite)
MISSING = "missing"          # no source report in this week folder
INVALID = "invalid"          # source report could not be read / failed validation
OK_STATUSES = {WRITTEN, REPLACED, UNCHANGED, VERIFIED}


class ReportSourceError(RuntimeError):
    """The week's source report is unreadable or not a valid report."""


@dataclass
class Photo:
    data: bytes
    fmt: str
    width_px: int
    height_px: int

    def describe(self) -> dict:
        return {"embedded": True, "format": self.fmt, "sha256": hashlib.sha256(self.data).hexdigest(),
                "size_bytes": len(self.data), "width_px": self.width_px, "height_px": self.height_px}


@dataclass
class ReportData:
    week: str
    path: Path
    source_sha256: str
    sheet_name: str
    headers: list[str]
    rows: list[list] = field(default_factory=list)      # cell values, report order
    photos: list[Photo | None] = field(default_factory=list)  # one per row

    @property
    def keys(self) -> list[str]:
        return json_keys(self.headers)


def json_keys(headers: list[str]) -> list[str]:
    """"Serial Number" -> "serial_number"; unique, never empty."""
    keys, seen = [], set()
    for i, h in enumerate(headers, start=1):
        key = re.sub(r"[^0-9a-z]+", "_", h.lower()).strip("_") or f"column_{i}"
        base, n = key, 2
        while key in seen:
            key, n = f"{base}_{n}", n + 1
        seen.add(key)
        keys.append(key)
    return keys


def json_value(value):
    """Cell value -> JSON value. Empty cell or empty string -> null; text kept verbatim."""
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_report(week_name: str, path: Path) -> ReportData:
    """Read a week's report read-only and validate it. Raises ReportSourceError."""
    try:
        source_sha256 = _sha256_file(path)
        wb = load_workbook(path)  # not read_only: embedded images are needed
    except Exception as exc:  # noqa: BLE001 -- any unreadable file is one clear error
        raise ReportSourceError(f"{path.name}: could not be read ({type(exc).__name__}: {exc})") from exc
    try:
        ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.worksheets[0]
        header_cells = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        while header_cells and header_cells[-1] in (None, ""):
            header_cells = header_cells[:-1]
        headers = [str(h).strip() if h is not None else "" for h in header_cells]
        if not headers or any(not h for h in headers):
            raise ReportSourceError(f"{path.name}: header row is empty or has a blank column")
        if len(set(headers)) != len(headers):
            raise ReportSourceError(f"{path.name}: duplicate column headers {headers}")
        missing = [h for h in REQUIRED_HEADERS if h not in headers]
        if missing:
            raise ReportSourceError(f"{path.name}: missing required column(s) {missing}")

        images_by_row: dict[int, list] = {}
        for im in ws._images:
            images_by_row.setdefault(im.anchor._from.row + 1, []).append(im)

        data = ReportData(week=week_name, path=path, source_sha256=source_sha256,
                          sheet_name=ws.title, headers=headers)
        width = len(headers)
        used_rows = set()
        for r, values in enumerate(ws.iter_rows(min_row=2, max_col=width, values_only=True), start=2):
            values = list(values) + [None] * (width - len(values))
            images = images_by_row.get(r, [])
            if all(v in (None, "") for v in values) and not images:
                continue  # formatted-but-empty row
            if len(images) > 1:
                raise ReportSourceError(f"{path.name}: row {r} has {len(images)} photos (expected 1)")
            data.rows.append(values)
            data.photos.append(_photo(images[0]) if images else None)
            used_rows.add(r)
        stray = sorted(set(images_by_row) - used_rows)
        if stray:
            raise ReportSourceError(f"{path.name}: photo(s) anchored outside the data rows at row(s) {stray}")

        serial_idx = headers.index("Serial Number")
        serials = [row[serial_idx] for row in data.rows]
        if any(not isinstance(s, int) or isinstance(s, bool) for s in serials):
            raise ReportSourceError(f"{path.name}: Serial Number must be a whole number on every row")
        if len(set(serials)) != len(serials):
            raise ReportSourceError(f"{path.name}: duplicate Serial Number values")
        return data
    finally:
        wb.close()


def _photo(im) -> Photo:
    raw = im._data()
    with PILImage.open(io.BytesIO(raw)) as pil:
        w, h = pil.size
        fmt = (pil.format or im.format or "").lower()
    return Photo(data=raw, fmt=fmt, width_px=w, height_px=h)


def build_json_document(data: ReportData) -> dict:
    keys = data.keys
    photo_key = keys[data.headers.index(PHOTO_HEADER)] if PHOTO_HEADER in data.headers else None
    records = []
    for values, photo in zip(data.rows, data.photos):
        rec = {k: json_value(v) for k, v in zip(keys, values)}
        if photo_key is not None:
            rec[photo_key] = photo.describe() if photo else None
        records.append(rec)
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "report_title": REPORT_STEM,
        "week": data.week,
        "source_workbook": data.path.name,
        "source_sha256": data.source_sha256,
        "sheet_name": data.sheet_name,
        "columns": [{"key": k, "header": h} for k, h in zip(keys, data.headers)],
        "record_count": len(records),
        "records": records,
    }


def validate_json_document(doc: dict) -> None:
    """Structural checks on the document that is about to be saved. Raises ValueError."""
    keys = [c["key"] for c in doc["columns"]]
    if doc["record_count"] != len(doc["records"]):
        raise ValueError("record_count does not match the number of records")
    for i, rec in enumerate(doc["records"], start=1):
        if list(rec) != keys:
            raise ValueError(f"record {i} fields do not match the columns")
    serials = [rec["serial_number"] for rec in doc["records"]]
    if len(set(serials)) != len(serials):
        raise ValueError("duplicate serial_number values")


def serialize_json(doc: dict) -> bytes:
    validate_json_document(doc)
    text = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if json.loads(text) != doc:  # round-trip: what is saved parses back to exactly the data
        raise ValueError("JSON round-trip mismatch")
    return text.encode("utf-8")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _backup(path: Path, backup_root: Path, week_name: str) -> Path:
    dest_dir = backup_root / week_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    shutil.copy2(path, dest)
    if _sha256_file(dest) != _sha256_file(path):
        raise OSError(f"backup of {path.name} could not be verified")
    return dest


def build_excel(data: ReportData, output_path: Path) -> None:
    """Rebuild the report workbook from parsed rows + the original photo bytes (atomic save)."""
    wb = Workbook()
    ws = wb.active
    ws.title = data.sheet_name
    for col_letter, width in report_builder.COLUMN_WIDTHS.items():
        ws.column_dimensions[col_letter].width = width
    for c_idx, header in enumerate(data.headers, start=1):
        c = ws.cell(1, c_idx, header)
        c.font, c.fill = report_builder.HEADER_FONT, report_builder.HEADER_FILL
        c.alignment, c.border = report_builder.HEADER_ALIGN, report_builder.DATA_BORDER
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = "A2"
    photo_col = data.headers.index(PHOTO_HEADER) + 1 if PHOTO_HEADER in data.headers else None
    for offset, (values, photo) in enumerate(zip(data.rows, data.photos)):
        r = offset + 2
        for c_idx, value in enumerate(values, start=1):
            c = ws.cell(r, c_idx, value)
            c.font, c.alignment, c.border = (report_builder.DATA_FONT, report_builder.DATA_ALIGN,
                                             report_builder.DATA_BORDER)
        ws.row_dimensions[r].height = report_builder.ROW_HEIGHT_POINTS
        if photo is not None and photo_col is not None:
            w, h = report_builder._scaled_size(photo.data, 420, 320)
            img = XLImage(io.BytesIO(photo.data))
            img.width, img.height = w, h
            img.anchor = OneCellAnchor(
                _from=AnchorMarker(col=photo_col - 1, colOff=0, row=r - 1, rowOff=0),
                ext=XDRPositiveSize2D(cx=pixels_to_EMU(w), cy=pixels_to_EMU(h)))
            ws.add_image(img)
    buf = io.BytesIO()
    wb.save(buf)
    _atomic_write_bytes(output_path, buf.getvalue())


def generate_excel(week_name: str, week_dir: Path, overwrite: bool, backup_root: Path) -> dict:
    """Excel command, one week. Never reads or writes the JSON file."""
    path = week_dir / EXCEL_NAME
    if not path.is_file():
        return {"week": week_name, "status": MISSING, "path": path,
                "detail": f"no {EXCEL_NAME} in this week folder (the authoritative source)"}
    try:
        data = load_report(week_name, path)
    except ReportSourceError as exc:
        return {"week": week_name, "status": INVALID, "path": path, "detail": str(exc)}
    n, n_photos = len(data.rows), sum(p is not None for p in data.photos)
    if not overwrite:
        return {"week": week_name, "status": VERIFIED, "path": path, "records": n,
                "detail": f"{n} rows, {n_photos} photos; existing report kept unchanged"}
    backup = _backup(path, backup_root, week_name)
    build_excel(data, path)
    rebuilt = load_report(week_name, path)
    if rebuilt.rows != data.rows or [p and p.data for p in rebuilt.photos] != [p and p.data for p in data.photos]:
        shutil.copy2(backup, path)  # restore the original
        return {"week": week_name, "status": INVALID, "path": path,
                "detail": "rebuilt report did not match its source; original restored"}
    return {"week": week_name, "status": REPLACED, "path": path, "records": n,
            "detail": f"{n} rows, {n_photos} photos; previous file backed up to {backup}"}


def generate_json(week_name: str, week_dir: Path, overwrite: bool, backup_root: Path) -> dict:
    """JSON command, one week. Reads the Excel report read-only; never writes it."""
    source = week_dir / EXCEL_NAME
    path = week_dir / JSON_NAME
    if not source.is_file():
        return {"week": week_name, "status": MISSING, "path": path,
                "detail": f"no {EXCEL_NAME} in this week folder (the authoritative source)"}
    try:
        data = load_report(week_name, source)
        payload = serialize_json(build_json_document(data))
    except (ReportSourceError, ValueError) as exc:
        return {"week": week_name, "status": INVALID, "path": path, "detail": str(exc)}
    n = len(data.rows)
    if path.exists():
        if path.is_file() and path.read_bytes() == payload:
            return {"week": week_name, "status": UNCHANGED, "path": path, "records": n,
                    "detail": f"{n} records; existing JSON already up to date"}
        if not overwrite:
            return {"week": week_name, "status": CONFLICT, "path": path, "records": n,
                    "detail": "existing JSON differs from the report; kept unchanged "
                              "(rerun with --overwrite to back it up and replace it)"}
        if not path.is_file():
            return {"week": week_name, "status": INVALID, "path": path,
                    "detail": f"{JSON_NAME} exists but is not a file"}
        backup = _backup(path, backup_root, week_name)
        _atomic_write_bytes(path, payload)
        return {"week": week_name, "status": REPLACED, "path": path, "records": n,
                "detail": f"{n} records; previous file backed up to {backup}"}
    _atomic_write_bytes(path, payload)
    return {"week": week_name, "status": WRITTEN, "path": path, "records": n,
            "detail": f"{n} records"}
