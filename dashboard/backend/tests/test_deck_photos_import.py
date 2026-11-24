"""Phase 8.5 -- Excel compatibility: original Deck Photos workbooks and Image Register header/layout variations.
All workbooks are generated in memory; every folder is temporary."""

from __future__ import annotations

import io

import openpyxl
import pytest
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import PatternFill

from conftest import jpeg_bytes
from test_imports import GOOD, HEADER, commit, corrupt_media, make, preview, row, statuses, tree, workbook  # noqa: F401

DECK_NAME = "Nord Kudu  Deck Photos -16 NOV 2026 Part 1 (1).xlsx"
BANNER = " DECK PHOTOS\nM.V. NORD KUDU\nDate: 16 NOV 2026"


def deck(sheets=None, *, extra_sheets=()) -> bytes:
    """sheets: [(title, banner, headings {row: text}, photos [(cell, bytes)])] -- the real layout: banner in A1
    (merged), headings merged 14 columns wide, photographs anchored in the grid."""
    sheets = sheets if sheets is not None else [("NOV 16 2026 ", BANNER, {10: "FORECASTLE DECK AREA", 20: "CARGO HOLD NO. 1"},
                                                  [("H5", jpeg_bytes(301)), ("A5", jpeg_bytes(302)), ("A12", jpeg_bytes(303)),
                                                   ("H20", jpeg_bytes(304)), ("A25", jpeg_bytes(305))])]
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, banner, headings, photos in sheets:
        ws = wb.create_sheet(title)
        if banner is not None:
            ws["A1"] = banner
            ws.merge_cells("A1:N3")
        for r, text in headings.items():
            ws.cell(r, 1, text)
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=14)
        for cell, data in photos:
            ws.add_image(XLImage(io.BytesIO(data)), cell)
    for title in extra_sheets:
        wb.create_sheet(title)["A1"] = "notes"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def rows_of(p):
    return [r for r in p["rows"] if r["status"] != "skipped"]


# ===================================================================== Deck Photos: the original workbook format
def test_deck_workbook_preview_mirrors_the_extraction_rules(make, tmp_path):
    c = make()
    before = tree(tmp_path)
    r = preview(c, deck(), name=DECK_NAME)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["source_format"] == "deck_photos" and p["can_import"] and p["summary"]["ready"] == 5
    got = [(x["serial_number"], x["source_cell"], x["official_location"]) for x in rows_of(p)]
    # reading order: top-to-bottom, then left-to-right; a heading applies strictly below its own row
    assert got == [(1, "A5", None), (2, "H5", None), (3, "A12", "FORECASTLE DECK AREA"),
                   (4, "H20", "FORECASTLE DECK AREA"), (5, "A25", "CARGO HOLD NO. 1")]
    first = rows_of(p)[0]
    assert first["inspection_date"] == "2026-11-16" and first["date_source"] == "report_date" and first["report_date"] == "11/16/2026"
    assert first["vessel_name"] == "Nord Kudu" and first["worksheet_name"] == "NOV 16 2026"
    assert first["image_filename"] == "DP-20261116-0001.jpg" and first["warnings"] == []
    assert "not labelled" in " ".join(first["notes"])
    assert tree(tmp_path) == before                                   # preview writes nothing


def test_deck_workbook_commit_integrity_and_rollback(make, data_root):
    c = make()
    prod = tree(data_root)
    res = commit(c, deck(), name=DECK_NAME)
    assert res.status_code == 200, res.text
    iid = res.json()["import_id"]
    recs = sorted((r for r in c.app.state.store.records if r.import_id == iid), key=lambda r: r.record_id)
    assert [r.official_location for r in recs] == [None, None, "FORECASTLE DECK AREA", "FORECASTLE DECK AREA", "CARGO HOLD NO. 1"]
    assert all(r.original_excel_filename == DECK_NAME and r.vessel_name == "Nord Kudu" for r in recs)
    assert c.get(f"/api/imports/{iid}").json()["source_format"] == "deck_photos"
    assert c.get(f"/api/imports/{iid}/verify").json()["valid"] is True
    assert c.post(f"/api/imports/{iid}/withdraw", json={"reason": "test rollback", "confirm_import_id": iid}).status_code == 200
    assert not [r for r in c.app.state.store.records if r.import_id == iid]
    assert tree(data_root) == prod


@pytest.mark.parametrize("title,banner,source,warn,error", [
    ("NOV 16 2026 ", BANNER, "report_date", None, None),                                  # both agree
    ("NOV 23 2026 ", BANNER, "worksheet_name", "disagrees with the worksheet-name date", None),  # Week 34 case
    ("NOV 16 2026", " DECK PHOTOS\nM.V. NORD KUDU", "worksheet_name", "banner has no readable date", None),
    ("Deck", BANNER, "report_date", "is not a date; the banner date", None),
    ("Deck", " DECK PHOTOS", None, None, "No valid inspection date"),
])
def test_deck_date_rules(make, title, banner, source, warn, error):
    c = make()
    p = preview(c, deck([(title, banner, {}, [("A5", jpeg_bytes(310))])]), name=DECK_NAME).json()
    r = rows_of(p)[0]
    assert r["date_source"] == source
    if title.startswith("NOV 23"):
        assert r["inspection_date"] == "2026-11-23"                 # the sheet date wins (Phase 8 decision)
    assert (warn is None and not r["warnings"]) or any(warn in w for w in r["warnings"])
    if error:
        assert r["status"] == "invalid" and error in r["errors"][0] and p["can_import"] is False


def test_vessel_from_banner_when_the_file_was_renamed_and_unknown_otherwise(make):
    c = make()
    p = preview(c, deck(), name="photos.xlsx").json()
    assert {r["vessel_name"] for r in rows_of(p)} == {"Nord Kudu"} and not rows_of(p)[0]["warnings"]
    p = preview(c, deck([("NOV 16 2026", " DECK PHOTOS\nDate: 16 NOV 2026", {}, [("A5", jpeg_bytes(311))])]), name="photos.xlsx").json()
    assert rows_of(p)[0]["vessel_name"] == "Unknown vessel" and "Unknown vessel" in rows_of(p)[0]["warnings"][0]


def test_multiple_sheets_continue_serials_and_other_sheets_are_reported(make):
    c = make()
    data = deck([("NOV 16 2026 ", BANNER, {}, [("A5", jpeg_bytes(320))]),
                 ("NOV 17 2026", BANNER.replace("16 NOV", "17 NOV"), {}, [("A5", jpeg_bytes(321)), ("B5", jpeg_bytes(322))])],
                extra_sheets=["Notes"])
    p = preview(c, data, name=DECK_NAME).json()
    assert [(r["serial_number"], r["worksheet_name"], r["inspection_date"]) for r in rows_of(p)] == [
        (1, "NOV 16 2026", "2026-11-16"), (2, "NOV 17 2026", "2026-11-17"), (3, "NOV 17 2026", "2026-11-17")]
    assert any("ignored: Notes" in w for w in p["file_warnings"])


def test_deck_without_photographs_and_damaged_photographs(make):
    c = make()
    r = preview(c, deck([("NOV 16 2026", BANNER, {10: "MAIN DECK"}, [])]), name=DECK_NAME)
    assert r.status_code == 400 and r.json()["code"] == "no_records"
    r = preview(c, corrupt_media(deck()), name=DECK_NAME)             # every picture unreadable
    assert r.status_code == 400 and r.json()["code"] == "no_records" and "5 embedded picture(s) are damaged" in r.json()["detail"]


# ===================================================================== duplicates
def test_production_deck_workbook_is_recognised(data_root, tmp_path):
    import shutil

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app
    from conftest import sign_in

    root = tmp_path / "root"                                          # private copy of the temporary fixture tree
    shutil.copytree(data_root, root)
    data = deck()
    (root / "Week 30" / DECK_NAME).write_bytes(data)
    with TestClient(create_app(Settings(data_root=root, analysis_dir=tmp_path / "a", import_dir=tmp_path / "i"))) as c:
        sign_in(c, "admin")
        r = preview(c, data, name="renamed copy.xlsx")
        assert r.status_code == 409 and r.json()["code"] == "duplicate_workbook" and DECK_NAME in r.json()["detail"]


def test_resaved_deck_matches_existing_records_by_worksheet_and_photo(make):
    c = make()
    r0 = commit(c, deck(), name=DECK_NAME)
    assert r0.status_code == 200, r0.text
    again = deck(extra_sheets=["Notes"])                              # same photographs, different file bytes
    p = preview(c, again, name="Nord Kudu  Deck Photos copy.xlsx").json()
    assert statuses(p).count("duplicate") == 5 and p["can_import"] is False
    assert "same worksheet and photograph" in " ".join(rows_of(p)[0]["notes"])
    r = commit(c, again, name="Nord Kudu  Deck Photos copy.xlsx")
    assert r.status_code == 422 and r.json()["code"] == "nothing_to_import"
    # the same photograph on another week's sheet is a legitimate repeated appearance
    later = deck([("NOV 23 2026", BANNER.replace("16 NOV", "23 NOV"), {}, [("A5", jpeg_bytes(301))])])
    p = preview(c, later, name=DECK_NAME).json()
    assert statuses(p) == ["ready"] and "Repeated appearance" in " ".join(rows_of(p)[0]["notes"])


def test_same_photo_twice_in_one_sheet_is_kept_as_two_appearances(make):
    c = make()
    p = preview(c, deck([("NOV 16 2026", BANNER, {}, [("A5", jpeg_bytes(330)), ("H5", jpeg_bytes(330))])]), name=DECK_NAME).json()
    assert statuses(p) == ["ready", "ready"] and "row 1 of this workbook" in " ".join(rows_of(p)[1]["notes"])


# ===================================================================== Image Register variations
def register(header, rows, *, title_rows=0, sheet="Image Register", photo_col=None, format_row=None) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for i in range(title_rows):
        ws.append([f"Week 43 register (title line {i + 1})"])
    ws.append(header)
    pc = photo_col or "G"
    for i, r in enumerate(rows, start=title_rows + 2):
        ws.append(r["values"])
        for ph in r["photos"]:
            ws.add_image(XLImage(io.BytesIO(ph)), f"{pc}{i}")
    if format_row:
        ws.cell(format_row, 1).fill = PatternFill("solid", fgColor="FFFF00")   # formatting, no value
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


VARIANT = ["Serial No.", "File Name", "Date", "Source Workbook", "Sheet Name", "Location", "Photo", "Activity", "Confidence"]


def test_header_variants_and_title_rows_are_accepted(make):
    c = make()
    p = preview(c, register(VARIANT, GOOD, title_rows=2)).json()
    assert p["source_format"] == "image_register" and p["summary"]["ready"] == 3
    assert [r["row_number"] for r in rows_of(p)] == [4, 5, 6]          # real worksheet rows
    assert rows_of(p)[0]["official_location"] == "CARGO HOLD NO. 1"
    assert any("header row is row 3" in w for w in p["file_warnings"])


def test_reordered_columns_follow_the_photo_column(make):
    c = make()
    order = [6, 0, 1, 2, 3, 4, 5, 7, 8]                                # photograph column first
    rows = [{"values": [r["values"][i] for i in order], "photos": r["photos"]} for r in GOOD]
    p = preview(c, register([HEADER[i] for i in order], rows, photo_col="A")).json()
    assert p["summary"]["ready"] == 3 and {r["image_filename"] for r in rows_of(p)} == {"W43-0001.jpg", "W43-0002.jpg", "W43-0003.jpg"}


def test_duplicate_column_is_rejected_clearly(make):
    c = make()
    r = preview(c, register(HEADER + ["Serial No"], GOOD))
    assert r.status_code == 400 and r.json()["code"] == "duplicate_columns" and "columns A, J" in r.json()["detail"]


def test_missing_required_column_after_aliasing(make):
    c = make()
    r = preview(c, register([h for h in VARIANT if h != "Photo"], GOOD))
    assert r.json()["code"] in ("missing_columns", "missing_worksheet")
    r = preview(c, register([h for h in HEADER if h != "Worksheet Name"], GOOD))
    assert r.json()["code"] == "missing_columns" and "Worksheet Name" in r.json()["detail"]


def test_formatted_empty_rows_do_not_count_as_data(make):
    c = make()
    p = preview(c, register(HEADER, GOOD, format_row=6000))
    assert p.status_code == 200 and p.json()["summary"]["ready"] == 3 and len(p.json()["rows"]) == 3


def test_empty_and_invalid_values_are_reported_per_row(make):
    c = make()
    rows = [row("abc", None, date="not a date", ws="Sheet", photo=jpeg_bytes(340)), row(2, "W43-0002.jpg", photo=None)]
    p = preview(c, register(HEADER, rows)).json()
    a, b = rows_of(p)
    assert a["status"] == b["status"] == "invalid"
    assert any("Serial Number" in e for e in a["errors"]) and any("Image Filename" in e for e in a["errors"])
    assert any("No valid inspection date" in e for e in a["errors"]) and any("No readable photograph" in e for e in b["errors"])
