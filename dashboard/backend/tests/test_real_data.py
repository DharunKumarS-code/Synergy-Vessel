"""Read-only checks against the real Synergy Vessel data (skipped if absent).

Expected figures come from the completed extraction work (2026-11-16/17).
"""

from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_DATA_ROOT, Settings
from app.main import create_app
from conftest import sign_in

REAL = DEFAULT_DATA_ROOT
pytestmark = pytest.mark.skipif(
    not (REAL / "Week 30" / "Final Official vs AI Location Report.json").is_file(),
    reason="real data not present",
)


@pytest.fixture(scope="module")
def real_client():
    with TestClient(create_app(Settings(data_root=REAL))) as c:
        sign_in(c, "admin")   # SV-02: an isolated test account (temporary auth folder; never the real one)
        yield c


def test_all_weeks_integrated(real_client):
    rep = real_client.get("/api/validation").json()
    t = rep["totals"]
    assert t["weeks_discovered"] == 11 and t["weeks_readable"] == 11
    assert t["json_records"] == 1058
    assert t["register_only_appearances"] == 20
    assert t["total_appearances"] == 1078
    assert t["unique_photographs"] == 609
    assert t["resolved_images"] == 1078
    assert t["missing_images"] == 0 and t["ambiguous_images"] == 0 and t["invalid_records"] == 0
    assert t["official_location_labelled"] == 1012 and t["official_location_unlabelled"] == 66
    assert t["dated_records"] == 1078
    assert rep["unlinked_image_files"] == [] and rep["issues"] == []
    for w in rep["weeks"]:
        assert w["json_records"] == w["declared_record_count"]


def test_every_image_id_unique_and_served(real_client):
    items = real_client.get("/api/inspections?limit=2000").json()["items"]
    ids = [i["image_id"] for i in items]
    assert len(ids) == 1078 and len(set(ids)) == 1078
    r = real_client.get(items[0]["image_url"])
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"


def test_week34_dated_from_worksheet(real_client):
    items = real_client.get("/api/inspections?week=week 34&limit=200").json()["items"]
    assert len(items) == 98
    assert {i["inspection_date"] for i in items} == {"2026-08-30"}
    assert {i["date_source"] for i in items} == {"worksheet_name"}


def test_location_dates_span_weeks(real_client):
    body = real_client.get("/api/inspections/dates", params={"location": "CARGO HOLD NO. 1"}).json()
    assert len(body["dates"]) == 11
    counts = Counter(i["date"] for i in body["dates"])
    assert all(v == 1 for v in counts.values())
