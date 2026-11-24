"""Phase 10.1 -- read-only location review queue (GET /api/locations/review). Synthetic imports in temp folders only."""

from __future__ import annotations

from app.imports.service import review_group_key
from app.location_intel.providers import MockLocationProvider
from app.location_intel.service import LocationIntelligence
from conftest import jpeg_bytes, sign_in
from test_deck_photos_import import BANNER, DECK_NAME, deck
from test_imports import commit, make, tree, workbook  # noqa: F401
from test_location_intel import HATCH, client, committed_deck, no_network  # noqa: F401  (fixtures + network guard)

URL = "/api/locations/review"


def queue(c, **headers):
    r = c.get(URL)
    assert r.status_code == 200, r.text
    return r.json()


def groups(q):
    return {g["group_key"]: g for g in q["groups"]}


def mine(q, iid):
    """Pending occurrences of one import, by group (the synthetic seed import has pending rows of its own)."""
    out = {}
    for g in q["groups"]:
        occ = [o for o in g["occurrences"] if o["import_id"] == iid]
        if occ:
            out[g["group_key"]] = occ
    return out


def test_empty_queue(make):
    q = queue(make())
    assert q["groups"] == [] and q["total_pending"] == 0 and q["group_count"] == 0 and q["read_only"] is True


def test_pending_rows_are_grouped_with_context_and_stored_review_fields(client):
    iid = committed_deck(client)
    q = queue(client)
    g = groups(q)
    assert set(mine(q, iid)) == {"forward section", review_group_key(HATCH)}   # auto-applied rows are not pending
    assert q["imports_scanned"] == 2 and q["withdrawn_excluded"] == 0
    assert q["total_pending"] == sum(x["pending_count"] for x in q["groups"]) == sum(len(x["occurrences"]) for x in q["groups"])
    hatch = g[review_group_key(HATCH)]
    assert hatch["display_text"] == HATCH and hatch["pending_count"] == 1 and hatch["import_ids"] == [iid]
    assert hatch["ai_suggestions"] == [{"value": "CARGO HOLD NO. 3", "count": 1}]
    o = hatch["occurrences"][0]
    assert (o["import_id"], o["row_number"], o["original_location"], o["review_status"]) == (iid, 3, HATCH, "pending")
    assert o["record_id"] and o["worksheet_name"] and o["rule_method"] == "unknown" and o["ai_confidence_band"] == "medium"
    assert "ai_confidence" not in o                                               # raw AI confidence never exposed
    assert g["forward section"]["occurrences"][0]["ai_suggestion"] is None


def test_grouping_is_case_and_whitespace_only_and_deterministic(client):
    texts = {4: "Aft winch house", 8: "AFT   winch  HOUSE ", 12: "Aft winch house 2", 16: "Aft wnch house", 20: "Aft winch house."}
    extra = [("NOV 18 2026 ", BANNER, texts, [(f"A{r + 1}", jpeg_bytes(450 + r)) for r in texts])]
    r = commit(client, deck(extra), name=DECK_NAME)
    assert r.status_code == 200, r.text
    q1, q2 = queue(client), queue(client)
    assert q1 == q2                                                               # deterministic
    g = groups(q1)
    aft = g["aft winch house"]
    assert aft["pending_count"] == 2 and aft["import_count"] == 1
    assert {v["text"] for v in aft["variants"]} == {"Aft winch house", "AFT winch HOUSE"}   # originals kept for display
    for distinct in ("aft winch house 2", "aft wnch house", "aft winch house."):  # numbers / spelling / punctuation kept
        assert g[distinct]["pending_count"] == 1
    order = [(-x["pending_count"], x["group_key"]) for x in q1["groups"]]
    assert order == sorted(order) and q1["groups"][0]["group_key"] == "aft winch house"


def test_decided_rows_leave_the_queue_and_undecided_ones_stay(client):
    iid = committed_deck(client)
    assert client.post(f"/api/imports/{iid}/locations", json={"row_number": 2, "action": "keep_original"}).status_code == 200
    assert set(mine(queue(client), iid)) == {review_group_key(HATCH)}
    assert client.post(f"/api/imports/{iid}/locations", json={"row_number": 3, "action": "accept"}).status_code == 200
    assert mine(queue(client), iid) == {}


def test_withdrawn_imports_are_excluded_until_reinstated(client):
    iid = committed_deck(client)
    body = {"reason": "queue test", "confirm_import_id": iid}
    pending = queue(client)["total_pending"]
    assert client.post(f"/api/imports/{iid}/withdraw", json=body).status_code == 200
    q = queue(client)
    assert mine(q, iid) == {} and q["withdrawn_excluded"] == 1 and q["total_pending"] == pending - 2
    assert client.post(f"/api/imports/{iid}/reinstate", json=body).status_code == 200
    assert len(mine(queue(client), iid)) == 2 and queue(client)["total_pending"] == pending


def test_unreadable_decisions_exclude_the_import_and_are_reported(client):
    iid = committed_deck(client)
    (client.app.state.imports.dir / iid / "locations.json").write_text("{bad", encoding="utf-8")
    q = queue(client)
    assert mine(q, iid) == {} and q["unreadable_excluded"] == 1 and any(iid in p for p in q["problems"])


def test_administrators_only(client):
    """SV-02 (F1): the queue is import management data -- administrators only."""
    committed_deck(client)
    for role in ("viewer", "reviewer"):
        sign_in(client, role)
        r = client.get(URL)
        assert (r.status_code, r.json()["code"]) == (403, "forbidden"), role
    sign_in(client, "admin")
    assert client.get(URL).status_code == 200
    client.headers.pop("Authorization")
    r = client.get(URL)
    assert r.status_code == 401 and r.json()["code"] == "not_authenticated"


def test_queue_is_read_only_and_never_calls_ai(client, tmp_path):
    iid = committed_deck(client)
    calls = MockLocationProvider(RuntimeError("the queue must never call the AI"))
    client.app.state.imports.location_intel = LocationIntelligence(client.app.state.imports.settings, provider=calls)
    records = [(r.record_id, r.official_location) for r in client.app.state.store.records]
    audit_before = len(client.app.state.auth.audit.entries(limit=1000))
    before = tree(tmp_path)
    queue(client)
    queue(client)
    assert tree(tmp_path) == before                                               # nothing written anywhere
    assert calls.calls == []
    assert [(r.record_id, r.official_location) for r in client.app.state.store.records] == records
    assert len(client.app.state.auth.audit.entries(limit=1000)) == audit_before
    assert not (client.app.state.imports.dir / iid / "locations.json").exists()
