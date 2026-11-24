"""Phase 6 -- AI analysis history and inspection reports (offline; mock provider only)."""

from __future__ import annotations

import csv
import io
import re
import zlib

import pytest
from fastapi.testclient import TestClient

from app.ai.providers import MOCK_SCENARIOS, MockConditionProvider
from app.config import Settings
from app.main import create_app
from conftest import sign_in

SCENARIO_BY_EARLIER = {"W30-0001": "deteriorated", "W30-0003": "needs_review"}


def _by_image(e, l, p):
    return MOCK_SCENARIOS[SCENARIO_BY_EARLIER.get(e.image_id, "stable")]


@pytest.fixture
def client(data_root, tmp_path):
    settings = Settings(data_root=data_root, analysis_dir=tmp_path / "analyses")
    with TestClient(create_app(settings, condition_provider=MockConditionProvider(_by_image))) as c:
        sign_in(c, "reviewer")
        yield c


def rid(c, image_id):
    return next(r.record_id for r in c.app.state.store.records if r.image_id == image_id and r.source == "json_report")


def analyse(c, earlier, later="W34-0001", **extra):
    r = c.post("/api/ai-analysis/compare", json={"earlier_record_id": rid(c, earlier), "later_record_id": rid(c, later),
                                                    "acknowledge_warnings": True, **extra})
    assert r.status_code == 200, r.text
    return r.json()["analysis"]


@pytest.fixture
def two(client):
    a = analyse(client, "W30-0001", location="Main Deck")   # DETERIORATED
    b = analyse(client, "W30-0003", location="Cargo hold")  # NEEDS_REVIEW
    return a, b


# --------------------------------------------------------------------------- PDF helpers
def pdf_text(data: bytes) -> str:
    """Decompress every FlateDecode stream and return the concatenated text operators."""
    out = []
    for m in re.finditer(rb"/Filter /FlateDecode >>\nstream\n(.*?)\nendstream", data, re.S):
        out.append(zlib.decompress(m.group(1)).decode("latin-1"))
    return "\n".join(out)


def assert_valid_pdf(data: bytes) -> None:
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    xref = int(re.search(rb"startxref\n(\d+)\n%%EOF", data).group(1))
    assert data[xref:xref + 4] == b"xref"
    count = int(re.search(rb"xref\n0 (\d+)\n", data[xref:]).group(1))
    offsets = re.findall(rb"(\d{10}) 00000 n ", data[xref:])
    assert len(offsets) == count - 1
    for i, off in enumerate(offsets, start=1):
        assert data[int(off):].startswith(f"{i} 0 obj".encode())


# --------------------------------------------------------------------------- AI history
def test_empty_history(client):
    h = client.get("/api/ai-analysis/history").json()
    assert h == {"total": 0, "count": 0, "offset": 0, "limit": 50, "items": []}


def test_history_lists_filters_sorts_and_pages(client, two):
    a, b = two
    h = client.get("/api/ai-analysis/history").json()
    assert h["total"] == 2 and [i["analysis_id"] for i in h["items"]] == [b["analysis_id"], a["analysis_id"]]
    get = lambda **p: client.get("/api/ai-analysis/history", params=p).json()  # noqa: E731
    assert [i["analysis_id"] for i in get(classification="DETERIORATED")["items"]] == [a["analysis_id"]]
    assert get(classification="NEEDS_REVIEW")["total"] == 1
    sign_in(client, "reviewer")  # SV-03: a different reviewer approves (no self-approval)
    client.post(f"/api/ai-analysis/{a['analysis_id']}/review", json={"status": "APPROVED", "reviewer": "Inspector A"})
    assert [i["analysis_id"] for i in get(review_status="APPROVED")["items"]] == [a["analysis_id"]]
    assert get(review_status="PENDING_REVIEW")["total"] == 1
    # pagination
    p1, p2 = get(limit=1, offset=0), get(limit=1, offset=1)
    assert p1["count"] == p2["count"] == 1 and p1["total"] == 2
    assert p1["items"][0]["analysis_id"] != p2["items"][0]["analysis_id"]
    # sorting
    assert [i["analysis_id"] for i in get(sort="analysed_asc")["items"]] == [a["analysis_id"], b["analysis_id"]]
    assert get(sort="classification")["items"][0]["classification"] == "DETERIORATED"
    assert get(sort="review_status")["items"][0]["review"]["status"] == "PENDING_REVIEW"
    # search and dates
    assert get(q=a["analysis_id"].lower())["total"] == 1
    assert get(q="w30-0003")["total"] == 1
    assert get(q="cargo  HOLD")["total"] == 1
    assert get(date_from="2026-09-01", date_to="2026-09-30")["total"] == 2   # later date in range
    assert get(date_from="2026-08-02", date_to="2026-08-02")["total"] == 2   # earlier date in range
    assert get(date_from="2026-10-01")["total"] == 0
    assert client.get("/api/ai-analysis/history", params={"date_from": "2026-09-30", "date_to": "2026-09-01"}).status_code == 400
    assert client.get("/api/ai-analysis/history", params={"sort": "random"}).status_code == 422


def test_analysis_detail(client, two):
    a, _ = two
    d = client.get(f"/api/ai-analysis/{a['analysis_id']}").json()
    assert d["earlier"]["image_id"] == "W30-0001" and d["simulated"] is True
    assert d["ai_result"]["deterioration_evidence"]


# --------------------------------------------------------------------------- reports
def test_preview_summary(client):
    p = client.get("/api/reports/preview").json()
    s = p["summary"]
    store = client.app.state.store
    assert s["appearances"] == len(store.records)
    assert s["missing_photographs"] == sum(1 for r in store.records if r.image_status != "resolved")
    assert s["unique_photographs"] < s["appearances"]  # repeated photo counted once
    assert s["analyses"] == 0 and p["analyses"] == []


def test_csv_report(client):
    r = client.get("/api/reports/inspections.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert re.match(r'attachment; filename="synergy-inspections_[a-z0-9-]+_\d{8}-\d{4}\.csv"', r.headers["content-disposition"])
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    header, body = rows[0], rows[1:]
    assert "Official Location" in header and "AI suggested location (unverified)" in header
    assert len(body) == len(client.app.state.store.records)
    ids = [row[0] for row in body]
    assert len(ids) == len(set(ids))  # no duplicate rows
    ref = header.index("Photograph reference")
    assert "Unavailable" in [row[ref] for row in body]  # missing photo labelled, not invented
    text = r.content.decode("utf-8-sig")
    assert "E:\\" not in text and "Extracted Images" not in text  # no internal paths


def test_csv_neutralises_formula_injection():
    from app.reports.render import _cell

    assert _cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _cell("-1") == "'-1" and _cell("Main Deck") == "Main Deck"


def test_date_and_location_filtering(client):
    get = lambda **p: client.get("/api/reports/preview", params=p).json()["summary"]  # noqa: E731
    all_ = get()["appearances"]
    aug = get(date_from="2026-08-01", date_to="2026-08-31")
    assert 0 < aug["appearances"] < all_ and aug["inspection_dates"] == 1
    assert get(date_from="2026-09-06")["inspection_dates"] == 1
    loc = get(location="cargo hold")
    assert loc["appearances"] >= 1 and set(loc["locations"]) <= {"CARGO HOLD NO. 1"}
    assert get(location="  main   deck ")["appearances"] >= 2  # official + AI location, whitespace tolerant
    assert get(week="Week 30")["weeks"] == ["Week 30"]
    rows = list(csv.reader(io.StringIO(client.get("/api/reports/inspections.csv", params={"date_to": "2026-08-31"}).content.decode("utf-8-sig"))))
    assert {row[2] for row in rows[1:]} == {"2026-08-02"}


def test_invalid_filters(client):
    bad = [({"date_from": "2026-09-30", "date_to": "2026-09-01"}, 400, "invalid_date_range"),
           ({"vessel": "no-such-vessel"}, 404, "unknown_vessel"),
           ({"week": "Week 99"}, 400, "unknown_week")]
    for params, status, code in bad:
        r = client.get("/api/reports/preview", params=params)
        assert r.status_code == status and r.json()["code"] == code
    assert client.get("/api/reports/preview", params={"date_from": "06/09/2026"}).status_code == 422
    assert client.get("/api/reports/preview", params={"classification": "WORSE"}).status_code == 422
    assert client.get("/api/reports/preview", params={"location": "x" * 201}).status_code == 422


def test_empty_reports(client):
    p = {"location": "no such location anywhere"}
    assert client.get("/api/reports/preview", params=p).json()["summary"]["appearances"] == 0
    for url in ("/api/reports/inspections.csv", "/api/reports/inspection.pdf"):
        r = client.get(url, params=p)
        assert r.status_code == 422 and r.json()["code"] == "empty_report"
    r = client.get("/api/reports/analyses.csv")
    assert r.status_code == 422 and r.json()["code"] == "no_analyses"


def test_pdf_without_analyses(client):
    r = client.get("/api/reports/inspection.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert_valid_pdf(r.content)
    t = pdf_text(r.content)
    for s in ("Vessel Inspection Report", "B. Inspection Summary", "C. Location Details", "D. Historical Comparison",
              "No AI condition assessment is available for this selection.", "Unavailable", "Not labelled"):
        assert s in t, s
    assert "SIMULATED" not in t and "/DCTDecode" not in r.content.decode("latin-1")  # nothing invented
    assert "E:\\\\" not in t


def _real_jpegs(client, tmp_path, monkeypatch):
    """The synthetic fixture photos are placeholder bytes; serve small real JPEGs instead (tmp only)."""
    from PIL import Image

    store = client.app.state.store
    paths = {}
    for i, image_id in enumerate(("W30-0001", "W30-0003", "W34-0001")):
        p = tmp_path / f"{image_id}.jpg"
        Image.new("RGB", (64, 48), (40 * i, 90, 160)).save(p, format="JPEG")
        paths[image_id] = p
    monkeypatch.setattr(store.images, "resolve", lambda image_id: (paths[image_id], "image/jpeg"))


def test_pdf_includes_saved_analyses_with_labels(client, two, tmp_path, monkeypatch):
    a, b = two
    _real_jpegs(client, tmp_path, monkeypatch)
    sign_in(client, "reviewer")  # SV-03: a different reviewer approves (no self-approval)
    client.post(f"/api/ai-analysis/{a['analysis_id']}/review", json={"status": "APPROVED", "reviewer": "Inspector A"})
    r = client.get("/api/reports/inspection.pdf")
    assert_valid_pdf(r.content)
    t = pdf_text(r.content)
    assert a["analysis_id"] in t and b["analysis_id"] in t
    assert "SIMULATED - Simulated result" in t  # simulated never presented as genuine
    assert "HUMAN-APPROVED" not in t           # approval of a simulated result does not make it a finding
    assert "Classification: Deteriorated" in t and "Classification: Needs Review" in t
    assert f"Approved by {client.get('/api/auth/me').json()['display_name']}" in t
    # earlier+later photographs embedded once each (W34-0001 shared by both pairs -> deduplicated)
    assert r.content.count(b"/Subtype /Image") == 3


def test_pdf_classification_filter_and_review_labels(client, two, monkeypatch):
    a, b = two
    t = pdf_text(client.get("/api/reports/inspection.pdf", params={"classification": "NEEDS_REVIEW"}).content)
    assert b["analysis_id"] in t and a["analysis_id"] not in t
    # a non-simulated, approved analysis is labelled human-approved; pending ones are unverified
    from app.reports.builder import assessment_status

    rec = client.app.state.analysis.store.get(a["analysis_id"]).model_copy(update={"simulated": False})
    assert assessment_status(rec)[0] == "UNVERIFIED"
    rec.review.status = rec.review.status.__class__("APPROVED")
    assert assessment_status(rec)[0] == "HUMAN-APPROVED"
    rec.review.status = rec.review.status.__class__("REJECTED")
    assert assessment_status(rec)[0] == "REJECTED"


def test_analyses_csv(client, two):
    r = client.get("/api/reports/analyses.csv")
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert {row["Classification"] for row in rows} == {"DETERIORATED", "NEEDS_REVIEW"}
    assert all(row["Assessment status"] == "SIMULATED" and row["Simulated"] == "yes" for row in rows)
    assert all(row["Review status"] == "PENDING_REVIEW" for row in rows)


def test_missing_photograph_in_pdf_comparison(client, two, monkeypatch):
    # if a saved pair's photo can no longer be read, the PDF shows a placeholder instead of failing
    store = client.app.state.store
    monkeypatch.setattr(store.images, "resolve", lambda image_id: (_ for _ in ()).throw(OSError("gone")))
    r = client.get("/api/reports/inspection.pdf")
    assert r.status_code == 200 and "Photograph unavailable" in pdf_text(r.content)


def test_reports_do_not_modify_data(client, two, data_root):
    snap = lambda: {p: p.read_bytes() for p in data_root.rglob("*") if p.is_file() and "_analyses" not in p.parts}  # noqa: E731
    before = snap()
    for url in ("/api/reports/inspections.csv", "/api/reports/analyses.csv", "/api/reports/inspection.pdf", "/api/reports/preview"):
        assert client.get(url).status_code == 200
    assert snap() == before
