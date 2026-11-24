"""Phase 7 -- Excel register import. Synthetic workbooks only; every import store is a tmp folder."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient
from openpyxl.drawing.image import Image as XLImage

from app.config import Settings
from app.imports import service as import_service
from app.main import create_app
from conftest import sign_in, XLSX_A, jpeg_bytes

HEADER = ["Serial Number", "Image Filename", "Report Date", "Original Excel Filename", "Worksheet Name",
          "Official Location", "Extracted Photograph", "Activity Identified", "Location Confidence"]
XLSX_NEW = "Nord Kudu  Deck Photos -09 AUG 2026 Part 1 (1).xlsx"


def row(serial, fname, date="8/9/2026", ws="AUG 09 2026", loc="CARGO HOLD NO. 1", excel=XLSX_NEW, photo=None):
    return {"values": [serial, fname, date, excel, ws, loc, None, "No activity observed", None],
            "photos": [] if photo is None else (photo if isinstance(photo, list) else [photo])}


def workbook(rows, header=HEADER, sheet="Image Register") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for i, r in enumerate(rows, start=2):
        ws.append(r["values"])
        for p in r["photos"]:
            ws.add_image(XLImage(io.BytesIO(p)), f"G{i}")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def corrupt_media(data: bytes) -> bytes:
    src, out = zipfile.ZipFile(io.BytesIO(data)), io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            blob = src.read(item.filename)
            dst.writestr(item, b"not an image at all" if item.filename.startswith("xl/media/") else blob)
    return out.getvalue()


@pytest.fixture
def make(data_root, tmp_path):
    clients = []

    def _make():
        s = Settings(data_root=data_root, analysis_dir=tmp_path / "analyses", import_dir=tmp_path / "imports")
        c = TestClient(create_app(s))
        c.__enter__()
        clients.append(c)
        sign_in(c, "admin")
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def preview(c, data, name="Week_43_Image_Register.xlsx"):
    return c.post("/api/imports/preview", params={"filename": name}, content=data,
                  headers={"Content-Type": "application/octet-stream"})


def commit(c, data, name="Week_43_Image_Register.xlsx", fingerprint=None):
    return c.post("/api/imports", params={"filename": name, "fingerprint": fingerprint or hashlib.sha256(data).hexdigest()},
                  content=data, headers={"Content-Type": "application/octet-stream"})


def statuses(p):
    return [r["status"] for r in p["rows"]]


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.relative_to(root).parts[0].startswith("_")}


GOOD = [row(1, "W43-0001.jpg", photo=jpeg_bytes(101)),
        row(2, "W43-0002.jpg", loc=None, photo=jpeg_bytes(102)),
        row(3, "W43-0003.jpg", loc="Main Deck", photo=jpeg_bytes(103))]


# --------------------------------------------------------------------------- valid workbook
def test_preview_validates_and_writes_nothing(make, tmp_path):
    c = make()
    data = workbook(GOOD)
    r = preview(c, data)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["fingerprint"] == hashlib.sha256(data).hexdigest() and p["can_import"] is True
    assert p["summary"]["ready"] == 3 and p["summary"]["failed"] == 0 and p["summary"]["skipped"] == 0
    first = p["rows"][0]
    assert first["inspection_date"] == "2026-08-09" and first["date_source"] == "report_date"
    assert first["vessel_name"] == "Nord Kudu" and first["week_number"] == 43
    assert any("not labelled" in n for n in p["rows"][1]["notes"])
    assert not (tmp_path / "imports").exists()


def test_commit_is_atomic_and_records_become_available(make, tmp_path, data_root):
    c = make()
    before = tree(data_root)
    data = workbook(GOOD)
    r = commit(c, data)
    assert r.status_code == 200, r.text
    res = r.json()
    iid = res["import_id"]
    assert res["summary"]["imported"] == 3 and res["status"] == "completed"
    folder = tmp_path / "imports" / iid
    assert (folder / "source.xlsx").read_bytes() == data  # byte-exact copy of the upload
    assert sorted(p.name for p in (folder / "images").iterdir()) == sorted(
        f"{hashlib.sha256(jpeg_bytes(n)).hexdigest()}.jpg" for n in (101, 102, 103))
    assert not [p for p in (tmp_path / "imports").iterdir() if p.name.startswith(".staging-")]

    recs = [r for r in c.app.state.store.records if r.source == "excel_import"]
    assert len(recs) == 3 and all(r.import_id == iid for r in recs)
    rec = next(r for r in recs if r.serial_number == 3)
    assert rec.official_location == "Main Deck" and rec.ai_suggested_location is None  # kept separate, not invented
    assert rec.week == "Week 43 (imported)" and rec.inspection_date.isoformat() == "2026-08-09"
    img = c.get(rec.image_url)
    assert img.status_code == 200 and img.content == jpeg_bytes(103)
    unlabelled = next(r for r in recs if r.serial_number == 2)
    assert unlabelled.official_location_labelled is False and unlabelled.official_location is None
    # visible through the existing APIs (Phases 2-6)
    assert any(i["record_id"] == rec.record_id for i in c.get("/api/inspections", params={"limit": 2000}).json()["items"])
    assert c.get("/api/search", params={"location": "main deck"}).status_code in (200, 404)
    assert tree(data_root) == before  # production data untouched


def test_history_and_detail(make):
    c = make()
    iid = commit(c, workbook(GOOD)).json()["import_id"]
    h = c.get("/api/imports").json()
    assert [i["import_id"] for i in h["imports"]] == [iid]
    item = h["imports"][0]
    assert item["filename"] == "Week_43_Image_Register.xlsx" and item["summary"]["imported"] == 3
    assert "rows" not in item
    d = c.get(f"/api/imports/{iid}").json()
    assert len(d["rows"]) == 3 and all(r["record_id"] for r in d["rows"])
    assert c.get("/api/imports/IMP-20260101-0123456789").status_code == 404
    assert c.get("/api/imports/../../etc").status_code in (400, 404)
    assert c.get("/api/imports/not-an-id").json()["code"] == "invalid_import_id"


# --------------------------------------------------------------------------- duplicates vs repeated appearances
def test_same_workbook_twice_is_blocked(make):
    c = make()
    data = workbook(GOOD)
    assert commit(c, data).status_code == 200
    for r in (preview(c, data), commit(c, data)):
        assert r.status_code == 409 and r.json()["code"] == "duplicate_workbook"
    assert len(c.get("/api/imports").json()["imports"]) == 1


def test_existing_production_register_is_blocked(make, data_root):
    c = make()
    data = (data_root / "Week 30" / "Week_30_Image_Register.xlsx").read_bytes()
    r = preview(c, data, "Week_30_Image_Register.xlsx")
    assert r.status_code == 409 and r.json()["code"] == "duplicate_workbook" and "already in the dataset" in r.json()["guidance"]


def test_duplicate_records_are_skipped_but_repeated_appearances_kept(make):
    c = make()
    existing = row(2, "W30-0002.jpg", date="8/2/2026", ws="AUG 02 2026", loc="Main Deck", excel=XLSX_A, photo=jpeg_bytes(2))
    reused = row(5, "W43-0005.jpg", photo=jpeg_bytes(2))         # same photo, new inspection -> repeated appearance
    first = row(6, "W43-0006.jpg", photo=jpeg_bytes(104))
    exact_copy = row(6, "W43-0006.jpg", photo=jpeg_bytes(104))   # same row twice in the file -> duplicate
    second_use = row(7, "W43-0007.jpg", loc="Hatch", photo=jpeg_bytes(104))  # same photo, other row -> kept
    p = preview(c, workbook([existing, reused, first, exact_copy, second_use])).json()
    assert statuses(p) == ["duplicate", "ready", "ready", "duplicate", "ready"]
    assert "Already in the dataset" in p["rows"][0]["notes"][0]
    assert any(n.startswith("Repeated appearance") for n in p["rows"][1]["notes"])
    assert "Repeats row 4" in p["rows"][3]["notes"][0]
    assert any("row 4 of this workbook" in n for n in p["rows"][4]["notes"])
    assert p["summary"]["duplicates"] == 2 and p["summary"]["repeated_appearances"] == 2
    assert p["summary"]["unique_photographs"] == 2


def test_rows_already_imported_by_another_workbook_are_duplicates(make):
    c = make()
    assert commit(c, workbook(GOOD)).status_code == 200
    again = workbook(GOOD + [row(9, "W43-0009.jpg", photo=jpeg_bytes(109))])  # different file, overlapping rows
    p = preview(c, again).json()
    assert statuses(p) == ["duplicate", "duplicate", "duplicate", "ready"]
    r = commit(c, again)
    assert r.status_code == 200 and r.json()["summary"]["imported"] == 1 and r.json()["summary"]["duplicates"] == 3
    assert len([x for x in c.app.state.store.records if x.source == "excel_import"]) == 4


def test_all_duplicates_means_nothing_to_import(make):
    c = make()
    assert commit(c, workbook(GOOD)).status_code == 200
    dup_only = workbook(GOOD + [row(None, None)])  # a blank row makes the bytes differ
    r = commit(c, dup_only)
    assert r.status_code == 422 and r.json()["code"] == "nothing_to_import"
    h = c.get("/api/imports").json()
    assert len(h["imports"]) == 1 and h["failed_attempts"][0]["code"] == "nothing_to_import"


# --------------------------------------------------------------------------- file-level validation
@pytest.mark.parametrize("data,name,code", [
    (b"", "a.xlsx", "empty_file"),
    (b"PK\x03\x04 broken zip", "a.xlsx", "malformed_workbook"),
    (b"serial,image\n1,x.jpg", "a.xlsx", "malformed_workbook"),
    (b"anything", "register.csv", "invalid_file_type"),
])
def test_malformed_files_are_rejected(make, data, name, code):
    c = make()
    r = preview(c, data, name)
    assert r.status_code == 400 and r.json()["code"] == code and r.json()["guidance"]


def test_missing_columns_and_wrong_sheet(make):
    c = make()
    r = preview(c, workbook(GOOD, header=[h for h in HEADER if h not in ("Report Date", "Worksheet Name")]))
    assert r.json()["code"] == "missing_columns" and "Report Date, Worksheet Name" in r.json()["detail"]
    # Phase 8.5: a register whose sheet was renamed is still recognised by its header row (with a warning) ...
    r = preview(c, workbook(GOOD, sheet="Sheet1"))
    assert r.status_code == 200 and "read from worksheet 'Sheet1'" in r.json()["file_warnings"][0]
    # ... but a sheet that is neither a register nor Deck Photos is rejected, naming what was found
    r = preview(c, workbook(GOOD, header=["Notes", "Other"], sheet="Sheet1"))
    assert r.json()["code"] == "missing_worksheet" and "Sheet1" in r.json()["guidance"]
    r = preview(c, workbook([]))
    assert r.json()["code"] == "no_records"


def test_header_matching_tolerates_case_and_spacing(make):
    c = make()
    p = preview(c, workbook(GOOD, header=[h.upper().replace(" ", "  ") for h in HEADER])).json()
    assert p["summary"]["ready"] == 3


def test_upload_size_limit(make, monkeypatch):
    from app.routes import imports as routes

    monkeypatch.setattr(routes, "MAX_UPLOAD_BYTES", 100)
    c = make()
    r = preview(c, workbook(GOOD))
    assert r.status_code == 413 and r.json()["code"] == "file_too_large"


# --------------------------------------------------------------------------- row-level validation
def test_invalid_dates_missing_values_and_images(make):
    c = make()
    rows = [
        row(1, "W43-0001.jpg", date="31/31/2026", ws="NOT A DATE", photo=jpeg_bytes(111)),   # no valid date
        row(2, "W43-0002.jpg", date="AMBIGUOUS/PENDING", photo=jpeg_bytes(112)),             # worksheet date used
        row(3, "W43-0003.jpg"),                                                                # missing image
        row(None, "W43-0004.jpg", photo=jpeg_bytes(114)),                                      # missing serial
        row(5, None, photo=jpeg_bytes(115)),                                                   # missing filename
        row(6, "W43-0006.jpg", photo=[jpeg_bytes(116), jpeg_bytes(117)]),                     # two photos in one row
        row(None, None, date=None, ws=None, loc=None, excel=None),                             # blank row
        row(8, "W43-0008.jpg", excel="whatever.xlsx", photo=jpeg_bytes(118)),                 # unknown vessel
    ]
    rows[6]["values"][7] = None
    p = preview(c, workbook(rows)).json()
    assert statuses(p) == ["invalid", "ready", "invalid", "invalid", "invalid", "invalid", "skipped", "ready"]
    r = p["rows"]
    assert "No valid inspection date" in r[0]["errors"][0]
    assert r[1]["date_source"] == "worksheet_name" and r[1]["inspection_date"] == "2026-08-09" and r[1]["warnings"]
    assert "missing image" in r[2]["errors"][0]
    assert "Serial Number" in r[3]["errors"][0] and "Image Filename" in r[4]["errors"][0]
    assert "expected exactly one" in r[5]["errors"][0]
    assert r[7]["vessel_name"] == "Unknown vessel" and r[7]["warnings"]
    assert p["summary"] == {**p["summary"], "ready": 2, "failed": 5, "blank_rows": 1, "skipped": 1, "records": 7}
    res = commit(c, workbook(rows)).json()
    assert res["summary"]["imported"] == 2 and res["summary"]["failed"] == 5
    assert [x["record_id"] is not None for x in res["rows"]] == [False, True, False, False, False, False, False, True]


def test_corrupted_embedded_photograph(make):
    c = make()
    p = preview(c, corrupt_media(workbook([row(1, "W43-0001.jpg", photo=jpeg_bytes(120))]))).json()
    assert statuses(p) == ["invalid"] and "damaged" in p["rows"][0]["errors"][0]
    assert "damaged" in p["file_warnings"][0]


# --------------------------------------------------------------------------- failure, interruption, recovery
def test_failed_write_leaves_no_partial_import(make, tmp_path, monkeypatch):
    c = make()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(import_service.os, "rename", boom)
    r = commit(c, workbook(GOOD))
    assert r.status_code == 500 and r.json()["code"] == "import_failed" and "Nothing was imported" in r.json()["detail"]
    root = tmp_path / "imports"
    assert not [p for p in root.iterdir() if p.name.startswith(("IMP-", ".staging-"))]
    assert not [x for x in c.app.state.store.records if x.source == "excel_import"]
    h = c.get("/api/imports").json()
    assert h["imports"] == [] and h["failed_attempts"][0]["code"] == "import_failed"


def test_preview_mismatch_is_refused(make, tmp_path):
    c = make()
    r = commit(c, workbook(GOOD), fingerprint="a" * 64)
    assert r.status_code == 409 and r.json()["code"] == "preview_mismatch"
    assert not (tmp_path / "imports").exists() or not list((tmp_path / "imports").glob("IMP-*"))


def test_interrupted_staging_is_ignored_reported_and_cleaned(make, tmp_path):
    root = tmp_path / "imports"
    stale = root / ".staging-IMP-20261118-aaaaaaaaaa"
    (stale / "images").mkdir(parents=True)
    (stale / "source.xlsx").write_bytes(b"partial")
    c = make()
    assert not [x for x in c.app.state.store.records if x.source == "excel_import"]
    assert c.get("/api/imports").json()["interrupted_imports"] == 1
    assert commit(c, workbook(GOOD)).status_code == 200
    assert not stale.exists() and c.get("/api/imports").json()["interrupted_imports"] == 0


def test_corrupt_manifest_is_ignored_safely(make, tmp_path):
    c = make()
    iid = commit(c, workbook(GOOD)).json()["import_id"]
    (tmp_path / "imports" / iid / "manifest.json").write_text("{not json", encoding="utf-8")
    c2 = make()  # restart
    assert not [x for x in c2.app.state.store.records if x.source == "excel_import"]
    h = c2.get("/api/imports").json()
    assert h["imports"] == [] and any(iid in p for p in h["problems"])
    assert c2.get("/api/health").status_code == 200


def test_imports_survive_restart_and_work_with_pair_validation(make):
    c = make()
    iid = commit(c, workbook(GOOD)).json()["import_id"]
    c2 = make()
    recs = [r for r in c2.app.state.store.records if r.import_id == iid]
    assert len(recs) == 3
    v = c2.get("/api/ai-analysis/validate", params={"earlier_record_id": "W30-0003", "later_record_id": recs[0].record_id}).json()
    assert v["earlier"]["readable"] and v["later"]["readable"] and v["later"]["sha256_matches_record"] is True
    manifest = json.loads((Path(c2.app.state.imports.dir) / iid / "manifest.json").read_text())
    assert manifest["schema_version"] == 1 and manifest["summary"]["imported"] == 3
