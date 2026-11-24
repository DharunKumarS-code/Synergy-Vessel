"""Combines all weekly sources into one in-memory, read-only record set.

Linking rule (verified against real data on 2026-11-18): the JSON
``image_filename`` values are the pre-2026-11-16 names whose files were
deleted, so each JSON record is linked to its photo in ``Extracted Images -
New`` by the SHA-256 stored in ``extracted_photograph.sha256``. When one hash
matches several files in the same week (a photo repeated in that week), the
register row whose Official Location and Report Date agree with the record is
chosen. Register rows not claimed by any JSON record are added as
``source='image_register'`` appearances so no appearance is dropped.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import Settings
from ..models import InspectionRecord, Photograph
from .image_service import ImageFile, ImageIndex, scan_image_dir
from .json_loader import WeekSource, discover_weeks, load_register, load_report
from ..imports.store import IMAGES, WITHDRAWN, iter_committed, read_decisions, read_state
from ..location_intel.service import effective_location

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUNE": 6,
    "JUL": 7, "JULY": 7, "AUG": 8, "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_WS_DATE_RE = re.compile(r"^\s*([A-Za-z]+)\s+(\d{1,2})\s*,?\s*(\d{4})\s*$")
_VESSEL_RE = re.compile(r"^(.*?)\s+Deck\s+Photos", re.IGNORECASE)


def normalise(text: Optional[str]) -> str:
    return " ".join(str(text).split()).casefold() if text is not None else ""


def parse_report_date(value: Any) -> Optional[dt.date]:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.datetime.strptime(value.strip(), "%m/%d/%Y").date()
        except ValueError:
            return None
    return None


def parse_worksheet_date(value: Any) -> Optional[dt.date]:
    if not isinstance(value, str):
        return None
    m = _WS_DATE_RE.match(value)
    if not m or m.group(1).upper() not in _MONTHS:
        return None
    try:
        return dt.date(int(m.group(3)), _MONTHS[m.group(1).upper()], int(m.group(2)))
    except ValueError:
        return None


def vessel_from(excel_filename: Optional[str]) -> tuple[str, str]:
    """(vessel_id, vessel_name) taken from the source workbook filename."""
    m = _VESSEL_RE.match(excel_filename or "")
    name = " ".join(m.group(1).split()) if m else "Unknown vessel"
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown", name


@dataclass
class IntegrationReport:
    data_root: str
    weeks: list[dict[str, Any]] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    invalid_records: list[dict[str, Any]] = field(default_factory=list)
    missing_images: list[dict[str, Any]] = field(default_factory=list)
    ambiguous_images: list[dict[str, Any]] = field(default_factory=list)
    unlinked_image_files: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    loaded_at: str = ""


class InspectionStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.records: list[InspectionRecord] = []
        self.by_id: dict[str, InspectionRecord] = {}
        self.images = ImageIndex([])
        self.report = IntegrationReport(data_root=str(settings.data_root))

    # ------------------------------------------------------------------ build
    @classmethod
    def build(cls, settings: Settings) -> "InspectionStore":
        store = cls(settings)
        weeks = discover_weeks(settings)
        store.images = ImageIndex([w.image_dir for w in weeks])
        raw: list[InspectionRecord] = []
        linked: set[str] = set()
        all_files: list[ImageFile] = []
        for week in weeks:
            load_report(week)
            load_register(week, settings)
            files = scan_image_dir(week.image_dir, week.folder.name, settings.allowed_image_suffixes)
            if not files:
                week.issues.append(f"no images found in '{settings.image_dir_name}'")
            for f in files:
                store.images.add(f)
            all_files.extend(files)
            built = store._build_week(week, files)
            raw.extend(built)
            linked.update(r.image_id for r in built if r.image_id)
            store._summarise_week(week, built, files)
        raw.extend(store._load_imports())

        sha_counts = Counter(
            r.extracted_photograph.sha256 for r in raw if r.extracted_photograph and r.extracted_photograph.sha256
        )
        for r in raw:
            if r.extracted_photograph and r.extracted_photograph.sha256:
                r.same_photo_appearances = sha_counts[r.extracted_photograph.sha256]
        raw.sort(key=lambda r: (r.week_number is None, r.week_number or 0, r.source != "json_report",
                                r.serial_number or 0, r.register_serial_number or 0))
        seen: Counter[str] = Counter()
        for r in raw:
            seen[r.record_id] += 1
            if seen[r.record_id] > 1:  # keep both; make the id unique
                r.issues.append(f"duplicate record_id '{r.record_id}' renamed")
                r.record_id = f"{r.record_id}~{seen[r.record_id]}"
            store.by_id[r.record_id] = r
        store.records = raw
        store._finalise_report(weeks, linked, all_files, sha_counts)
        return store

    def _build_week(self, week: WeekSource, files: list[ImageFile]) -> list[InspectionRecord]:
        ph = self.settings.unlabelled_placeholder
        files_by_name = {f.path.name: f for f in files}
        files_by_sha: dict[str, list[ImageFile]] = defaultdict(list)
        for f in files:
            files_by_sha[f.sha256].append(f)
        reg_by_name = {str(r["image_filename"]): r for r in week.register_rows}
        used: set[str] = set()
        out: list[InspectionRecord] = []

        for rec in week.records:
            issues = list(rec.get("_issues", []))
            photo = rec.get("extracted_photograph")
            photo_model = Photograph(**{k: photo.get(k) for k in Photograph.model_fields}) if isinstance(photo, dict) else None
            sha = photo_model.sha256 if photo_model else None
            image: Optional[ImageFile] = None
            status, method = "missing", None
            if not sha:
                issues.append("record has no photograph sha256; cannot link image")
            else:
                cands = [f for f in files_by_sha.get(sha, []) if f.path.name not in used]
                if not cands:
                    issues.append("no unclaimed image file with matching sha256")
                elif len(cands) == 1:
                    image, status, method = cands[0], "resolved", "sha256"
                else:
                    want_loc = normalise(rec.get("official_location"))
                    narrowed = []
                    for f in cands:
                        reg = reg_by_name.get(f.path.name)
                        if reg is None:
                            continue
                        reg_loc = normalise(reg.get("official_location") or ph)
                        if reg_loc == want_loc and str(reg.get("report_date")) == str(rec.get("report_date")):
                            narrowed.append(f)
                    if len(narrowed) == 1:
                        image, status, method = narrowed[0], "resolved", "sha256+register_location"
                    else:
                        image, status, method = cands[0], "ambiguous", "sha256_first_of_many"
                        issues.append(f"{len(cands)} files share this sha256; chose first in order")
            if image:
                used.add(image.path.name)
            reg = reg_by_name.get(image.path.name) if image else None
            out.append(self._make(week, rec, "json_report", image, status, method, photo_model, reg, issues))

        # register appearances not represented in the JSON report
        for reg in week.register_rows:
            name = str(reg["image_filename"])
            if name in used:
                continue
            f = files_by_name.get(name)
            issues = ["appearance present in image register but not in JSON report"]
            if f is None:
                issues.append("register image file missing on disk")
            photo_model = Photograph(sha256=f.sha256, size_bytes=f.size_bytes) if f else None
            rec = {
                "serial_number": None,
                "image_filename": None,
                "report_date": reg.get("report_date"),
                "original_excel_filename": reg.get("original_excel_filename"),
                "worksheet_name": reg.get("worksheet_name"),
                "official_location": reg.get("official_location"),
            }
            out.append(self._make(week, rec, "image_register", f, "resolved" if f else "missing",
                                  "register_filename" if f else None, photo_model, reg, issues))
            if f:
                used.add(name)
        return out

    def _load_imports(self) -> list[InspectionRecord]:
        """Phase 7: records from committed Excel imports (separate store, read-only here)."""
        out: list[InspectionRecord] = []
        problems: list[str] = []
        for manifest, folder in iter_committed(self.settings.import_dir, problems):
            if read_state(folder)[0] == WITHDRAWN:
                continue  # Phase 8.4: a withdrawn import contributes no records (its files are kept)
            img_dir = folder / IMAGES
            decisions, decision_problem = read_decisions(folder)
            if decision_problem:
                problems.append(decision_problem)
            self.images.approve(img_dir)
            for row in manifest["rows"]:
                if row.get("status") != "imported" or not row.get("record_id"):
                    continue
                rid, sha = row["record_id"], row.get("sha256")
                path = img_dir / f"{sha}.{row.get('image_format') or 'jpg'}"
                status = "resolved" if sha and path.is_file() else "missing"
                if status == "resolved":
                    self.images.add(ImageFile(rid, path.resolve(), f"imports/{folder.name}", sha, path.stat().st_size))
                official, loc_method, loc_review = effective_location(row, decisions.get(str(row.get("row_number"))))
                week_no = row.get("week_number")
                try:
                    date = dt.date.fromisoformat(row["inspection_date"]) if row.get("inspection_date") else None
                except ValueError:
                    date = None
                out.append(InspectionRecord(
                    record_id=rid, source="excel_import", import_id=folder.name,
                    week=f"Week {week_no} (imported)" if week_no else "Imported", week_folder=f"imports/{folder.name}",
                    week_number=week_no, vessel_id=row.get("vessel_id") or "unknown",
                    vessel_name=row.get("vessel_name") or "Unknown vessel", serial_number=row.get("serial_number"),
                    original_image_filename=row.get("image_filename"), report_date=row.get("report_date"),
                    original_excel_filename=row.get("original_excel_filename"), worksheet_name=row.get("worksheet_name"),
                    official_location=official, ai_suggested_location=None,
                    activity_identified=row.get("activity_identified"), location_confidence=row.get("location_confidence"),
                    extracted_photograph=Photograph(embedded=True, format=row.get("image_format"), sha256=sha,
                                                    size_bytes=row.get("image_size"), width_px=row.get("width_px"),
                                                    height_px=row.get("height_px")),
                    official_location_labelled=bool(official and normalise(official) != normalise(self.settings.unlabelled_placeholder)),
                    inspection_date=date, date_source=row.get("date_source"),
                    original_location=row.get("official_location"), location_method=loc_method, location_review=loc_review,
                    register_image_filename=row.get("image_filename"), register_serial_number=row.get("serial_number"),
                    image_id=rid if status == "resolved" else None,
                    image_url=f"/api/images/{rid}" if status == "resolved" else None,
                    image_status=status, image_match_method="excel_import" if status == "resolved" else None,
                    issues=list(row.get("warnings") or []) + ([] if status == "resolved" else ["imported photograph file is missing"]),
                ))
        self.report.issues.extend(problems)
        return out

    def _make(self, week, rec, source, image, status, method, photo, reg, issues) -> InspectionRecord:
        ph = self.settings.unlabelled_placeholder
        report_date = rec.get("report_date")
        d_report = parse_report_date(report_date)
        d_ws = parse_worksheet_date(rec.get("worksheet_name"))
        if d_report:
            date, date_src = d_report, "report_date"
            if d_ws and d_ws != d_report:
                issues.append(f"report_date {d_report} disagrees with worksheet_name date {d_ws}")
        elif d_ws:
            date, date_src = d_ws, "worksheet_name"
        else:
            date, date_src = None, None
            issues.append(f"no interpretable date (report_date={report_date!r})")
        vessel_id, vessel_name = vessel_from(rec.get("original_excel_filename"))
        if vessel_name == "Unknown vessel":
            issues.append("vessel name not found in original_excel_filename")
        official = rec.get("official_location")
        image_id = image.image_id if image else None
        serial = rec.get("serial_number")
        record_id = image_id or f"{(week.folder.name).replace(' ', '_')}-json-{rec.get('_index', 0):04d}"
        return InspectionRecord(
            record_id=record_id,
            source=source,
            week=week.week_label,
            week_folder=week.folder.name,
            week_number=week.week_number,
            vessel_id=vessel_id,
            vessel_name=vessel_name,
            serial_number=serial if isinstance(serial, int) else None,
            original_image_filename=rec.get("image_filename"),
            report_date=None if report_date is None else str(report_date),
            original_excel_filename=rec.get("original_excel_filename"),
            worksheet_name=rec.get("worksheet_name"),
            official_location=official,
            ai_suggested_location=rec.get("ai_suggested_location"),
            activity_identified=rec.get("activity_identified"),
            location_confidence=rec.get("location_confidence"),
            extracted_photograph=photo,
            official_location_labelled=bool(official and str(official).strip() and normalise(official) != normalise(ph)),
            inspection_date=date,
            date_source=date_src,
            register_image_filename=str(reg["image_filename"]) if reg else None,
            register_serial_number=reg.get("serial_number") if reg and isinstance(reg.get("serial_number"), int) else None,
            image_id=image_id,
            image_url=f"/api/images/{image_id}" if image_id else None,
            image_status=status,
            image_match_method=method,
            issues=issues,
        )

    # ----------------------------------------------------------------- report
    def _summarise_week(self, week: WeekSource, built: list[InspectionRecord], files: list[ImageFile]) -> None:
        self.report.weeks.append({
            "week": week.week_label,
            "folder": week.folder.name,
            "week_number": week.week_number,
            "readable": week.readable,
            "declared_record_count": week.report_meta.get("record_count"),
            "json_records": sum(r.source == "json_report" for r in built),
            "register_only_appearances": sum(r.source == "image_register" for r in built),
            "register_rows": len(week.register_rows),
            "image_files": len(files),
            "resolved_images": sum(r.image_status == "resolved" for r in built),
            "invalid_records": len(week.invalid_records),
            "issues": week.issues,
        })
        for inv in week.invalid_records:
            self.report.invalid_records.append({"week": week.week_label, **inv})
        self.report.issues.extend(f"{week.folder.name}: {i}" for i in week.issues)

    def _finalise_report(self, weeks, linked, all_files, sha_counts) -> None:
        rep = self.report
        recs = self.records
        rep.missing_images = [
            {"record_id": r.record_id, "week": r.week, "serial_number": r.serial_number, "issues": r.issues}
            for r in recs if r.image_status == "missing"
        ]
        rep.ambiguous_images = [
            {"record_id": r.record_id, "week": r.week, "serial_number": r.serial_number}
            for r in recs if r.image_status == "ambiguous"
        ]
        rep.unlinked_image_files = sorted(f"{f.week_folder}/{f.path.name}" for f in all_files if f.image_id not in linked)
        if self.images.duplicate_ids:
            rep.issues.append(f"duplicate image ids across weeks: {sorted(set(self.images.duplicate_ids))}")
        rep.totals = {
            "weeks_discovered": len(weeks),
            "weeks_readable": sum(w.readable for w in weeks),
            "json_records": sum(r.source == "json_report" for r in recs),
            "register_only_appearances": sum(r.source == "image_register" for r in recs),
            "imported_records": sum(r.source == "excel_import" for r in recs),
            "total_appearances": len(recs),
            "invalid_records": len(rep.invalid_records),
            "unique_photographs": len(sha_counts),
            "image_files_on_disk": len(all_files),
            "resolved_images": sum(r.image_status == "resolved" for r in recs),
            "ambiguous_images": len(rep.ambiguous_images),
            "missing_images": len(rep.missing_images),
            "official_location_labelled": sum(r.official_location_labelled for r in recs),
            "official_location_unlabelled": sum(not r.official_location_labelled for r in recs),
            "dated_records": sum(r.inspection_date is not None for r in recs),
            "dated_from_worksheet_name": sum(r.date_source == "worksheet_name" for r in recs),
            "records_with_issues": sum(bool(r.issues) for r in recs),
        }
        rep.loaded_at = dt.datetime.now().isoformat(timespec="seconds")
