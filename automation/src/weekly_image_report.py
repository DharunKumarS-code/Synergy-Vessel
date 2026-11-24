from __future__ import annotations

import hashlib
import io
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.styles.colors import Color
from openpyxl.utils.units import pixels_to_EMU
from PIL import Image as PILImage

from src import image_extraction as ie

SHEET_TITLE = "Image Register"
NEW_IMAGES_DIRNAME = "Extracted Images - New"
PENDING_DATE_TEXT = "AMBIGUOUS/PENDING"   # reference convention (Week 34)
FAILED_PHOTO_TEXT = "EXTRACTION FAILED"
NO_HEADING = "Not Labelled"               # extraction._heading_for_row's "no heading" value

HEADERS = [
    "Serial Number", "Image Filename", "Report Date", "Original Excel Filename",
    "Worksheet Name", "Official Location", "Extracted Photograph", "Activity Identified",
    "Location Confidence",
]
PHOTO_COL = 7  # G
# Reference widths, with the removed column G ("AI Suggested Location") taken out.
COLUMN_WIDTHS = {"A": 10.0, "B": 20.0, "C": 12.0, "D": 42.0, "E": 14.0, "F": 30.0,
                 "G": 27.44140625, "H": 17.44140625, "I": 20.0}
HEADER_ROW_HEIGHT = 24.0
PHOTO_ROW_HEIGHT = 107.25
PHOTO_WIDTH_PX = 180

HEADER_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFFFF")
HEADER_FILL = PatternFill(patternType="solid", fgColor=Color(theme=1, tint=0.249977111117893))
HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
DATA_FONT = Font(name="Arial", size=10)
DATA_ALIGN = Alignment(vertical="top", wrap_text=True)


class WeeklyReportError(RuntimeError):
    pass


@dataclass
class WeekOutput:
    week_number: int
    week_folder: Path
    report_path: Path
    images_dir: Path
    records: list[ie.ImageRecord] = field(default_factory=list)
    locations: dict[str, str | None] = field(default_factory=dict)  # image_id -> location/None
    renamed: list[str] = field(default_factory=list)  # names that collided and got a suffix


def photo_size(image_bytes: bytes) -> tuple[int, int]:
    """Reference rule: 180 px wide, height in proportion to the original."""
    with PILImage.open(io.BytesIO(image_bytes)) as im:
        w, h = im.size
    return PHOTO_WIDTH_PX, max(1, round(PHOTO_WIDTH_PX * h / w))


def report_date_text(rec: ie.ImageRecord) -> str:
    d = rec.date
    return f"{d.month}/{d.day}/{d.year}" if d else PENDING_DATE_TEXT


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    """folder/stem+suffix, or the first free stem_v2/_v3/... -- an existing file is never reused."""
    candidate, n = folder / f"{stem}{suffix}", 1
    while candidate.exists():
        n += 1
        candidate = folder / f"{stem}_v{n}{suffix}"
    return candidate


def official_locations(records: list[ie.ImageRecord]) -> dict[str, str | None]:
    """image_id -> source section heading, or None (blank) when there is no heading above the
    photo or when the same photo appears in this week under two or more different headings
    (ambiguous). "No heading" is not a heading: a copy with no heading neither conflicts with
    a copy that has one nor inherits it."""
    headings_by_hash: dict[str, set[str]] = defaultdict(set)
    for r in records:
        if r.sha256 and r.source_section_heading and r.source_section_heading != NO_HEADING:
            headings_by_hash[r.sha256].add(r.source_section_heading)
    result = {}
    for r in records:
        heading = r.source_section_heading
        ambiguous = bool(r.sha256) and len(headings_by_hash[r.sha256]) > 1
        result[r.image_id] = None if (heading == NO_HEADING or ambiguous or not heading) else heading
    return result


def write_week_images(out: WeekOutput, run: ie.ExtractionRun) -> None:
    out.images_dir.mkdir(exist_ok=True)
    for rec in out.records:
        if rec.status != ie.STATUS_EXTRACTED:
            continue
        target = unique_path(out.images_dir, rec.image_id, f".{rec.image_format or 'bin'}")
        if target.stem != rec.image_id:
            out.renamed.append(f"{rec.image_id} -> {target.name}")
            run.issues.append(("FILENAME_COLLISION", f"Week {rec.week_number}",
                               f"{rec.image_id}.{rec.image_format} already existed; wrote {target.name}"))
        try:
            with target.open("xb") as f:  # never overwrite
                f.write(rec.image_bytes)
            if ie.sha256_file(target) != rec.sha256:
                raise OSError("written file hash does not match the embedded image")
            rec.image_filename = target.name
        except Exception as exc:
            rec.status = ie.STATUS_FAILED
            rec.error = f"Write failed: {type(exc).__name__}: {exc}"
            run.issues.append(("IMAGE_WRITE_FAILED", rec.image_id, rec.error))


def build_week_report(out: WeekOutput) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE
    for col, width in COLUMN_WIDTHS.items():
        ws.column_dimensions[col].width = width
    for i, h in enumerate(HEADERS, start=1):
        c = ws.cell(1, i, h)
        c.font, c.fill, c.alignment = HEADER_FONT, HEADER_FILL, HEADER_ALIGN
    ws.row_dimensions[1].height = HEADER_ROW_HEIGHT
    ws.freeze_panes = "A2"

    for r, rec in enumerate(out.records, start=2):
        values = [rec.serial_number, rec.image_filename or None, report_date_text(rec),
                  rec.workbook, rec.worksheet, out.locations.get(rec.image_id),
                  None if rec.status == ie.STATUS_EXTRACTED else FAILED_PHOTO_TEXT, None, None]
        for col, v in enumerate(values, start=1):
            if v is None:
                continue  # blank cells stay truly empty (photo cell: the photo sits above it)
            c = ws.cell(r, col, v)
            c.font, c.alignment = DATA_FONT, DATA_ALIGN
        height_pt = PHOTO_ROW_HEIGHT
        if rec.status == ie.STATUS_EXTRACTED:
            w, h = photo_size(rec.image_bytes)
            img = XLImage(io.BytesIO(rec.image_bytes))
            img.width, img.height = w, h
            img.anchor = OneCellAnchor(
                _from=AnchorMarker(col=PHOTO_COL - 1, colOff=0, row=r - 1, rowOff=0),
                ext=XDRPositiveSize2D(cx=pixels_to_EMU(w), cy=pixels_to_EMU(h)))
            ws.add_image(img)
            # keep a taller-than-4:3 photo inside its own row so it never covers the next row
            height_pt = max(PHOTO_ROW_HEIGHT, h * 0.75 + 1.5)
        ws.row_dimensions[r].height = height_pt

    fd, tmp = tempfile.mkstemp(prefix=".register_", suffix=".tmp", dir=out.week_folder)
    os.close(fd)
    try:
        wb.save(tmp)
        if out.report_path.exists():
            raise WeeklyReportError(f"Report already exists: {out.report_path}")
        os.rename(tmp, out.report_path)  # never replaces an existing file
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


WEEK_NEW = "NEW"
WEEK_COMPLETE = "COMPLETE"
WEEK_ATTENTION = "NEEDS_ATTENTION"


@dataclass
class WeekStatus:
    week_number: int
    folder_name: str
    status: str
    reasons: list[str] = field(default_factory=list)


def register_filename(week_number: int) -> str:
    return f"Week_{week_number:02d}_Image_Register.xlsx"


def count_source_images(source_workbooks: list[Path]) -> int:
    """Embedded images across every worksheet of the week's source workbooks (read-only)."""
    import openpyxl
    total = 0
    for path in source_workbooks:
        wb = openpyxl.load_workbook(path, data_only=True)
        try:
            total += sum(len(ws._images) for ws in wb.worksheets)
        finally:
            wb.close()
    return total


def check_completed_week(week_folder: Path, week_number: int, expected: int) -> list[str]:
    """Problems that stop an existing week's output counting as complete (empty list = complete):
    one "Image Register" sheet, the nine headers, `expected` data rows and embedded photos (one
    per row, in the photo column), register filenames == the files in "Extracted Images - New",
    and every file byte-identical (SHA-256) to its row's embedded photo. Read-only."""
    import openpyxl
    reg = week_folder / register_filename(week_number)
    images_dir = week_folder / NEW_IMAGES_DIRNAME
    if not reg.is_file():
        return [f"{reg.name} is missing"]
    try:
        wb = openpyxl.load_workbook(reg)
    except Exception as exc:
        return [f"{reg.name} cannot be opened ({type(exc).__name__}: {exc})"]
    problems = []
    try:
        if wb.sheetnames != [SHEET_TITLE]:
            return [f"{reg.name}: worksheets are {wb.sheetnames}, expected only '{SHEET_TITLE}'"]
        ws = wb[SHEET_TITLE]
        if [c.value for c in ws[1]] != HEADERS or ws.max_column != len(HEADERS):
            problems.append(f"{reg.name}: columns are not the expected nine")
        rows = ws.max_row - 1
        if rows != expected:
            problems.append(f"{reg.name}: {rows} data rows, expected {expected}")
        if len(ws._images) != expected:
            problems.append(f"{reg.name}: {len(ws._images)} embedded photos, expected {expected}")
        anchored = {}
        for im in ws._images:
            anchored.setdefault(im.anchor._from.row + 1, []).append(im)
        names = [ws.cell(r, 2).value for r in range(2, ws.max_row + 1)]
        files = sorted(p.name for p in images_dir.iterdir()) if images_dir.is_dir() else []
        if sorted(n for n in names if n) != files or len(files) != expected:
            problems.append(f"register filenames do not match the {len(files)} file(s) in "
                            f"'{NEW_IMAGES_DIRNAME}' (expected {expected})")
        if problems:
            return problems
        for r, name in enumerate(names, start=2):
            ims = anchored.get(r, [])
            if len(ims) != 1 or ims[0].anchor._from.col != PHOTO_COL - 1:
                problems.append(f"row {r}: expected exactly one photo in the photo column")
            elif hashlib.sha256(ims[0]._data()).hexdigest() != ie.sha256_file(images_dir / name):
                problems.append(f"row {r}: {name} differs from the embedded photo (SHA-256)")
    finally:
        wb.close()
    return problems


def week_status(week_folder: Path, week_number: int, source_workbooks: list[Path]) -> WeekStatus:
    """NEW: no register and no extracted photos yet -> extract. COMPLETE: existing output passes
    check_completed_week -> skip. Anything else -> NEEDS_ATTENTION (skipped, never repaired or
    overwritten)."""
    st = WeekStatus(week_number, week_folder.name, WEEK_NEW)
    reg = week_folder / register_filename(week_number)
    images_dir = week_folder / NEW_IMAGES_DIRNAME
    has_photos = images_dir.is_dir() and any(images_dir.iterdir())
    if not reg.exists() and not has_photos:
        return st
    try:
        expected = count_source_images(source_workbooks)
    except Exception as exc:
        st.status, st.reasons = WEEK_ATTENTION, [f"source workbooks unreadable ({type(exc).__name__}: {exc})"]
        return st
    problems = check_completed_week(week_folder, week_number, expected)
    st.status, st.reasons = (WEEK_ATTENTION, problems) if problems else (WEEK_COMPLETE, [])
    return st


def generate_weekly_reports(project_root: Path, min_merge_width: int = ie.DEFAULT_MIN_MERGE_WIDTH,
                            weeks: set[int] | None = None,
                            ) -> tuple[ie.ExtractionRun, list[WeekOutput], list[WeekStatus]]:
    """Extract only weeks with no output yet (NEW). COMPLETE and NEEDS_ATTENTION weeks are left
    exactly as they are; every selected week's status is returned."""
    project_root = project_root.resolve()
    statuses = [week_status(wf.path, n, wf.source_workbooks)
                for wf in ie.find_week_folders(project_root)
                if (n := ie._week_number(wf.name)) is not None and (weeks is None or n in weeks)]
    eligible = {s.week_number for s in statuses if s.status == WEEK_NEW}
    run = ie.collect(project_root, min_merge_width, eligible)
    folders = {wf.name: wf.path for wf in ie.find_week_folders(project_root)}
    outputs: list[WeekOutput] = []
    for week_number, folder_name in run.weeks:
        week_folder = folders[folder_name]
        records = [r for r in run.records if r.week_number == week_number]
        report_path = unique_path(week_folder, f"Week_{week_number:02d}_Image_Register", ".xlsx")
        out = WeekOutput(week_number, week_folder, report_path,
                         week_folder / NEW_IMAGES_DIRNAME, records, official_locations(records))
        if report_path.stem != f"Week_{week_number:02d}_Image_Register":
            out.renamed.append(f"register -> {report_path.name}")
            run.issues.append(("FILENAME_COLLISION", folder_name,
                               f"Week_{week_number:02d}_Image_Register.xlsx already existed; wrote {report_path.name}"))
        write_week_images(out, run)
        build_week_report(out)
        outputs.append(out)
    return run, outputs, statuses


def verify_week_output(out: WeekOutput) -> list[str]:
    """Re-open a written report and check it against the records. Returns problems (empty = OK)."""
    import openpyxl
    problems = []
    wb = openpyxl.load_workbook(out.report_path)
    try:
        if wb.sheetnames != [SHEET_TITLE]:
            problems.append(f"worksheets are {wb.sheetnames}, expected only '{SHEET_TITLE}'")
        ws = wb[SHEET_TITLE]
        if ws.sheet_state != "visible":
            problems.append("Image Register is not visible")
        if [c.value for c in ws[1]] != HEADERS or ws.max_column != len(HEADERS):
            problems.append(f"header mismatch: {[c.value for c in ws[1]]}")
        if ws.max_row - 1 != len(out.records):
            problems.append(f"row count {ws.max_row - 1} != records {len(out.records)}")
        anchored = {im.anchor._from.row + 1: im for im in ws._images}
        if len(anchored) != len(ws._images):
            problems.append("more than one photo anchored on the same row")
        for r, rec in enumerate(out.records, start=2):
            row = [c.value for c in ws[r]]
            if row[0] != rec.serial_number or row[1] != (rec.image_filename or None):
                problems.append(f"row {r}: serial/filename mismatch")
            if row[5] != out.locations.get(rec.image_id):
                problems.append(f"row {r}: official location mismatch")
            if row[5] is not None and row[5] != rec.source_section_heading:
                problems.append(f"row {r}: location is not the source heading")
            if row[7] is not None or row[8] is not None:
                problems.append(f"row {r}: AI columns not blank")
            im = anchored.get(r)
            if rec.status == ie.STATUS_EXTRACTED:
                if im is None or im.anchor._from.col != PHOTO_COL - 1:
                    problems.append(f"row {r}: photo missing or in wrong column")
                elif hashlib.sha256(im._data()).hexdigest() != rec.sha256:
                    problems.append(f"row {r}: embedded photo bytes differ from source")
                f = out.images_dir / rec.image_filename
                if not f.is_file() or ie.sha256_file(f) != rec.sha256:
                    problems.append(f"row {r}: extracted file missing or different")
            elif im is not None:
                problems.append(f"row {r}: failed extraction but a photo is anchored")
    finally:
        wb.close()
    return problems
