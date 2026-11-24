from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.utils.units import pixels_to_EMU
from PIL import Image as PILImage

HEADERS = [
    "Serial Number", "Image Filename", "Report Date", "Original Excel Filename",
    "Worksheet Name", "Official Location", "AI Suggested Location",
    "Extracted Photograph", "Activity Identified", "Location Confidence",
    "Classification Status",
]

COLUMN_WIDTHS = {
    "A": 10, "B": 20, "C": 12, "D": 42, "E": 14, "F": 30, "G": 30, "H": 27.66, "I": 20, "J": 20,
    "K": 20,
}

HEADER_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFFFF")
HEADER_FILL = PatternFill(patternType="solid", fgColor="FF305496")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
DATA_FONT = Font(name="Arial", size=10)
DATA_ALIGN = Alignment(vertical="top", wrap_text=True)
THIN = Side(style="thin", color="FFCCCCCC")
DATA_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

STATUS_SHEET_TITLE = "Processing Status"  # Phase P1-2: optional second sheet

IMAGE_COLUMN_INDEX = 8  # H, 1-indexed
RESULT_SOURCE_HEADER = "Result Source"  # optional 12th column (--from-registers)
# --from-registers only: optional 13th/14th columns after Result Source (review_columns=True).
REVIEW_HEADERS = ["Activity Confidence", "Remarks"]
ROW_HEIGHT_POINTS = 107.25


class ReportGenerationError(RuntimeError):

    def __init__(self, message: str, original: Exception | None = None,
                 image_filename: str | None = None):
        self.original_exception = original
        self.image_filename = image_filename
        super().__init__(message)


@dataclass
class ReportRow:
    serial_number: int
    image_filename: str
    report_date: str
    original_excel_filename: str
    worksheet_name: str
    official_location: str
    ai_suggested_location: str | None
    image_bytes: bytes
    activity_identified: str
    location_confidence: str | None
    # One of "API_FAILURE", "VISUAL_REVIEW", "CLASSIFIED" -- computed once by
    # run_pipeline._classification_status(), the same single source of truth Phase AA's flagged
    # CSV uses, so this column and the CSV's failure_type column can never disagree about the
    # same image. Never derived independently here.
    classification_status: str = "CLASSIFIED"
    # --from-registers only (written only when build_report(result_source_column=True)): where
    # this row's result came from, e.g. "Same photo as W30-0001 (no AI request)".
    result_source: str = ""
    # --from-registers only (written only when build_report(review_columns=True)): the result's
    # own activity_confidence, and the existing review/failure reason (see run_pipeline._remarks).
    activity_confidence: str | None = None
    remarks: str = ""


def _scaled_size(image_bytes: bytes, max_w: int, max_h: int) -> tuple[int, int]:
    with PILImage.open(io.BytesIO(image_bytes)) as im:
        w, h = im.size
    scale = min(max_w / w, max_h / h, 1.0)
    return max(1, round(w * scale)), max(1, round(h * scale))


def build_report(
    week_name: str,
    rows: list[ReportRow],
    output_path: Path,
    image_max_width_px: int = 420,
    image_max_height_px: int = 320,
    processing_status: list[tuple[str, str]] | None = None,
    result_source_column: bool = False,
    review_columns: bool = False,
) -> None:
    """result_source_column: add a trailing "Result Source" column (L) after the unchanged 11
    columns -- used by run_pipeline --from-registers only; off by default.
    review_columns: then add "Activity Confidence" and "Remarks" after it (register mode only)."""
    headers = HEADERS + [RESULT_SOURCE_HEADER] if result_source_column else list(HEADERS)
    if review_columns:
        headers = headers + REVIEW_HEADERS
    wb = Workbook()
    ws = wb.active
    ws.title = "Image Register"

    for col_letter, width in COLUMN_WIDTHS.items():
        ws.column_dimensions[col_letter].width = width
    if result_source_column:
        ws.column_dimensions["L"].width = 34
    if review_columns:
        first = len(headers) - len(REVIEW_HEADERS) + 1
        ws.column_dimensions[get_column_letter(first)].width = 20
        ws.column_dimensions[get_column_letter(first + 1)].width = 50

    for col_idx, header in enumerate(headers, start=1):
        c = ws.cell(1, col_idx, header)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = HEADER_ALIGN
        c.border = DATA_BORDER
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = "A2"

    for row_offset, row in enumerate(rows):
        r = row_offset + 2
        values = [
            row.serial_number, row.image_filename, row.report_date,
            row.original_excel_filename, row.worksheet_name, row.official_location,
            row.ai_suggested_location, None, row.activity_identified, row.location_confidence,
            row.classification_status,
        ]
        if result_source_column:
            values.append(row.result_source)
        if review_columns:
            values += [row.activity_confidence, row.remarks]
        for col_idx, value in enumerate(values, start=1):
            if col_idx == IMAGE_COLUMN_INDEX:
                continue  # image cell stays empty; the photo floats above it via anchor
            c = ws.cell(r, col_idx, value)
            c.font = DATA_FONT
            c.alignment = DATA_ALIGN
            c.border = DATA_BORDER
        ws.row_dimensions[r].height = ROW_HEIGHT_POINTS

        try:
            # row.image_bytes is never modified here -- only read, to compute display size and
            # wrap in a fresh BytesIO for openpyxl's own embedding. A malformed/unreadable image
            # must never be silently dropped from the report (a ship-inspection document
            # missing a photo with no obvious trace is a safety risk) -- it fails this row
            # loudly instead, which aborts this week's whole report generation (see
            # ReportGenerationError's docstring for why that's the safer choice).
            width_px, height_px = _scaled_size(row.image_bytes, image_max_width_px, image_max_height_px)
            img_stream = io.BytesIO(row.image_bytes)
            xl_img = XLImage(img_stream)
            xl_img.width = width_px
            xl_img.height = height_px
            anchor = OneCellAnchor(
                _from=AnchorMarker(col=IMAGE_COLUMN_INDEX - 1, colOff=0, row=r - 1, rowOff=0),
                ext=XDRPositiveSize2D(cx=pixels_to_EMU(width_px), cy=pixels_to_EMU(height_px)),
            )
            xl_img.anchor = anchor
            ws.add_image(xl_img)
        except Exception as exc:
            raise ReportGenerationError(
                f"{row.image_filename}: could not embed image in the report "
                f"({type(exc).__name__}: {exc})",
                original=exc, image_filename=row.image_filename,
            ) from exc

    if processing_status:
        # Phase P1-2: a second sheet saying whether this report is COMPLETE or PARTIAL. The
        # "Image Register" sheet (schema, order, and being the first/active sheet) is unchanged.
        st = wb.create_sheet(STATUS_SHEET_TITLE)
        st.column_dimensions["A"].width = 30
        st.column_dimensions["B"].width = 100
        for r, (field, value) in enumerate([("Field", "Value"), *processing_status], start=1):
            for col_idx, text in enumerate((field, value), start=1):
                c = st.cell(r, col_idx, text)
                c.border = DATA_BORDER
                c.alignment = DATA_ALIGN
                c.font = HEADER_FONT if r == 1 else DATA_FONT
                if r == 1:
                    c.fill = HEADER_FILL

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic save: write to a temp file in the SAME directory (os.replace()'s atomicity
    # guarantee requires the same filesystem), then replace the real path only once the
    # workbook has fully, successfully saved. A failure partway through wb.save() -- or
    # anything after it -- never touches output_path at all, so an existing valid report from
    # a previous run is never left half-overwritten. os.replace() (not os.rename()) because
    # it's documented to work atomically on Windows even when the destination already exists
    # -- same pattern as state.save_state()'s Phase O fix.
    fd, tmp_name = tempfile.mkstemp(dir=str(output_path.parent), prefix=f"{output_path.name}.", suffix=".tmp")
    os.close(fd)  # wb.save() wants a path, not a raw fd -- release the fd, keep the filename
    tmp_path = Path(tmp_name)
    try:
        wb.save(tmp_path)
        # L3: flush the finished temp file to disk before swapping it in (same durability step
        # as state.save_state and the manifest writer).
        with open(tmp_path, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp_path, output_path)
    except BaseException as exc:
        # L3: BaseException so a Ctrl+C during the save also removes our own temp file; the
        # previous report is untouched either way. Only ordinary errors become
        # ReportGenerationError -- KeyboardInterrupt/SystemExit propagate unchanged.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass  # never let clean-up mask the original error
        if not isinstance(exc, Exception):
            raise
        raise ReportGenerationError(
            f"{output_path.name}: failed to save report ({type(exc).__name__}: {exc})",
            original=exc,
        ) from exc
