from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import openpyxl

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
BANNER_DATE_RE = re.compile(r"Date:\s*(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})")
SHEET_NAME_DATE_RE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2})\s+(\d{4})")

PENDING_DATE_TOKEN = "Timestamp_Pending"


class WorkbookExtractionError(RuntimeError):
    """A source workbook could not be opened or parsed (corrupt file, missing path, unexpected
    internal structure, etc.). Carries the workbook filename and the original exception so the
    caller can log full detail -- deliberately never caught here and turned into an empty/
    partial ExtractionResult, which would look like a successful extraction of zero photos.
    """

    def __init__(self, path: Path, original: Exception):
        self.workbook_path = path
        self.original_exception = original
        super().__init__(f"{path.name}: extraction failed ({type(original).__name__}: {original})")


def _parse_month_day_year(day: str, month_name: str, year: str) -> date | None:
    month = MONTH_MAP.get(month_name.upper())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def resolve_report_date(banner_text: str | None, sheet_name: str) -> tuple[date | None, str]:
    """Resolve a single report date for the sheet, cross-checking the banner cell against the
    worksheet name. Returns (date_or_None, note). A note is always returned so the caller can
    log exactly why a date was or wasn't trusted.

    Mirrors a real precedent found in this project: Week 34's banner said "23 AUG 2026" while
    its sheet name said "AUG 30 2026" -- the two disagreed, and the existing (manual) process
    fell back to Timestamp_Pending filenames rather than guessing. We reproduce that rule
    exactly: only trust a date when both sources parse AND agree.
    """
    banner_date = None
    if banner_text:
        m = BANNER_DATE_RE.search(banner_text)
        if m:
            banner_date = _parse_month_day_year(m.group(1), m.group(2), m.group(3))

    sheet_date = None
    m2 = SHEET_NAME_DATE_RE.search(sheet_name.strip())
    if m2:
        sheet_date = _parse_month_day_year(m2.group(2), m2.group(1), m2.group(3))

    if banner_date is None and sheet_date is None:
        return None, "Neither banner nor sheet name yielded a parseable date."
    if banner_date is None:
        return None, f"Banner date unparseable; sheet name suggested {sheet_date} but cannot confirm alone."
    if sheet_date is None:
        return None, f"Sheet name date unparseable; banner suggested {banner_date} but cannot confirm alone."
    if banner_date != sheet_date:
        return None, f"Banner date ({banner_date}) and sheet name date ({sheet_date}) disagree."
    return banner_date, "Banner and sheet name dates agree."


@dataclass
class ExtractedImage:
    anchor_row: int
    anchor_col: int
    official_location: str
    image_bytes: bytes
    image_ext: str
    source_workbook: str
    worksheet_name: str


@dataclass
class ExtractionResult:
    images: list[ExtractedImage] = field(default_factory=list)
    report_date: date | None = None
    date_note: str = ""
    warnings: list[str] = field(default_factory=list)


def _find_headings(ws, min_merge_width: int) -> list[tuple[int, str]]:
    """Return [(row, heading_text)] sorted by row, for merged ranges wide enough to be a
    section heading rather than a photo block (photo blocks are narrow; headings span most
    of the sheet width). Row 1 (the banner) is always excluded.
    """
    headings = []
    for rng in ws.merged_cells.ranges:
        if rng.min_row == 1:
            continue
        width = rng.max_col - rng.min_col + 1
        if width < min_merge_width:
            continue
        value = ws.cell(rng.min_row, rng.min_col).value
        if isinstance(value, str) and value.strip():
            headings.append((rng.min_row, value.strip()))
    return sorted(headings, key=lambda t: t[0])


def _heading_for_row(headings: list[tuple[int, str]], row: int) -> str:
    """A heading applies to every image row strictly after it, not its own row. Using a
    non-strict "<=" here misattributes the rare image whose anchor row happens to coincide
    with the very next heading's row (an irregular-spacing edge case confirmed in real data at
    Week 30 row 371) -- strict "<" was validated against all 1,058 known-correct labels across
    all 11 weeks with zero mismatches.
    """
    current = "Not Labelled"
    for heading_row, text in headings:
        if heading_row < row:
            current = text
        else:
            break
    return current


def _image_ext(image) -> str:
    fmt = getattr(image, "format", None)
    if fmt:
        return fmt.lower() if fmt.lower() != "jpeg" else "jpg"
    return "jpg"


def extract_workbook(path: Path, min_merge_width: int) -> ExtractionResult:
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        # Covers a missing path, a corrupt/non-Excel file, or anything else load_workbook()
        # itself can raise. Deliberately re-raised (never swallowed into an empty result) --
        # the caller (extract_week -> process_week -> run_weeks, Phase Q) must see this as a
        # real failure for this week, not quietly process zero photos as if that were correct.
        raise WorkbookExtractionError(path, exc) from exc

    try:
        result = _extract_from_workbook(wb, path, min_merge_width)
    except Exception as exc:
        # A workbook that opened but then failed during parsing (unexpected internal
        # structure, a malformed embedded image, etc.) gets the same clear treatment as an
        # outright open failure -- never a partial/misleading successful result.
        raise WorkbookExtractionError(path, exc) from exc
    finally:
        wb.close()  # always release the file handle, success or failure, since it opened fine

    return result


def _extract_from_workbook(wb, path: Path, min_merge_width: int) -> ExtractionResult:
    result = ExtractionResult()
    if len(wb.sheetnames) != 1:
        result.warnings.append(
            f"{path.name}: expected exactly 1 worksheet, found {len(wb.sheetnames)}; using the first."
        )
    sheet_name = wb.sheetnames[0]
    ws = wb[sheet_name]

    banner_text = ws.cell(1, 1).value
    report_date, date_note = resolve_report_date(banner_text, sheet_name)
    result.report_date = report_date
    result.date_note = date_note

    headings = _find_headings(ws, min_merge_width)

    images_with_pos = []
    for img in ws._images:
        anchor = img.anchor
        row = anchor._from.row + 1  # openpyxl anchors are 0-indexed; cells are 1-indexed
        col = anchor._from.col + 1
        images_with_pos.append((row, col, img))

    # reading order: top-to-bottom, then left-to-right within the same visual row block
    images_with_pos.sort(key=lambda t: (t[0], t[1]))

    for row, col, img in images_with_pos:
        location = _heading_for_row(headings, row)
        result.images.append(
            ExtractedImage(
                anchor_row=row,
                anchor_col=col,
                official_location=location,
                image_bytes=img._data(),
                image_ext=_image_ext(img),
                source_workbook=path.name,
                worksheet_name=sheet_name,
            )
        )

    return result


def _dedupe_exact_images(images: list[ExtractedImage], warnings: list[str]) -> list[ExtractedImage]:
    """Drop exact (byte-for-byte) duplicate images, keeping the first occurrence in reading
    order. Confirmed against real data: Week 30's source file embeds the same 2 photos twice --
    once early (before any heading) and again later under an unrelated heading ("PORT AND
    STARBOARD MAIN DECK FACING FORWARD VIEW"). The existing hand-built reports kept the first,
    unlabeled occurrence and dropped the later mislabeled repeat, so that is the rule here too.
    Every drop is logged rather than silently discarded.
    """
    seen: dict[str, int] = {}
    kept = []
    for position, img in enumerate(images, start=1):
        content_hash = hashlib.md5(img.image_bytes).hexdigest()
        if content_hash in seen:
            warnings.append(
                f"Dropped duplicate image at position {position} "
                f"({img.source_workbook}, row {img.anchor_row}, location '{img.official_location}'): "
                f"byte-identical to the image already kept at position {seen[content_hash]}."
            )
            continue
        seen[content_hash] = len(kept) + 1
        kept.append(img)
    return kept


def extract_week(source_workbooks: list[Path], min_merge_width: int = 10) -> ExtractionResult:
    """Extract every photo across all of a week's source workbooks, in combined reading order
    (file order as given, then top-to-bottom/left-to-right within each file), with exact
    duplicate images removed (see _dedupe_exact_images). The caller (discovery.find_source_workbooks)
    is responsible for giving files in a stable order.
    """
    combined = ExtractionResult()
    report_dates = []
    for path in source_workbooks:
        single = extract_workbook(path, min_merge_width)
        combined.images.extend(single.images)
        combined.warnings.extend(single.warnings)
        report_dates.append((path.name, single.report_date, single.date_note))

    combined.images = _dedupe_exact_images(combined.images, combined.warnings)

    all_dates = [d for (_, d, _) in report_dates]
    distinct_dates = set(all_dates)
    if all_dates and None not in distinct_dates and len(distinct_dates) == 1:
        # trusted only when every source file in the week independently confirmed the same
        # date -- a single unconfirmed file is reason enough to fall back to pending, since we
        # then have no evidence its photos share the other files' date.
        combined.report_date = all_dates[0]
        combined.date_note = "Every source workbook for this week confirmed the same report date."
    else:
        combined.report_date = None
        if len(distinct_dates - {None}) > 1:
            combined.date_note = f"Source workbooks for this week disagree on report date: {distinct_dates - {None}}."
        else:
            combined.date_note = "Not every source workbook for this week yielded a confirmed report date."
        combined.warnings.append(combined.date_note)
        for name, _, note in report_dates:
            combined.warnings.append(f"{name}: {note}")

    return combined
