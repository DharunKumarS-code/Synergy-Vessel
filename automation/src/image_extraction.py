"""Extraction-only workflow: pull every embedded image out of the historical weekly source
workbooks, save each one byte-for-byte, and write a standalone Image_Extraction_Register.xlsx.

Deliberately independent of the AI classification pipeline (run_pipeline.py):
  - imports no provider/vision code, never reads settings for a provider, never loads .env, and
    makes no network calls -- it only reads the source .xlsx files and writes new output files;
  - does NOT drop byte-identical duplicates (unlike extraction.extract_week): every embedded
    image appearance keeps its own register row, and duplicates are only *identified* by SHA-256;
  - reads EVERY worksheet of every workbook (extraction.extract_workbook reads only the first).

Reused unchanged from the existing, validated implementation:
  - discovery.find_week_folders / find_source_workbooks (week folders, "Final ..." reports and
    "~$" lock files excluded, "(N)" part-number ordering);
  - extraction.resolve_report_date (a date is trusted only when the A1 banner and the sheet
    name agree), _find_headings / _heading_for_row (section heading, recorded only as source
    metadata, never as a classification), _image_ext, and WorkbookExtractionError.

Safety: source workbooks are opened read-only by openpyxl (never saved); each one is SHA-256
hashed before and after processing and the result is reported. Output goes only to a new,
empty output directory, and no existing file is ever overwritten (files are created with "xb").
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from src.discovery import WEEK_DIR_RE, find_week_folders
from src.extraction import (
    WorkbookExtractionError,
    _find_headings,
    _heading_for_row,
    _image_ext,
    resolve_report_date,
)

REGISTER_FILENAME = "Image_Extraction_Register.xlsx"
IMAGES_DIRNAME = "Extracted_Images"
DEFAULT_MIN_MERGE_WIDTH = 10

# The 11-column register. Columns 1-7 are populated from source data; 8-11 are left blank for
# the separate, future AI classification stage.
REGISTER_HEADERS = [
    "Image ID", "Week", "Workbook", "Worksheet", "Date", "Serial Number", "Image Filename",
    "Activity", "Location", "Activity Confidence", "Location Confidence",
]
DETAILS_HEADERS = [
    "Image ID", "Extraction Status", "SHA-256", "Duplicate Of", "Duplicate Scope",
    "Byte Size", "Image Format", "Anchor Row", "Anchor Column", "Source Section Heading",
    "Date Status", "Error",
]

STATUS_EXTRACTED = "EXTRACTED"
STATUS_FAILED = "EXTRACTION_FAILED"
DUP_NONE = ""
DUP_SAME_WEEK = "SAME_WEEK"
DUP_CROSS_WEEK = "CROSS_WEEK"

HEADER_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFFFF")
HEADER_FILL = PatternFill(patternType="solid", fgColor="FF305496")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)


class OutputExistsError(RuntimeError):
    """The output directory already contains files -- refuse rather than overwrite anything."""


@dataclass
class ImageRecord:
    image_id: str
    week_number: int
    week_folder: str
    workbook: str
    worksheet: str
    serial_number: int
    anchor_row: int
    anchor_col: int
    source_section_heading: str
    sheet_date: date | None
    sheet_date_note: str
    status: str = STATUS_EXTRACTED
    image_format: str = ""
    image_bytes: bytes | None = field(default=None, repr=False)
    sha256: str = ""
    byte_size: int = 0
    image_filename: str = ""
    error: str = ""
    duplicate_of: str = DUP_NONE
    duplicate_scope: str = DUP_NONE
    date: date | None = None       # set only when the week-level date rule confirms it
    date_status: str = ""


@dataclass
class WorkbookInfo:
    week_number: int
    path: Path
    sha256_before: str
    sha256_after: str = ""
    worksheets: list[str] = field(default_factory=list)
    image_count: int = 0
    error: str = ""

    @property
    def unchanged(self) -> bool:
        return bool(self.sha256_after) and self.sha256_before == self.sha256_after


@dataclass
class ExtractionRun:
    records: list[ImageRecord] = field(default_factory=list)
    workbooks: list[WorkbookInfo] = field(default_factory=list)
    weeks: list[tuple[int, str]] = field(default_factory=list)  # (week number, folder name)
    issues: list[tuple[str, str, str]] = field(default_factory=list)  # (category, where, detail)
    week_dates: dict[int, tuple[date | None, str]] = field(default_factory=dict)

    @property
    def extracted(self) -> list[ImageRecord]:
        return [r for r in self.records if r.status == STATUS_EXTRACTED]

    @property
    def failed(self) -> list[ImageRecord]:
        return [r for r in self.records if r.status == STATUS_FAILED]

    @property
    def unique_hashes(self) -> set[str]:
        return {r.sha256 for r in self.extracted}

    @property
    def duplicate_appearances(self) -> list[ImageRecord]:
        return [r for r in self.extracted if r.duplicate_of]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def image_id_for(week_number: int, serial: int) -> str:
    return f"W{week_number:02d}-{serial:04d}"


def week_dirname(week_number: int) -> str:
    return f"Week_{week_number:02d}"


def _read_workbook(path: Path, week_number: int, folder_name: str, min_merge_width: int,
                   next_serial: int) -> tuple[list[ImageRecord], list[str]]:
    """Read every worksheet of one workbook. Workbook-level failures raise
    WorkbookExtractionError; a single unreadable image becomes an EXTRACTION_FAILED record
    (never silently skipped)."""
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        raise WorkbookExtractionError(path, exc) from exc
    records: list[ImageRecord] = []
    try:
        sheetnames = list(wb.sheetnames)
        for sheet_name in sheetnames:
            ws = wb[sheet_name]
            sheet_date, note = resolve_report_date(ws.cell(1, 1).value, sheet_name)
            headings = _find_headings(ws, min_merge_width)
            positioned = []
            for img in ws._images:
                frm = img.anchor._from
                positioned.append((frm.row + 1, frm.col + 1, img))
            positioned.sort(key=lambda t: (t[0], t[1]))
            for row, col, img in positioned:
                serial = next_serial + len(records)
                rec = ImageRecord(
                    image_id=image_id_for(week_number, serial), week_number=week_number,
                    week_folder=folder_name, workbook=path.name, worksheet=sheet_name,
                    serial_number=serial, anchor_row=row, anchor_col=col,
                    source_section_heading=_heading_for_row(headings, row),
                    sheet_date=sheet_date, sheet_date_note=note,
                )
                try:
                    data = img._data()
                    if not data:
                        raise ValueError("embedded image has no data")
                    rec.image_bytes = data
                    rec.image_format = _image_ext(img)
                    rec.sha256 = hashlib.sha256(data).hexdigest()
                    rec.byte_size = len(data)
                except Exception as exc:
                    rec.status = STATUS_FAILED
                    rec.error = f"{type(exc).__name__}: {exc}"
                records.append(rec)
    except Exception as exc:
        raise WorkbookExtractionError(path, exc) from exc
    finally:
        wb.close()
    return records, sheetnames


def collect(project_root: Path, min_merge_width: int = DEFAULT_MIN_MERGE_WIDTH,
            weeks: set[int] | None = None) -> ExtractionRun:
    """Discover week folders and read every embedded image (in memory; nothing is written).
    Each source workbook is hashed before reading and again afterwards."""
    run = ExtractionRun()
    for wf in find_week_folders(project_root):
        week_number = _week_number(wf.name)
        if weeks is not None and week_number not in weeks:
            continue
        run.weeks.append((week_number, wf.name))
        if not wf.source_workbooks:
            run.issues.append(("NO_SOURCE_WORKBOOKS", wf.name, "No source workbooks found in this week folder."))
        week_records: list[ImageRecord] = []
        workbook_failed = False
        for path in wf.source_workbooks:
            info = WorkbookInfo(week_number=week_number, path=path, sha256_before=sha256_file(path))
            run.workbooks.append(info)
            try:
                recs, sheetnames = _read_workbook(path, week_number, wf.name, min_merge_width,
                                                  next_serial=len(week_records) + 1)
                info.worksheets = sheetnames
                info.image_count = len(recs)
                week_records.extend(recs)
                if not recs:
                    run.issues.append(("NO_IMAGES", f"{wf.name} / {path.name}",
                                       "Workbook contains no embedded images."))
            except WorkbookExtractionError as exc:
                workbook_failed = True
                info.error = str(exc)
                run.issues.append(("WORKBOOK_FAILED", f"{wf.name} / {path.name}", str(exc)))
            info.sha256_after = sha256_file(path)
            if not info.unchanged:
                run.issues.append(("SOURCE_CHANGED", f"{wf.name} / {path.name}",
                                   "Source workbook hash changed during processing."))
        _apply_week_date_rule(run, week_number, wf.name, week_records, workbook_failed)
        run.records.extend(week_records)
    _mark_duplicates(run.records)
    for r in run.failed:
        run.issues.append(("IMAGE_FAILED", f"{r.image_id} ({r.workbook} / {r.worksheet}, row {r.anchor_row})",
                           r.error))
    return run


def _week_number(folder_name: str) -> int:
    return int(WEEK_DIR_RE.match(folder_name.strip()).group(1))


def _apply_week_date_rule(run: ExtractionRun, week_number: int, folder_name: str,
                          records: list[ImageRecord], workbook_failed: bool) -> None:
    """The existing pipeline's rule, extended to every worksheet: a date is used only when every
    worksheet of every source workbook in the week independently confirms the SAME date (banner
    and sheet name agree). Otherwise the Date column stays blank and the reason is recorded."""
    sheet_dates = {}
    for r in records:
        sheet_dates[(r.workbook, r.worksheet)] = (r.sheet_date, r.sheet_date_note)
    distinct = {d for d, _ in sheet_dates.values()}
    if sheet_dates and not workbook_failed and None not in distinct and len(distinct) == 1:
        week_date = next(iter(distinct))
        status = "Confirmed: every worksheet's banner and sheet-name dates agree."
    else:
        week_date = None
        if workbook_failed:
            status = "Not confirmed: a source workbook in this week could not be read."
        elif not sheet_dates:
            status = "Not confirmed: no images/worksheets with images in this week."
        elif len(distinct - {None}) > 1:
            status = ("Not confirmed: worksheets disagree on the date ("
                      + ", ".join(sorted(str(d) for d in distinct - {None})) + ").")
        else:
            status = "Not confirmed: at least one worksheet's banner/sheet-name date is missing or disagrees."
        if records:
            run.issues.append(("DATE_NOT_CONFIRMED", folder_name, status))
            for (wb_name, ws_name), (_, note) in sorted(sheet_dates.items()):
                run.issues.append(("DATE_DETAIL", f"{folder_name} / {wb_name} / {ws_name}", note))
    run.week_dates[week_number] = (week_date, status)
    for r in records:
        r.date = week_date
        r.date_status = status if week_date else f"{status} Worksheet: {r.sheet_date_note}"


def _mark_duplicates(records: list[ImageRecord]) -> None:
    """Identify byte-identical images by SHA-256. The first appearance (week order, then reading
    order) is the original; every later appearance keeps its own row and points at it.

    A repeat of an image already seen earlier in the SAME week is SAME_WEEK and points at that
    week's first appearance (these are exactly the repeats the existing pipeline's per-week
    dedupe drops); otherwise a repeat is CROSS_WEEK and points at the overall first appearance.
    """
    first: dict[str, ImageRecord] = {}
    first_in_week: dict[tuple[int, str], ImageRecord] = {}
    for r in records:
        if r.status != STATUS_EXTRACTED:
            continue
        week_original = first_in_week.get((r.week_number, r.sha256))
        original = first.get(r.sha256)
        if week_original is not None:
            r.duplicate_of, r.duplicate_scope = week_original.image_id, DUP_SAME_WEEK
        elif original is not None:
            r.duplicate_of, r.duplicate_scope = original.image_id, DUP_CROSS_WEEK
        first.setdefault(r.sha256, r)
        first_in_week.setdefault((r.week_number, r.sha256), r)


def ensure_empty_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise OutputExistsError(f"Output path exists and is not a directory: {output_dir}")
        if any(output_dir.iterdir()):
            raise OutputExistsError(
                f"Output directory is not empty: {output_dir}. Choose a new --output-dir; "
                "existing files are never overwritten.")


def write_images(run: ExtractionRun, output_dir: Path) -> None:
    """Write every extracted image's original bytes (no re-encoding) to
    <output_dir>/Extracted_Images/Week_NN/<Image ID>.<ext>, then verify each file's SHA-256."""
    images_root = output_dir / IMAGES_DIRNAME
    for week_number, _ in run.weeks:
        (images_root / week_dirname(week_number)).mkdir(parents=True, exist_ok=True)
    for r in run.extracted:
        filename = f"{r.image_id.replace('-', '_')}.{r.image_format}"
        target = images_root / week_dirname(r.week_number) / filename
        try:
            with target.open("xb") as f:  # never overwrite
                f.write(r.image_bytes)
            if sha256_file(target) != r.sha256:
                raise OSError("written file hash does not match the embedded image")
            r.image_filename = filename
        except Exception as exc:
            r.status = STATUS_FAILED
            r.error = f"Write failed: {type(exc).__name__}: {exc}"
            run.issues.append(("IMAGE_WRITE_FAILED", r.image_id, r.error))
    if any(i[0] == "IMAGE_WRITE_FAILED" for i in run.issues):
        # a failed write changes which appearance is the first *extracted* one -- recompute
        for r in run.records:
            r.duplicate_of = r.duplicate_scope = DUP_NONE
        _mark_duplicates(run.records)


def _style_header(ws, headers: list[str], widths: list[float]) -> None:
    for i, (h, w) in enumerate(zip(headers, widths), start=1):
        c = ws.cell(1, i, h)
        c.font, c.fill, c.alignment = HEADER_FONT, HEADER_FILL, HEADER_ALIGN
        ws.column_dimensions[c.column_letter].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def summary_rows(run: ExtractionRun) -> list[tuple[str, object]]:
    per_week = {wn: 0 for wn, _ in run.weeks}
    for r in run.extracted:
        per_week[r.week_number] += 1
    rows: list[tuple[str, object]] = [
        ("Workbooks discovered", len(run.workbooks)),
        ("Workbooks processed successfully", sum(1 for w in run.workbooks if not w.error)),
        ("Workbooks failed", sum(1 for w in run.workbooks if w.error)),
        ("Weeks processed", len(run.weeks)),
        ("Weeks", ", ".join(str(wn) for wn, _ in run.weeks)),
        ("Embedded images found", len(run.records)),
        ("Images extracted", len(run.extracted)),
        ("Images that could not be extracted", len(run.failed)),
        ("Unique images (SHA-256)", len(run.unique_hashes)),
        ("Duplicate image appearances (total)", len(run.duplicate_appearances)),
        ("  of which within the same week",
         sum(1 for r in run.duplicate_appearances if r.duplicate_scope == DUP_SAME_WEEK)),
        ("  of which across weeks",
         sum(1 for r in run.duplicate_appearances if r.duplicate_scope == DUP_CROSS_WEEK)),
        ("Rows with blank Date (not reliably confirmed)", sum(1 for r in run.records if r.date is None)),
        ("Source workbooks unchanged (SHA-256 before = after)",
         f"{sum(1 for w in run.workbooks if w.unchanged)} of {len(run.workbooks)}"),
        ("AI classification", "Not performed (columns 8-11 intentionally blank)"),
    ]
    for wn, _ in run.weeks:
        rows.append((f"Images in Week {wn}", per_week[wn]))
    return rows


def write_register(run: ExtractionRun, path: Path) -> None:
    """Write the register workbook atomically (temp file + rename); refuse to overwrite."""
    if path.exists():
        raise OutputExistsError(f"Register already exists: {path}")
    wb = Workbook()
    ws = wb.active
    ws.title = "Image Register"
    _style_header(ws, REGISTER_HEADERS, [12, 7, 48, 16, 12, 9, 18, 18, 24, 12, 12])
    for r in run.records:
        ws.append([r.image_id, r.week_number, r.workbook, r.worksheet, r.date, r.serial_number,
                   r.image_filename or None, None, None, None, None])
        if r.date is not None:
            ws.cell(ws.max_row, 5).number_format = "yyyy-mm-dd"
    ws.auto_filter.ref = f"A1:K{max(ws.max_row, 1)}"

    det = wb.create_sheet("Extraction Details")
    _style_header(det, DETAILS_HEADERS, [12, 18, 66, 12, 12, 10, 8, 8, 8, 40, 60, 40])
    for r in run.records:
        det.append([r.image_id, r.status, r.sha256 or None, r.duplicate_of or None,
                    r.duplicate_scope or None, r.byte_size or None, r.image_format or None,
                    r.anchor_row, r.anchor_col, r.source_section_heading, r.date_status,
                    r.error or None])
    det.auto_filter.ref = f"A1:L{max(det.max_row, 1)}"

    sm = wb.create_sheet("Summary")
    _style_header(sm, ["Metric", "Value"], [52, 40])
    for k, v in summary_rows(run):
        sm.append([k, v])

    wbs = wb.create_sheet("Workbooks")
    _style_header(wbs, ["Week", "Workbook", "Worksheets", "Images", "SHA-256 Before",
                        "SHA-256 After", "Unchanged", "Error"], [7, 48, 24, 8, 66, 66, 10, 40])
    for w in run.workbooks:
        wbs.append([w.week_number, w.path.name, ", ".join(w.worksheets), w.image_count,
                    w.sha256_before, w.sha256_after, "YES" if w.unchanged else "NO", w.error or None])

    iss = wb.create_sheet("Issues")
    _style_header(iss, ["Category", "Where", "Detail"], [22, 60, 90])
    for row in run.issues:
        iss.append(list(row))
    if not run.issues:
        iss.append(["NONE", "", "No extraction failures or metadata issues."])

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".register_", suffix=".xlsx", dir=path.parent)
    os.close(fd)
    try:
        wb.save(tmp)
        if path.exists():
            raise OutputExistsError(f"Register already exists: {path}")
        os.rename(tmp, path)  # os.rename never replaces an existing file on Windows
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def run_extraction(project_root: Path, output_dir: Path,
                   min_merge_width: int = DEFAULT_MIN_MERGE_WIDTH,
                   weeks: set[int] | None = None) -> ExtractionRun:
    """Full extraction-only workflow. Refuses a non-empty output directory before reading
    anything, and refuses an output directory inside a week folder (source data)."""
    output_dir = output_dir.resolve()
    project_root = project_root.resolve()
    for wf in find_week_folders(project_root):
        wp = wf.path.resolve()
        if output_dir == wp or wp in output_dir.parents:
            raise OutputExistsError(f"Output directory must not be inside a source week folder: {wf.path}")
    ensure_empty_output_dir(output_dir)
    run = collect(project_root, min_merge_width, weeks)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_images(run, output_dir)
    write_register(run, output_dir / REGISTER_FILENAME)
    return run
