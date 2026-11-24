"""Endpoint behaviour against the synthetic data root."""

import pytest

from conftest import sign_in


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["records"] == 6 and body["weeks_loaded"] == 2


def test_validation_endpoint(client):
    assert client.get("/api/validation").status_code == 403        # SV-02: administrators only
    sign_in(client, "admin")
    try:
        body = client.get("/api/validation").json()
    finally:
        client.headers.pop("Authorization")                     # the next test starts as a viewer again
    assert body["totals"]["invalid_records"] == 1
    assert len(body["missing_images"]) == 1
    assert "data_root" not in body                               # SV-02: no absolute server path


def test_vessels(client):
    v = client.get("/api/vessels").json()
    assert [x["vessel_id"] for x in v] == ["nord-kudu"]
    assert v[0]["record_count"] == 6
    assert v[0]["first_inspection_date"] == "2026-08-02"


def test_locations_case_insensitive_grouping_and_placeholder(client):
    locs = {l["location"]: l for l in client.get("/api/locations").json()}
    main = locs["Main Deck"]
    assert main["record_count"] == 3 and sorted(main["variants"]) == ["Main Deck", "main deck"]
    assert locs["Not Labelled"]["is_placeholder"] is True
    names = [l["location"] for l in client.get("/api/locations?include_placeholder=false").json()]
    assert "Not Labelled" not in names


def test_inspections_list_and_pagination(client):
    body = client.get("/api/inspections?limit=2&offset=1").json()
    assert body["total"] == 6 and body["count"] == 2 and body["offset"] == 1
    assert client.get("/api/inspections?week=week 34").json()["total"] == 2
    assert client.get("/api/inspections?date=2026-09-06").json()["total"] == 2


def test_search_official_only_by_default(client):
    body = client.get("/api/inspections/search", params={"location": "main deck"}).json()
    assert body["count"] == 3
    assert all(i["official_location"].lower() == "main deck" for i in body["items"])


def test_search_with_ai_location_is_supplementary(client):
    body = client.get("/api/inspections/search",
                      params={"location": "Main Deck", "include_ai_location": "true"}).json()
    assert body["count"] == 4
    ai_hit = next(i for i in body["items"] if i["record_id"] == "W30-0001")
    assert ai_hit["official_location"] == "Not Labelled"


def test_search_contains_and_no_results(client):
    assert client.get("/api/inspections/search", params={"location": "cargo", "match": "contains"}).json()["count"] == 1
    assert client.get("/api/inspections/search", params={"location": "Nowhere"}).json()["count"] == 0


def test_dates_for_location(client):
    body = client.get("/api/inspections/dates", params={"location": "Main Deck"}).json()
    assert [d["date"] for d in body["dates"]] == ["2026-08-02", "2026-09-06"]
    sept = body["dates"][1]
    assert sept["record_count"] == 2 and sept["resolved_image_count"] == 1


def test_images_for_location_and_date(client):
    body = client.get("/api/inspections/images", params={"location": "Main Deck", "date": "2026-08-02"}).json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["image_id"] == "W30-0002" and item["image_url"] == "/api/images/W30-0002"
    empty = client.get("/api/inspections/images", params={"location": "Main Deck", "date": "2026-01-01"}).json()
    assert empty["count"] == 0


def test_record_by_id(client):
    assert client.get("/api/inspections/W30-0003").json()["official_location"] == "CARGO HOLD NO. 1"
    assert client.get("/api/inspections/nope").status_code == 404


def test_image_served_byte_identical(client, data_root):
    r = client.get("/api/images/W30-0002")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == (data_root / "Week 30" / "Extracted Images - New" / "W30-0002.jpg").read_bytes()


def test_image_unknown_id_404(client):
    r = client.get("/api/images/W99-9999")
    assert r.status_code == 404


@pytest.mark.parametrize("path,codes", [
    ("/api/images/..%2Fsecret.txt", {400, 404}),
    ("/api/images/..%2F..%2Fsecret.txt", {400, 404}),
    ("/api/images/%2e%2e", {400, 404}),
    ("/api/images/..", {400, 404}),
    ("/api/images/W30-0001.jpg", {400}),
    ("/api/images/C:%5CWindows%5Cwin.ini", {400}),
    ("/api/images/..%5Csecret.txt", {400}),
])
def test_image_traversal_blocked(client, path, codes):
    r = client.get(path)
    assert r.status_code in codes
    assert b"outside approved folders" not in r.content


@pytest.mark.parametrize("url,code", [
    ("/api/inspections/search", 422),
    ("/api/inspections/search?location=", 422),
    ("/api/inspections/search?location=%20%20", 422),
    ("/api/inspections/search?location=x&match=regex", 422),
    ("/api/inspections/dates", 422),
    ("/api/inspections/dates?location=Nowhere", 404),
    ("/api/inspections/images?location=Main%20Deck", 422),
    ("/api/inspections/images?location=Main%20Deck&date=02-08-2026", 422),
    ("/api/inspections/images?location=Nowhere&date=2026-08-02", 404),
    ("/api/inspections?limit=0", 422),
    ("/api/inspections?vessel=unknown", 404),
    ("/api/locations?vessel=unknown", 404),
])
def test_invalid_requests(client, url, code):
    assert client.get(url).status_code == code


def test_only_get_allowed(client):
    assert client.post("/api/health").status_code == 405
    assert client.delete("/api/images/W30-0001").status_code == 405
