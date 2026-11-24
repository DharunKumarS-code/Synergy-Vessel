"""Read-only discovery and loading of weekly JSON reports and image registers.

Nothing here writes to disk. JSON is opened in text mode for reading and
registers are opened with ``openpyxl`` in ``read_only`` mode.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..config import Settings

WEEK_RE = re.compile(r"week\s*(\d+)", re.IGNORECASE)

EXPECTED_JSON_KEYS = (
    "serial_number",
    "image_filename",
    "report_date",
    "original_excel_filename",
    "worksheet_name",
    "official_location",
    "ai_suggested_location",
    "extracted_photograph",
    "activity_identified",
    "location_confidence",
)

REGISTER_HEADERS = {
    "Serial Number": "serial_number",
    "Image Filename": "image_filename",
    "Report Date": "report_date",
    "Original Excel Filename": "original_excel_filename",
    "Worksheet Name": "worksheet_name",
    "Official Location": "official_location",
}


@dataclass
class WeekSource:
    folder: Path
    week_number: Optional[int]
    report_path: Path
    image_dir: Path
    register_path: Optional[Path]
    week_label: str = ""
    report_meta: dict[str, Any] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    invalid_records: list[dict[str, Any]] = field(default_factory=list)
    register_rows: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    readable: bool = False


def week_number_from(name: str) -> Optional[int]:
    m = WEEK_RE.search(name)
    return int(m.group(1)) if m else None


def discover_weeks(settings: Settings) -> list[WeekSource]:
    """Every direct child folder of the data root that contains the report JSON."""
    weeks: list[WeekSource] = []
    root = settings.data_root
    if not root.is_dir():
        return weeks
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        report = child / settings.report_filename
        if not (child.is_dir() and report.is_file()):
            continue
        num = week_number_from(child.name)
        register = None
        if num is not None:
            candidate = child / settings.register_template.format(week=num)
            register = candidate if candidate.is_file() else None
        weeks.append(
            WeekSource(
                folder=child,
                week_number=num,
                report_path=report,
                image_dir=child / settings.image_dir_name,
                register_path=register,
                week_label=child.name,
            )
        )
    weeks.sort(key=lambda w: (w.week_number is None, w.week_number or 0, w.folder.name))
    return weeks


def load_report(week: WeekSource) -> None:
    """Populate ``week.records``; problems are recorded, never raised."""
    try:
        with week.report_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        week.issues.append(f"report unreadable: {type(exc).__name__}: {exc}")
        return
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        week.issues.append("report has no 'records' list")
        return
    week.readable = True
    week.report_meta = {k: v for k, v in data.items() if k != "records"}
    if isinstance(data.get("week"), str):
        week.week_label = data["week"]
    declared = data.get("record_count")
    if declared != len(data["records"]):
        week.issues.append(
            f"record_count {declared!r} != {len(data['records'])} records present"
        )
    for idx, rec in enumerate(data["records"]):
        if not isinstance(rec, dict):
            week.invalid_records.append(
                {"index": idx, "reason": f"record is {type(rec).__name__}, not object", "raw": repr(rec)[:200]}
            )
            continue
        missing = [k for k in EXPECTED_JSON_KEYS if k not in rec]
        rec = dict(rec)
        rec["_index"] = idx
        rec["_issues"] = [f"missing keys: {', '.join(missing)}"] if missing else []
        week.records.append(rec)


def load_register(week: WeekSource, settings: Settings) -> None:
    """Read the week's image register (read-only) into ``week.register_rows``."""
    if week.register_path is None:
        week.issues.append("image register not found")
        return
    try:
        import openpyxl

        wb = openpyxl.load_workbook(week.register_path, read_only=True, data_only=True)
    except Exception as exc:  # corrupt workbook, permission, etc.
        week.issues.append(f"register unreadable: {type(exc).__name__}: {exc}")
        return
    try:
        ws = wb[settings.register_sheet] if settings.register_sheet in wb.sheetnames else wb.active
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None) or ()
        col = {h: i for i, h in enumerate(header) if h in REGISTER_HEADERS}
        absent = [h for h in REGISTER_HEADERS if h not in col]
        if absent:
            week.issues.append(f"register missing columns: {', '.join(absent)}")
            return
        for row in rows:
            if row is None or all(v is None for v in row):
                continue
            item = {REGISTER_HEADERS[h]: (row[i] if i < len(row) else None) for h, i in col.items()}
            if item["image_filename"]:
                week.register_rows.append(item)
    finally:
        wb.close()
