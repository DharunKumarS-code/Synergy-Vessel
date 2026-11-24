"""Phase 3: combined Official/AI location search with match-source reporting."""

from app.services.location_service import matched_fields

SEARCH = "/api/inspections/search"


def _search(client, location, **params):
    return client.get(SEARCH, params={"location": location, **params}).json()


def test_official_only_by_default_reports_matched_on(client):
    body = _search(client, "Main Deck")
    assert body["count"] == 3
    assert all(i["matched_on"] == ["official_location"] for i in body["items"])


def test_ai_location_matches_are_labelled_as_ai(client):
    body = _search(client, "main deck", include_ai_location="true")
    by_id = {i["record_id"]: i for i in body["items"]}
    assert body["count"] == 4
    assert by_id["W30-0001"]["matched_on"] == ["ai_suggested_location"]
    assert by_id["W30-0001"]["official_location"] == "Not Labelled"  # never overwritten
    assert by_id["W30-0002"]["matched_on"] == ["official_location"]


def test_partial_case_insensitive_whitespace_tolerant(client):
    body = _search(client, "  MAIN   deck ", match="contains", include_ai_location="true")
    assert body["count"] == 4
    body = _search(client, "cargo HOLD", match="contains")
    assert [i["record_id"] for i in body["items"]] == ["W30-0003"]


def test_placeholder_never_a_partial_match(client):
    assert _search(client, "label", match="contains", include_ai_location="true")["count"] == 0
    assert _search(client, "Not Labelled")["count"] == 1  # exact still finds it


def test_duplicate_photo_appearances_kept_as_distinct_records(client):
    body = _search(client, "main deck", match="contains")
    ids = [i["record_id"] for i in body["items"]]
    assert len(ids) == len(set(ids)) == 3
    shared = [i for i in body["items"] if i["same_photo_appearances"] == 2]
    assert {i["record_id"] for i in shared} == {"W30-0002", "W34-0001"}


def test_record_matching_both_fields(store):
    rec = store.by_id["W30-0002"].model_copy(update={"ai_suggested_location": "Main Deck Fwd"})
    assert matched_fields(rec, "main deck", match="contains", include_ai_location=True) == [
        "official_location", "ai_suggested_location"]
    assert matched_fields(rec, "main deck", match="exact", include_ai_location=True) == ["official_location"]
    assert matched_fields(rec, "main deck", match="contains", include_ai_location=False) == ["official_location"]


def test_search_items_keep_full_record_shape(client):
    item = _search(client, "CARGO HOLD NO. 1")["items"][0]
    for key in ("image_id", "image_url", "inspection_date", "date_source", "week", "week_folder",
                "official_location", "ai_suggested_location", "location_confidence", "image_status"):
        assert key in item
