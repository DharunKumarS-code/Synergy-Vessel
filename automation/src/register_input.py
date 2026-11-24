from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from . import discovery
from . import weekly_image_report as wr

REPORT_DATE_COL, WORKBOOK_COL, WORKSHEET_COL, LOCATION_COL = 3, 4, 5, 6


class RegisterInputError(RuntimeError):
    """A week's register is missing, incomplete or inconsistent with its photos. Lists every
    problem found; the week must not be processed."""


@dataclass
class RegisterRow:
    image_id: str               # primary identifier, e.g. "W30-0001"
    serial_number: int
    image_filename: str         # e.g. "W30-0001.jpg"
    image_path: Path
    sha256: str
    report_date: str            # the register's text, verbatim ("8/2/2026" or "AMBIGUOUS/PENDING")
    source_workbook: str
    worksheet_name: str
    official_location: str | None  # verbatim; None = blank = needs AI assessment
    image_bytes: bytes = field(repr=False, default=b"")

    @property
    def needs_ai(self) -> bool:
        return self.official_location is None


def week_number(week_name: str) -> int:
    match = discovery.WEEK_DIR_RE.match(week_name.strip())
    if match is None:
        raise RegisterInputError(f"{week_name!r} is not a 'Week N' folder name")
    return int(match.group(1))


def register_path(week: discovery.WeekFolder) -> Path:
    return week.path / wr.register_filename(week_number(week.name))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_register_week(week: discovery.WeekFolder) -> list[RegisterRow]:
    """Validate the week's register (see module docstring) and return every row in register
    order. Raises RegisterInputError on any problem; never writes anything."""
    number = week_number(week.name)
    if not week.source_workbooks:
        raise RegisterInputError(f"{week.name}: no source workbooks -- cannot confirm the register is complete")
    try:
        expected = wr.count_source_images(week.source_workbooks)
    except Exception as exc:  # noqa: BLE001 -- reported as a refusal, never a partial result
        raise RegisterInputError(f"{week.name}: source workbooks unreadable "
                                 f"({type(exc).__name__}: {exc})") from exc
    problems = wr.check_completed_week(week.path, number, expected)
    if problems:
        raise RegisterInputError(f"{week.name}: register is not complete -- " + "; ".join(problems))

    reg = register_path(week)
    images_dir = week.path / wr.NEW_IMAGES_DIRNAME
    wb = openpyxl.load_workbook(reg)  # read-only use: never saved
    try:
        ws = wb[wr.SHEET_TITLE]
        embedded = {im.anchor._from.row + 1: _sha256(im._data()) for im in ws._images}
        values = [[c.value for c in r] for r in ws.iter_rows(min_row=2, max_row=ws.max_row)]
    finally:
        wb.close()

    rows: list[RegisterRow] = []
    problems = []
    seen_ids: set[str] = set()
    for offset, cells in enumerate(values):
        excel_row = offset + 2
        serial, filename = cells[0], cells[1]
        expected_id = f"W{number:02d}-{serial:04d}" if isinstance(serial, int) else None
        if expected_id is None or not isinstance(filename, str):
            problems.append(f"row {excel_row}: serial number / image filename missing or invalid")
            continue
        image_id = Path(filename).stem
        if Path(filename).name != filename or image_id != expected_id:
            problems.append(f"row {excel_row}: filename {filename!r} is not {expected_id}.<ext>")
            continue
        if image_id in seen_ids:
            problems.append(f"row {excel_row}: duplicate Image ID {image_id}")
            continue
        seen_ids.add(image_id)
        path = images_dir / filename
        try:
            data = path.read_bytes()
        except OSError as exc:
            problems.append(f"row {excel_row}: {filename} unreadable ({type(exc).__name__}: {exc})")
            continue
        digest = _sha256(data)
        if digest != embedded.get(excel_row):
            problems.append(f"row {excel_row}: {filename} SHA-256 does not match the register's embedded photo")
            continue
        location = cells[LOCATION_COL - 1]
        if isinstance(location, str) and location == "":
            location = None
        rows.append(RegisterRow(
            image_id=image_id, serial_number=serial, image_filename=filename, image_path=path,
            sha256=digest, report_date=str(cells[REPORT_DATE_COL - 1] or ""),
            source_workbook=str(cells[WORKBOOK_COL - 1] or ""),
            worksheet_name=str(cells[WORKSHEET_COL - 1] or ""),
            official_location=location, image_bytes=data,
        ))
    if problems:
        raise RegisterInputError(f"{week.name}: register refused -- " + "; ".join(problems))
    if [r.serial_number for r in rows] != list(range(1, len(rows) + 1)):
        raise RegisterInputError(f"{week.name}: register serial numbers are not 1..{len(rows)} in order")
    return rows
