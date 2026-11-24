"""Parse an uploaded workbook entirely in memory. Nothing is written here.

Two formats are accepted (Phase 8.5):

* **Image Register** (the weekly ``Week_NN_Image_Register.xlsx`` files): a worksheet named ``Image Register``
  (or any worksheet whose header row has the register columns); a header row, found within the first rows, with
  the columns below (common spelling variants accepted); one photograph embedded per data row in the
  ``Extracted Photograph`` column.
* **Deck Photos** (the original vessel workbooks): worksheets named by date (``AUG 02 2026``) with a banner in A1
  (``DECK PHOTOS / M.V. <vessel> / Date: 02 AUG 2026``), section headings in wide merged cells and photographs
  anchored in the grid. This mirrors the automation's extraction rules (reading order top-to-bottom then
  left-to-right; a heading applies to photographs strictly below it; headings are merged ranges >= 10 columns).
  When banner and sheet-name dates disagree, the sheet-name date is used and a warning is shown.

Missing information is reported, never invented.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Optional

from ..services.inspection_store import normalise, parse_report_date, parse_worksheet_date, vessel_from

FORMAT_REGISTER, FORMAT_DECK = "image_register", "deck_photos"
HEADING_MIN_MERGE_WIDTH = 10        # same as automation settings.yaml heading_min_merge_width
HEADER_SEARCH_ROWS = 10
_BANNER_DATE_RE = re.compile(r"Date:\s*(\d{1,2})\s+([A-Za-z]+)\.?\s*,?\s*(\d{4})", re.IGNORECASE)
_BANNER_VESSEL_RE = re.compile(r"M\.?\s*V\.?\s+([^\n]+)", re.IGNORECASE)
_HEADER_ALIASES = {
    "Serial Number": ("serial no", "serial", "s no", "sr no", "serial num"),
    "Image Filename": ("image file name", "filename", "file name", "image name", "image id"),
    "Report Date": ("date", "inspection date"),
    "Original Excel Filename": ("original excel file name", "source workbook", "excel filename", "source excel filename",
                                "original workbook"),
    "Worksheet Name": ("worksheet", "sheet name", "sheet"),
    "Official Location": ("location",),
    "Extracted Photograph": ("photograph", "photo", "picture", "image"),
    "Activity Identified": ("activity",),
    "Location Confidence": ("confidence",),
}

SHEET_NAME = "Image Register"
REQUIRED_COLUMNS = ("Serial Number", "Image Filename", "Report Date", "Original Excel Filename",
                    "Worksheet Name", "Official Location", "Extracted Photograph")
OPTIONAL_COLUMNS = ("Activity Identified", "Location Confidence")
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
MAX_ROWS = 5000
_WEEK_RE = re.compile(r"^W(\d{1,3})-\d+", re.IGNORECASE)
_EXT = {"jpeg": "jpg", "jpg": "jpg", "png": "png"}


class WorkbookRejected(Exception):
    """The whole file is unusable; nothing can be imported from it."""

    def __init__(self, code: str, message: str, guidance: str):
        super().__init__(message)
        self.code, self.message, self.guidance = code, message, guidance


@dataclass
class ParsedRow:
    row_number: int                      # worksheet row (1-based, header is row 1)
    serial_number: Optional[int] = None
    image_filename: Optional[str] = None
    report_date: Optional[str] = None
    original_excel_filename: Optional[str] = None
    worksheet_name: Optional[str] = None
    official_location: Optional[str] = None
    activity_identified: Optional[str] = None
    location_confidence: Optional[str] = None
    inspection_date: Optional[dt.date] = None
    date_source: Optional[str] = None
    week_number: Optional[int] = None
    vessel_id: str = "unknown"
    vessel_name: str = "Unknown vessel"
    sha256: Optional[str] = None
    image_format: Optional[str] = None
    image_size: Optional[int] = None
    width_px: Optional[int] = None
    height_px: Optional[int] = None
    source_cell: Optional[str] = None   # Deck Photos: worksheet cell the photograph is anchored at
    location_intel: Optional[dict] = None  # Phase 9: location standardisation (official_location stays the Excel text)
    status: str = "ready"               # ready | duplicate | skipped | invalid
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ParsedWorkbook:
    filename: str
    sha256: str
    size_bytes: int
    rows: list[ParsedRow]
    photos: dict[str, bytes]             # sha256 -> bytes (one copy per unique photograph)
    warnings: list[str] = field(default_factory=list)
    source_format: str = FORMAT_REGISTER
    location_summary: Optional[dict] = None  # Phase 9


def _text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = " ".join(str(v).split())
    return s or None


def _date_text(v: Any) -> Optional[str]:
    """Report Date exactly as the registers write it (m/d/yyyy); real Excel dates are rendered the same way."""
    if isinstance(v, dt.datetime):
        v = v.date()
    if isinstance(v, dt.date):
        return f"{v.month}/{v.day}/{v.year}"
    return _text(v)


def safe_filename(name: Optional[str]) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    base = re.sub(r"[^A-Za-z0-9 ._()-]", "_", base).strip(" .")
    return (base or "upload.xlsx")[:120]


def _damaged_media(data: bytes) -> int:
    """openpyxl silently drops pictures it cannot decode; count them so rows are not misreported."""
    from PIL import Image

    bad = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.startswith("xl/media/"):
                try:
                    with Image.open(io.BytesIO(z.read(name))) as im:
                        im.verify()
                except Exception:  # noqa: BLE001
                    bad += 1
    return bad


def parse_workbook(data: bytes, filename: str) -> ParsedWorkbook:
    filename = safe_filename(filename)
    if not filename.lower().endswith(".xlsx"):
        raise WorkbookRejected("invalid_file_type", "Only Excel workbooks (.xlsx) can be imported.",
                               "Save the register as an Excel Workbook (.xlsx) and upload it again.")
    if not data:
        raise WorkbookRejected("empty_file", "The uploaded file is empty.", "Choose the register workbook again.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise WorkbookRejected("file_too_large", f"The workbook is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                               "Split the register into smaller workbooks and import them one at a time.")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise WorkbookRejected("malformed_workbook", "The file is not a valid Excel workbook.",
                               "Open the file in Excel, re-save it as .xlsx and try again.")
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception:  # noqa: BLE001 - any parser failure means the workbook is unusable
        raise WorkbookRejected("malformed_workbook", "The workbook could not be read; it may be damaged.",
                               "Open the file in Excel, re-save it as .xlsx and try again.") from None

    damaged = _damaged_media(data)
    sheet, header_idx, col = _find_register(wb)
    if sheet is None:
        decks = [n for n in wb.sheetnames if _is_deck_sheet(wb[n], n)]
        if decks:
            return _parse_deck(wb, decks, data, filename, damaged)
        raise WorkbookRejected("missing_worksheet",
                               "The workbook is neither an Image Register nor a Deck Photos workbook.",
                               f"Found worksheet(s): {', '.join(wb.sheetnames) or 'none'}. Import an Image Register "
                               "(an 'Image Register' sheet with its header row) or an original Deck Photos workbook.")
    ws = wb[sheet]
    rows = list(ws.iter_rows(min_row=1, values_only=True))
    if not rows:
        raise WorkbookRejected("no_header", f"The '{sheet}' worksheet is empty.", "Import a register that has a header row.")
    photo_col = col["Extracted Photograph"]

    # Embedded photographs, keyed by worksheet row (openpyxl anchors are 0-based).
    by_row: dict[int, list[Any]] = {}
    stray = 0
    for img in getattr(ws, "_images", []):
        anchor = getattr(getattr(img, "anchor", None), "_from", None)
        if anchor is None or anchor.col != photo_col or anchor.row <= header_idx:
            stray += 1
            continue
        by_row.setdefault(anchor.row + 1, []).append(img)
    # Formatting can extend a sheet far below its data: ignore trailing rows with no values and no photograph.
    while len(rows) > header_idx + 1 and all(_text(v) is None for v in rows[-1]) and len(rows) not in by_row:
        rows.pop()
    if len(rows) - 1 - header_idx > MAX_ROWS:
        raise WorkbookRejected("too_many_rows", f"The register has more than {MAX_ROWS} rows.",
                               "Split the register into smaller workbooks.")

    out = ParsedWorkbook(filename=filename, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), rows=[], photos={})
    if normalise(sheet) != normalise(SHEET_NAME):
        out.warnings.append(f"No worksheet is named '{SHEET_NAME}'; the register was read from worksheet '{sheet}'.")
    if header_idx:
        out.warnings.append(f"The header row is row {header_idx + 1}; the {header_idx} row(s) above it were ignored.")
    if damaged:
        out.warnings.append(f"{damaged} embedded picture(s) are damaged and could not be read.")
    if stray:
        out.warnings.append(f"{stray} embedded picture(s) outside the 'Extracted Photograph' column were ignored.")
    data_rows = 0
    for idx, values in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        get = lambda c: values[col[c]] if col[c] is not None and col[c] < len(values) else None  # noqa: E731
        r = ParsedRow(row_number=idx)
        cells = [get(c) for c in REQUIRED_COLUMNS if c != "Extracted Photograph"] + [get(c) for c in OPTIONAL_COLUMNS]
        if all(_text(v) is None for v in cells) and idx not in by_row:
            r.status = "skipped"
            r.notes.append("Blank row.")
            out.rows.append(r)
            continue
        data_rows += 1
        serial = get("Serial Number")
        if isinstance(serial, float) and serial.is_integer():
            serial = int(serial)
        if isinstance(serial, str) and serial.strip().isdigit():
            serial = int(serial.strip())
        if isinstance(serial, int) and not isinstance(serial, bool) and serial > 0:
            r.serial_number = serial
        else:
            r.errors.append("Serial Number is missing or not a positive whole number.")
        r.image_filename = _text(get("Image Filename"))
        if not r.image_filename:
            r.errors.append("Image Filename is missing.")
        r.report_date = _date_text(get("Report Date"))
        r.original_excel_filename = _text(get("Original Excel Filename"))
        r.worksheet_name = _text(get("Worksheet Name"))
        r.official_location = _text(get("Official Location"))
        r.activity_identified = _text(get("Activity Identified"))
        r.location_confidence = _text(get("Location Confidence"))

        d_report = parse_report_date(get("Report Date"))
        d_ws = parse_worksheet_date(r.worksheet_name)
        if d_report:
            r.inspection_date, r.date_source = d_report, "report_date"
            if d_ws and d_ws != d_report:
                r.warnings.append(f"Report Date {d_report} disagrees with the worksheet-name date {d_ws}.")
        elif d_ws:
            r.inspection_date, r.date_source = d_ws, "worksheet_name"
            r.warnings.append(f"Report Date '{r.report_date or ''}' is not a date; the worksheet-name date {d_ws} was used.")
        else:
            r.errors.append(f"No valid inspection date (Report Date '{r.report_date or ''}', worksheet '{r.worksheet_name or ''}').")

        r.vessel_id, r.vessel_name = vessel_from(r.original_excel_filename)
        if r.vessel_name == "Unknown vessel":
            r.warnings.append("Vessel name not found in Original Excel Filename; recorded as 'Unknown vessel'.")
        if not r.official_location:
            r.notes.append("No Official Location (recorded as not labelled).")
        m = _WEEK_RE.match(r.image_filename or "")
        r.week_number = int(m.group(1)) if m else None

        imgs = by_row.get(idx, [])
        if not imgs:
            r.errors.append("No readable photograph is embedded in this row (missing image"
                            + (", or its picture is damaged)." if damaged else ")."))
        elif len(imgs) > 1:
            r.errors.append(f"{len(imgs)} photographs are embedded in this row; expected exactly one.")
        else:
            _read_photo(r, imgs[0], out)
        if r.errors:
            r.status = "invalid"
        out.rows.append(r)
    if data_rows == 0:
        raise WorkbookRejected("no_records", "The register contains no inspection records.",
                               f"Check that the data rows are below the header row on the '{sheet}' sheet.")
    return out


# ---------------------------------------------------------------------------- shared helpers (Phase 8.5)
def _read_photo(r: ParsedRow, img: Any, out: ParsedWorkbook) -> None:
    try:
        blob = img._data()
        from PIL import Image

        with Image.open(io.BytesIO(blob)) as im:
            im.verify()
        with Image.open(io.BytesIO(blob)) as im:
            r.width_px, r.height_px = im.size
            fmt = (im.format or "").lower()
        if fmt not in _EXT:
            raise ValueError(fmt)
        r.image_format, r.image_size = _EXT[fmt], len(blob)
        r.sha256 = hashlib.sha256(blob).hexdigest()
        out.photos.setdefault(r.sha256, blob)
    except Exception:  # noqa: BLE001
        r.errors.append("The embedded photograph is damaged or not a JPEG/PNG image.")


def _header_key(value: Any) -> str:
    """Header comparison ignoring case, spacing and punctuation ('Serial No.' == 'serial no')."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(value).casefold()).split()) if value is not None else ""


_HEADER_LOOKUP = {_header_key(alias): canon for canon, aliases in _HEADER_ALIASES.items()
                  for alias in (canon,) + aliases}


def _map_header(values: tuple) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for i, h in enumerate(values):
        canon = _HEADER_LOOKUP.get(_header_key(h)) if _text(h) else None
        if canon:
            found.setdefault(canon, []).append(i)
    return found


def _find_register(wb) -> tuple[Optional[str], int, Any]:
    """(sheet, header row index, {column: index}) of the register, or (None, 0, None) if the workbook has none.
    A sheet named 'Image Register' is always treated as the register; otherwise any sheet whose header row
    (within the first rows) carries at least three register columns including 'Extracted Photograph'."""
    from openpyxl.utils import get_column_letter

    named = next((n for n in wb.sheetnames if normalise(n) == normalise(SHEET_NAME)), None)
    candidates = [named] if named else list(wb.sheetnames)
    for name in candidates:
        ws = wb[name]
        best: Optional[tuple[int, dict]] = None
        for i, values in enumerate(ws.iter_rows(min_row=1, max_row=HEADER_SEARCH_ROWS, values_only=True)):
            found = _map_header(values)
            if best is None or len(found) > len(best[1]):
                best = (i, found)
        if best is None or (not named and (len(best[1]) < 3 or "Extracted Photograph" not in best[1])):
            continue
        idx, found = best
        if not found:
            idx = 0
        dupes = {c: v for c, v in found.items() if len(v) > 1}
        if dupes:
            c, v = next(iter(dupes.items()))
            raise WorkbookRejected("duplicate_columns",
                                   f"The column '{c}' appears more than once (columns {', '.join(get_column_letter(i + 1) for i in v)}).",
                                   "Keep one column per heading so the register can be read unambiguously.")
        missing = [c for c in REQUIRED_COLUMNS if c not in found]
        if missing:
            raise WorkbookRejected("missing_columns", f"Required column(s) missing: {', '.join(missing)}.",
                                   "The header row must contain: " + ", ".join(REQUIRED_COLUMNS) + ".")
        return name, idx, {c: (found[c][0] if c in found else None) for c in REQUIRED_COLUMNS + OPTIONAL_COLUMNS}
    return None, 0, None


def _is_deck_sheet(ws, name: str) -> bool:
    banner = _text(ws.cell(1, 1).value) or ""
    return "deck photos" in banner.casefold() or (parse_worksheet_date(name) is not None and bool(getattr(ws, "_images", [])))


def _banner_date(banner: str) -> Optional[dt.date]:
    m = _BANNER_DATE_RE.search(banner or "")
    return parse_worksheet_date(f"{m.group(2)} {m.group(1)} {m.group(3)}") if m else None


def _sheet_date(name: str, banner: str) -> tuple[Optional[dt.date], Optional[str], list[str], list[str]]:
    """(date, date_source, warnings, errors). Banner and sheet name agree -> confirmed; disagree -> the sheet-name
    date is used with a warning (Phase 8 decision); only one available -> it is used with a warning."""
    d_sheet, d_banner = parse_worksheet_date(name), _banner_date(banner)
    if d_sheet and d_banner:
        if d_sheet == d_banner:
            return d_sheet, "report_date", [], []
        return d_sheet, "worksheet_name", [f"Banner date {d_banner} disagrees with the worksheet-name date {d_sheet}; "
                                           f"the worksheet-name date was used."], []
    if d_sheet:
        return d_sheet, "worksheet_name", [f"The banner has no readable date; the worksheet-name date {d_sheet} was used."], []
    if d_banner:
        return d_banner, "report_date", [f"Worksheet name '{name.strip()}' is not a date; the banner date {d_banner} was used."], []
    return None, None, [], [f"No valid inspection date: neither the worksheet name '{name.strip()}' nor the banner gives one."]


def _parse_deck(wb, sheets: list[str], data: bytes, filename: str, damaged: int) -> ParsedWorkbook:
    from openpyxl.utils import get_column_letter

    out = ParsedWorkbook(filename=filename, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), rows=[], photos={},
                         source_format=FORMAT_DECK)
    ignored = [n for n in wb.sheetnames if n not in sheets]
    if ignored:
        out.warnings.append(f"Worksheet(s) without Deck Photos content were ignored: {', '.join(ignored)}.")
    if damaged:
        out.warnings.append(f"{damaged} embedded picture(s) are damaged and could not be read; they are not listed below.")
    vessel_id, vessel_name = vessel_from(filename)
    total = sum(len(getattr(wb[n], "_images", [])) for n in sheets)
    if total == 0 and damaged:
        raise WorkbookRejected("no_records", f"No photograph can be read: all {damaged} embedded picture(s) are damaged.",
                               "Open the workbook in Excel, check the pictures display, re-save it as .xlsx and try again.")
    if total == 0:
        raise WorkbookRejected("no_records", "The Deck Photos workbook contains no embedded photographs.",
                               "Check that the photographs are embedded in the workbook, not linked from another file.")
    if total > MAX_ROWS:
        raise WorkbookRejected("too_many_rows", f"The workbook has more than {MAX_ROWS} photographs.",
                               "Split the workbook into smaller workbooks.")
    serial, unplaced = 0, 0
    for name in sheets:
        ws = wb[name]
        banner = _text(ws.cell(1, 1).value) or ""
        if vessel_name == "Unknown vessel":
            m = _BANNER_VESSEL_RE.search(str(ws.cell(1, 1).value or ""))
            if m:
                vessel_id, vessel_name = vessel_from(f"{' '.join(m.group(1).split()).title()} Deck Photos")
        d, source, date_warnings, date_errors = _sheet_date(name, banner)
        headings = sorted((rng.min_row, str(ws.cell(rng.min_row, rng.min_col).value).strip())
                          for rng in ws.merged_cells.ranges
                          if rng.min_row != 1 and rng.max_col - rng.min_col + 1 >= HEADING_MIN_MERGE_WIDTH
                          and isinstance(ws.cell(rng.min_row, rng.min_col).value, str)
                          and ws.cell(rng.min_row, rng.min_col).value.strip())
        placed = []
        for img in getattr(ws, "_images", []):
            frm = getattr(getattr(img, "anchor", None), "_from", None)
            if frm is None:
                unplaced += 1
                continue
            placed.append((frm.row + 1, frm.col + 1, img))
        placed.sort(key=lambda t: (t[0], t[1]))
        for row, col_no, img in placed:
            serial += 1
            location = next((t for hr, t in reversed(headings) if hr < row), None)
            if location and normalise(location) == normalise("Not Labelled"):
                location = None
            r = ParsedRow(row_number=serial, serial_number=serial, original_excel_filename=filename,
                          worksheet_name=name.strip(), official_location=location, vessel_id=vessel_id, vessel_name=vessel_name,
                          source_cell=f"{get_column_letter(col_no)}{row}")
            r.inspection_date, r.date_source = d, source
            r.report_date = f"{d.month}/{d.day}/{d.year}" if d else None
            r.warnings += date_warnings
            r.errors += date_errors
            r.notes.append(f"Photograph {serial} read from worksheet '{name.strip()}', cell {r.source_cell}.")
            if not location:
                r.notes.append("No section heading above this photograph (recorded as not labelled).")
            _read_photo(r, img, out)
            stamp = f"{d:%Y%m%d}-" if d else ""
            r.image_filename = f"DP-{stamp}{serial:04d}.{r.image_format or 'jpg'}"
            if r.errors:
                r.status = "invalid"
            out.rows.append(r)
    if vessel_name == "Unknown vessel":
        for r in out.rows:
            r.warnings.append("Vessel name not found in the filename or banner; recorded as 'Unknown vessel'.")
    if unplaced:
        out.warnings.append(f"{unplaced} embedded picture(s) are not anchored to a cell and were ignored.")
    return out
