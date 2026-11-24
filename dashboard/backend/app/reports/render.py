"""CSV and PDF rendering of ReportData. No file paths or secrets are ever written."""

from __future__ import annotations

import csv
import io
from typing import Callable, Optional

from ..ai.records import AnalysisRecord
from ..models import InspectionRecord
from .builder import ReportData, assessment_status
from .pdf import PAGE_H, PAGE_W, PdfDocument, text_width, wrap

REPORT_TITLE = "Vessel Inspection Report"
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(v) -> str:
    """CSV cell, neutralising spreadsheet formula injection."""
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(FORMULA_PREFIXES) else s


def _csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([_cell(v) for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel reads UTF-8


def _photo_ref(r: InspectionRecord) -> str:
    if r.image_status != "resolved" or not r.image_id:
        return "Unavailable"
    return r.image_id


def inspections_csv(d: ReportData) -> bytes:
    header = ["Record ID", "Vessel", "Inspection date", "Date source", "Original report date", "Week",
              "Official Location", "AI suggested location (unverified)", "AI location confidence",
              "Activity identified", "Photograph reference", "Photograph status", "Same photograph appearances",
              "Record source"]
    rows = [[r.record_id, r.vessel_name, r.inspection_date.isoformat() if r.inspection_date else "",
             r.date_source or "", r.report_date or "", r.week,
             r.official_location if r.official_location_labelled else "Not labelled",
             r.ai_suggested_location or "", r.location_confidence or "", (r.activity_identified or "").strip(),
             _photo_ref(r), r.image_status, r.same_photo_appearances,
             {"image_register": "Image register", "excel_import": "Excel import"}.get(r.source, "Weekly JSON report")] for r in d.records]
    return _csv(header, rows)


def analyses_csv(d: ReportData) -> bytes:
    header = ["Analysis ID", "Assessment status", "Simulated", "Vessel", "Location", "Earlier date", "Earlier image",
              "Later date", "Later image", "Interval (days)", "Classification", "Model classification", "Confidence",
              "Comparison reliability", "Review status", "Human review required", "Explanation", "Observations",
              "Deterioration evidence", "Improvement evidence", "Limitations", "Validation adjustments",
              "Provider", "Model", "Prompt version", "Analysed at (UTC)"]
    rows = []
    for a in d.analyses:
        r = a.ai_result
        ev = lambda items: " | ".join(f"{e.region}: {e.change}" for e in items)  # noqa: E731
        rows.append([a.analysis_id, assessment_status(a)[0], "yes" if a.simulated else "no", a.vessel_name,
                     a.location or a.earlier.official_location or "", a.earlier.inspection_date, a.earlier.image_id,
                     a.later.inspection_date, a.later.image_id, a.interval_days, a.classification.value,
                     a.model_classification.value, a.confidence.value, a.comparison_reliability.value,
                     a.review.status.value, "yes" if a.human_review_required else "no", r.explanation,
                     " | ".join(r.visual_observations), ev(r.deterioration_evidence), ev(r.improvement_evidence),
                     " | ".join(r.limitations), " | ".join(a.policy_adjustments), a.provider, a.model,
                     a.prompt_version, a.created_at.isoformat()])
    return _csv(header, rows)


# --------------------------------------------------------------------------- PDF
INK = (0.09, 0.11, 0.16)
MUTED = (0.36, 0.40, 0.47)
ACCENT = (0.13, 0.42, 0.70)       # print-legible shade of the dashboard's light blue
BAND = (0.89, 0.94, 0.99)
WARN = (0.70, 0.40, 0.0)
CLASS_COLOURS = {"DETERIORATED": (0.75, 0.16, 0.15), "AMELIORATED": (0.18, 0.49, 0.20),
                 "STABLE": (0.13, 0.42, 0.70), "NEEDS_REVIEW": (0.70, 0.40, 0.0)}
CLASS_LABELS = {"DETERIORATED": "Deteriorated", "AMELIORATED": "Ameliorated", "STABLE": "Stable", "NEEDS_REVIEW": "Needs Review"}
M = 42  # margin
CONTENT_W = PAGE_W - 2 * M
BOTTOM = PAGE_H - 50


def _fmt(d) -> str:
    return d.strftime("%d %b %Y") if d else "Not recorded"


class _Layout:
    def __init__(self, doc: PdfDocument, title: str):
        self.doc, self.title, self.y = doc, title, 0.0
        self.new_page()

    def new_page(self):
        self.doc.add_page()
        self.doc.text(M, 28, self.title, 8, color=MUTED)
        self.doc.line(M, 34, PAGE_W - M, 34)
        self.y = 56

    def need(self, h: float):
        if self.y + h > BOTTOM:
            self.new_page()

    def heading(self, text: str, size: float = 14):
        self.need(size + 30)
        self.y += 6
        self.doc.text(M, self.y + size, text, size, bold=True, color=INK)
        self.y += size + 6
        self.doc.line(M, self.y, M + CONTENT_W, self.y, color=ACCENT, width=1)
        self.y += 10

    def para(self, text: str, size: float = 9.5, bold=False, color=INK, indent: float = 0, gap: float = 4):
        for ln in wrap(text, CONTENT_W - indent, size, bold):
            self.need(size + 4)
            self.doc.text(M + indent, self.y + size, ln, size, bold, color)
            self.y += size + 3.5
        self.y += gap

    def kv(self, rows: list[tuple[str, str]], key_w: float = 150, size: float = 9.5):
        for k, v in rows:
            lines = wrap(v, CONTENT_W - key_w, size)
            self.need(len(lines) * (size + 3.5) + 2)
            self.doc.text(M, self.y + size, k, size, color=MUTED)
            for i, ln in enumerate(lines):
                self.doc.text(M + key_w, self.y + size + i * (size + 3.5), ln, size, color=INK)
            self.y += len(lines) * (size + 3.5) + 3

    def note_box(self, text: str, color=WARN, fill=(1.0, 0.97, 0.90)):
        lines = wrap(text, CONTENT_W - 16, 9, True)
        h = len(lines) * 12.5 + 10
        self.need(h + 6)
        self.doc.rect(M, self.y, CONTENT_W, h, fill=fill, stroke=color, line_width=0.8)
        for i, ln in enumerate(lines):
            self.doc.text(M + 8, self.y + 15 + i * 12.5, ln, 9, True, color)
        self.y += h + 8

    def table(self, cols: list[tuple[str, float]], rows: list[list[str]], size: float = 7.5):
        widths = [w * CONTENT_W for _, w in cols]
        pad, lh = 3, size + 2.5

        def header():
            self.doc.rect(M, self.y, CONTENT_W, lh + 2 * pad, fill=BAND)
            x = M
            for (name, _), w in zip(cols, widths):
                self.doc.text(x + pad, self.y + pad + size, name, size, True, INK)
                x += w
            self.y += lh + 2 * pad

        self.need(lh * 3)
        header()
        for i, row in enumerate(rows):
            cells = [wrap(str(v), w - 2 * pad, size) for v, w in zip(row, widths)]
            h = max(len(c) for c in cells) * lh + 2 * pad
            if self.y + h > BOTTOM:
                self.new_page()
                header()
            if i % 2:
                self.doc.rect(M, self.y, CONTENT_W, h, fill=(0.97, 0.98, 0.99))
            x = M
            for c, w in zip(cells, widths):
                for j, ln in enumerate(c):
                    self.doc.text(x + pad, self.y + pad + size + j * lh, ln, size, color=INK)
                x += w
            self.y += h
        self.y += 8


def inspection_pdf(d: ReportData, read_image: Callable[[str], Optional[bytes]]) -> bytes:
    title = f"{REPORT_TITLE} - {d.vessel_name}"
    doc = PdfDocument(title)
    L = _Layout(doc, title)
    s = d.summary

    # A. Report information
    doc.text(M, L.y + 22, REPORT_TITLE, 22, True, INK)
    L.y += 34
    doc.text(M, L.y + 12, d.vessel_name, 12, True, ACCENT)
    L.y += 22
    L.heading("A. Report Information", 12)
    L.kv([("Report title", REPORT_TITLE), ("Vessel", d.vessel_name), ("Generated", d.generated_at.strftime("%d %b %Y %H:%M UTC")),
          ("Inspection period", f"{_fmt(d.period[0])} to {_fmt(d.period[1])}"),
          *d.filters.describe()])
    L.para("Sections B and C contain original inspection information exactly as recorded. Section E contains "
           "AI-generated assessments, which are separate from the inspection record and are labelled with their "
           "review status.", 8.5, color=MUTED)

    # B. Summary
    L.heading("B. Inspection Summary")
    L.kv([("Inspection appearances", str(s["appearances"])), ("Unique photographs", str(s["unique_photographs"])),
          ("Inspection dates", str(s["inspection_dates"])), ("Weeks", ", ".join(s["weeks"]) or "None"),
          ("Locations covered", str(len(s["locations"]))), ("Not labelled (no Official Location)", str(s["not_labelled"])),
          ("Photographs unavailable", str(s["missing_photographs"])), ("Saved AI analyses", str(s["analyses"]))])
    if s["locations"]:
        L.table([("Official Location", 0.8), ("Appearances", 0.2)], [[k, str(v)] for k, v in s["locations"].items()])

    # C. Location details
    L.heading("C. Location Details")
    if d.records:
        L.table([("Date", 0.11), ("Week", 0.09), ("Official Location", 0.27), ("AI location (unverified)", 0.2),
                 ("Photo ref.", 0.1), ("Activity", 0.23)],
                [[_fmt(r.inspection_date) + (" *" if r.date_source == "worksheet_name" else ""), r.week,
                  r.official_location if r.official_location_labelled else "Not labelled",
                  r.ai_suggested_location or "-", _photo_ref(r), (r.activity_identified or "-").strip() or "-"]
                 for r in d.records])
        if any(r.date_source == "worksheet_name" for r in d.records):
            L.para("* Date derived from the worksheet name; the original report date was recorded as ambiguous/pending.",
                   8, color=MUTED)
    else:
        L.para("No inspection records match the selected filters.", color=MUTED)

    # D. Historical comparison (only pairs a user actually selected and saved)
    L.heading("D. Historical Comparison")
    L.para("Pairs below were selected by a user in the comparison view. Photographs are not geometrically aligned; "
           "differences in lighting, perspective, camera distance and position are not evidence of condition change.",
           8.5, color=MUTED)
    if not d.analyses:
        L.para("No saved photograph comparisons match the selected filters.", color=MUTED)
    for a in d.analyses:
        _comparison(L, a, read_image)

    # E. AI condition assessment (only real saved analyses)
    L.heading("E. AI Condition Assessment")
    if not d.analyses:
        L.para("No AI condition assessment is available for this selection.", color=MUTED)
    for a in d.analyses:
        _assessment(L, a)

    # footer with page numbers
    total = len(doc.pages)
    for i in range(total):
        label = f"Page {i + 1} of {total}"
        doc.pages[i].append(
            f"BT /F1 8 Tf 0.36 0.40 0.47 rg {PAGE_W - M - text_width(label, 8):.2f} 24 Td ({label}) Tj ET".encode())
        doc.pages[i].append(
            "BT /F1 8 Tf 0.36 0.40 0.47 rg 42 24 Td (Generated from existing inspection records. Original source "
            "files are not modified.) Tj ET".encode())
    return doc.build()


def _comparison(L: _Layout, a: AnalysisRecord, read_image):
    box_w = (CONTENT_W - 12) / 2
    box_h = box_w * 0.75
    L.need(box_h + 70)
    label = assessment_status(a)[0]
    L.para(f"{a.analysis_id}  ·  {a.location or a.earlier.official_location or 'Location not recorded'}  ·  {label}",
           9.5, True, WARN if a.simulated else INK, gap=2)
    top = L.y
    for i, (tag, p) in enumerate((("EARLIER", a.earlier), ("LATER", a.later))):
        x = M + i * (box_w + 12)
        L.doc.rect(x, top, box_w, box_h, fill=(0.95, 0.96, 0.97), stroke=(0.8, 0.83, 0.87))
        data = read_image(p.image_id)
        if data:
            try:
                dw, dh = L.doc.image(data, x, top, box_w, box_h)
            except Exception:  # noqa: BLE001 - unreadable image -> placeholder, never a crash
                data = None
        if not data:
            L.doc.text(x + 10, top + box_h / 2, "Photograph unavailable", 9, color=MUTED)
        L.doc.text(x, top + box_h + 13, f"{tag}  {_fmt(p.inspection_date)}  ·  {p.image_id}  ·  {p.week}", 8.5, True, INK)
    L.y = top + box_h + 22
    L.para(f"Time between inspections: {a.interval_days} days. Limitations reported: "
           + ("; ".join(x.rstrip(". ") for x in a.ai_result.limitations) or "none reported") + ".", 8.5, color=MUTED)


def _assessment(L: _Layout, a: AnalysisRecord):
    r = a.ai_result
    label, sentence = assessment_status(a)
    L.need(120)
    L.para(f"{a.analysis_id}  ·  {_fmt(a.earlier.inspection_date)} ({a.earlier.image_id}) to "
           f"{_fmt(a.later.inspection_date)} ({a.later.image_id})", 10, True, gap=3)
    if a.simulated:
        L.note_box("SIMULATED - " + sentence)
    elif label != "HUMAN-APPROVED":
        L.note_box(f"{label} - " + sentence, color=WARN)
    else:
        L.note_box(f"{label} - " + sentence, color=(0.18, 0.49, 0.20), fill=(0.93, 0.98, 0.93))
    cls = a.classification.value
    L.need(20)
    L.doc.text(M, L.y + 12, f"Classification: {CLASS_LABELS[cls]}", 12, True, CLASS_COLOURS[cls])
    L.y += 20
    rows = [("Confidence", r.confidence.value.title()), ("Comparison reliability", r.comparison_reliability.value.title()),
            ("Same area", r.same_area.value.replace("_", " ").title()),
            ("Human review status", a.review.status.value.replace("_", " ").title())]
    if a.model_classification is not a.classification:
        rows.append(("Model classification", f"{CLASS_LABELS[a.model_classification.value]} (adjusted by validation rules)"))
    last = a.review.history[-1] if a.review.history else None
    if last:
        rows.append(("Last review", f"{last.status.value.replace('_', ' ').title()} by {last.reviewer}, "
                                    f"{last.at.strftime('%d %b %Y')}" + (f" - {last.note}" if last.note else "")))
    rows.append(("Model / prompt", f"{a.provider} / {a.model} / {a.prompt_version}"))
    L.kv(rows)
    for adj in a.policy_adjustments:
        L.para(adj, 8.5, color=WARN)
    L.para("Explanation", 9, True, MUTED, gap=1)
    L.para(r.explanation)
    for heading, items in (("Visual observations", r.visual_observations),
                           ("Deterioration evidence", [f"{e.region}: {e.change} (earlier: {e.earlier_appearance}; later: {e.later_appearance})" for e in r.deterioration_evidence]),
                           ("Improvement evidence", [f"{e.region}: {e.change} (earlier: {e.earlier_appearance}; later: {e.later_appearance})" for e in r.improvement_evidence]),
                           ("Differences not treated as condition changes", r.non_condition_differences),
                           ("Limitations", r.limitations)):
        L.para(heading, 9, True, MUTED, gap=1)
        if items:
            for it in items:
                L.para("- " + it, 9, indent=8, gap=1)
            L.y += 3
        else:
            L.para("None reported", 9, color=MUTED, indent=8)
    L.y += 6
    L.doc.line(M, L.y, M + CONTENT_W, L.y)
    L.y += 10
