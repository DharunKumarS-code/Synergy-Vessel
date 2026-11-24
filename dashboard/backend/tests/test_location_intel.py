"""Phase 9 -- Excel Location Intelligence. Rules, AI validation and fallback (mock providers only; a network guard
fails any socket use), configuration separation, preview/commit, review decisions, and Phase 8.4 interplay."""

from __future__ import annotations

import json
import socket
import time

import pytest

from app.config import Settings, get_settings
from app.location_intel.providers import (KEY_ENV, InvalidResponse, LocationQuery, MockLocationProvider, build_provider,
                                          validate_response)
from app.location_intel.rules import KnownLocations, key, match
from app.location_intel.service import LocationIntelligence
from conftest import jpeg_bytes, sign_in
from test_deck_photos_import import BANNER, DECK_NAME, deck, rows_of
from test_imports import commit, make, preview, row, tree, workbook  # noqa: F401

REAL = ["BOAT DECK AREA", "BRIDGE DECK", "CARGO HOLD NO. 1", "CARGO HOLD NO. 2", "CARGO HOLD NO. 3", "CARGO HOLD NO. 7",
        "Cross deck Between # 1&2", "Cross deck between # 5&6", "FORECASTLE DECK AREA", "FUNNEL DECK AREA",
        "MONKEY ISLAND", "Not Labelled", "TOP VIEW MAIN DECK", "PORT AND STARBOARD MAIN DECK FACING FORWARD VIEW"]
# Phase 9.4: an unknown, number-consistent text the AI may suggest for ("Cargo Hold" alone is ambiguous and is
# never sent to the AI; a suggestion of hold 3 needs the number 3 in the text).
HATCH = "Cargo hold 3 hatch cover"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open a network connection fails the test (in-process ASGI tests use no sockets)."""
    real_connect = socket.socket.connect
    local = ("127.0.0.1", "::1", "localhost")

    def guarded_connect(sock, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else address
        if host not in local:          # the event loop's own loopback socket pair is allowed
            raise AssertionError(f"network access attempted during a location-intelligence test: {host}")
        return real_connect(sock, address, *a, **k)

    def refuse(address, *a, **k):
        raise AssertionError(f"network access attempted during a location-intelligence test: {address}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.delenv(KEY_ENV, raising=False)


# ===================================================================== Layer 1 + 2: deterministic rules
@pytest.mark.parametrize("text,method,location", [
    ("FORECASTLE DECK AREA", "exact", "FORECASTLE DECK AREA"),
    ("forecastle deck area", "normalized", "FORECASTLE DECK AREA"),                  # case
    ("  FORECASTLE   DECK  AREA ", "exact", "FORECASTLE DECK AREA"),                 # whitespace (collapsed)
    ("Forecastle-Deck Area.", "normalized", "FORECASTLE DECK AREA"),                 # punctuation
    ("Foc'sle", "synonym", "FORECASTLE DECK AREA"),
    ("Forecastle", "synonym", "FORECASTLE DECK AREA"),
    ("Forecastle Deck", "synonym", "FORECASTLE DECK AREA"),
    ("Hold #3", "normalized", "CARGO HOLD NO. 3"),                                   # abbreviation + numbering
    ("cargo hold no 2", "normalized", "CARGO HOLD NO. 2"),
    ("X-deck btw 1&2", "normalized", "Cross deck Between # 1&2"),
    ("P&S main deck facing fwd view", "normalized", "PORT AND STARBOARD MAIN DECK FACING FORWARD VIEW"),
    ("Forcastle deck area", "normalized", "FORECASTLE DECK AREA"),                   # known misspelling
    ("Funel deck area", "spelling", "FUNNEL DECK AREA"),                             # close spelling
    ("Bridge", "synonym", "BRIDGE DECK"),
])
def test_rules_map_variants_to_existing_locations(text, method, location):
    r = match(text, KnownLocations(REAL))
    assert (r.method, r.normalized_location) == (method, location) and r.confidence >= 0.9


@pytest.mark.parametrize("text,method", [
    ("Cargo Hold", "ambiguous"), ("Main deck", "ambiguous"), ("Forward section", "unknown"), ("Engine room", "unknown"),
    ("Cargo hold 9", "unknown"), ("Not Labelled", "missing"), ("", "missing"), (None, "missing"),
])
def test_unresolved_locations_are_never_guessed(text, method):
    r = match(text, KnownLocations(REAL))
    assert r.method == method and r.normalized_location is None and r.confidence < 0.9
    if method == "ambiguous":
        assert len(r.candidates) >= 2


def test_numbers_must_agree_and_synonyms_need_an_existing_location():
    known = KnownLocations(REAL)
    assert match("Cargo hold 4", known).normalized_location is None           # never "CARGO HOLD NO. 1"
    assert match("Foc'sle", KnownLocations(["BRIDGE DECK"])).method == "unknown"  # canonical absent -> not invented
    assert key("Foc'sle") == "forecastle" and key("Hold No. 3") == key("CARGO HOLD #3")


def test_most_used_spelling_is_canonical():
    k = KnownLocations(["Cross deck between # 1&2"] + ["Cross deck Between # 1&2"] * 3)
    assert k.names == ["Cross deck Between # 1&2"]


# ===================================================================== Layer 3: response validation
Q = [LocationQuery("Forward section"), LocationQuery("Cargo Hold")]


@pytest.mark.parametrize("raw,error", [
    ("not json", "not valid JSON"), ("[]", "no 'suggestions'"), ({"answer": "x"}, "no 'suggestions'"), (None, "no 'suggestions'"),
])
def test_unusable_responses_are_rejected(raw, error):
    with pytest.raises(InvalidResponse, match=error):
        validate_response(raw, Q, KnownLocations(REAL))


def test_invalid_items_are_discarded_and_locations_never_invented():
    raw = {"suggestions": [
        {"original": "Forward section", "location": "Forward Deck Area", "confidence": 0.9},     # invented
        {"original": "Cargo Hold", "location": "cargo hold no. 3", "confidence": 1.4},            # confidence out of range
        {"original": "Something else", "location": "BRIDGE DECK", "confidence": 0.8},             # not requested
        {"location": "BRIDGE DECK", "confidence": 0.8},                                           # missing original
    ]}
    found, problems = validate_response(raw, Q, KnownLocations(REAL))
    assert found == {} and len(problems) == 4 and "not an existing location" in problems[0]
    found, _ = validate_response(json.dumps({"suggestions": [{"original": HATCH.lower(), "location": "cargo hold no. 3",
                                                               "confidence": 0.66, "reason": "  x " * 200}]}),
                                 [LocationQuery(HATCH)], KnownLocations(REAL))
    s = found[key(HATCH)]
    assert s.location == "CARGO HOLD NO. 3" and s.confidence == 0.66 and len(s.reason) <= 300   # canonical spelling


@pytest.mark.parametrize("text,suggested", [
    ("Cargo Hold 7 forward bulkhead", "CARGO HOLD NO. 1"),     # different number
    ("Hold 2 hatch coaming", "CARGO HOLD NO. 3"),              # different number
    ("Cargo Hold", "CARGO HOLD NO. 1"),                        # number not in the text: cannot be verified
    ("Overview photo", "Cross deck Between # 1&2"),            # numbered location for a text without numbers
])
def test_ai_suggestions_must_agree_with_numbers_in_the_text(text, suggested):
    found, problems = validate_response({"suggestions": [{"original": text, "location": suggested, "confidence": 0.9}]},
                                        [LocationQuery(text)], KnownLocations(REAL))
    assert found == {} and "number" in problems[0]


def test_number_agreement_keeps_valid_suggestions():
    qs = [LocationQuery("Hold 3 hatch coaming"), LocationQuery("Cross deck 1 & 2 port side"), LocationQuery("Bridge front 2")]
    found, problems = validate_response({"suggestions": [
        {"original": "Hold 3 hatch coaming", "location": "CARGO HOLD NO. 3", "confidence": 0.7},
        {"original": "Cross deck 1 & 2 port side", "location": "Cross deck Between # 1&2", "confidence": 0.7},
        {"original": "Bridge front 2", "location": "BRIDGE DECK", "confidence": 0.7},    # an unnumbered location is no conflict
    ]}, qs, KnownLocations(REAL))
    assert problems == [] and [s.location for s in found.values()] == ["CARGO HOLD NO. 3", "Cross deck Between # 1&2", "BRIDGE DECK"]


# ===================================================================== fallback behaviour of the service
class _Row:
    def __init__(self, loc, n=1):
        self.official_location, self.status, self.row_number = loc, "ready", n
        self.worksheet_name, self.original_excel_filename, self.image_filename, self.source_cell = "NOV 16 2026", "x.xlsx", "a.jpg", "A5"


def _svc(provider, timeout=5.0):
    return LocationIntelligence(Settings(location_ai_timeout_s=timeout), provider=provider)


def test_ai_is_asked_once_per_distinct_unresolved_location_and_never_auto_applied():
    mock = MockLocationProvider(lambda qs, known: {"suggestions": [
        {"original": q.original, "location": "CARGO HOLD NO. 3" if "hold" in q.original.lower() else None,
         "confidence": 0.72, "reason": "mock"} for q in qs]})
    rows = [_Row("Foc'sle"), _Row(HATCH), _Row("cargo hold 3  HATCH cover"), _Row("Forward section"), _Row(None),
            _Row("Cargo Hold"), _Row("N/A")]
    summary = _svc(mock).annotate(rows, KnownLocations(REAL))
    assert len(mock.calls) == 1 and sorted(q.original for q in mock.calls[0]) == [HATCH, "Forward section"]
    foc, hold, hold2, fwd, missing, ambiguous, na = (r.location_intel for r in rows)
    assert foc["review_status"] == "auto_applied" and foc["applied_location"] == "FORECASTLE DECK AREA" and not foc["review_required"]
    assert hold["normalization_method"] == "ai_suggestion" and hold["ai_suggestion"] == "CARGO HOLD NO. 3"
    assert hold["review_required"] and hold["applied_location"] == HATCH
    # rule confidence and AI confidence are kept apart; the AI band is never "high"
    assert hold["confidence"] == 0.0 and hold["confidence_band"] == "low" and hold["ai_confidence_band"] == "medium"
    assert hold["ai_agreement"] == "no_rule_candidate" and "AI suggestion only" in hold["review_reason"]
    assert hold2["ai_suggestion"] == "CARGO HOLD NO. 3"
    assert fwd["ai_suggestion"] is None and fwd["review_required"] and fwd["applied_location"] == "Forward section"
    assert missing["normalization_method"] == "missing" and missing["confidence_band"] == "none" and missing["applied_location"] is None
    # ambiguity is preserved, never resolved by the AI; placeholders are never sent
    assert ambiguous["ai_status"] == "not_sent" and ambiguous["ai_suggestion"] is None and len(ambiguous["candidates"]) >= 2
    assert ambiguous["review_reason"].startswith("Ambiguous") and ambiguous["applied_location"] == "Cargo Hold"
    assert na["ai_status"] == "not_sent" and "placeholder" in na["review_reason"]
    assert summary["counts"] == {"auto_applied": 1, "not_required": 0, "ai_suggestions": 2, "review_required": 6,
                                 "missing": 1, "unknown": 2, "ambiguous": 1, "ai_disagreements": 0}
    assert summary["ai"]["used"] == "ok" and summary["ai"]["simulated"] is True


def test_ai_confidence_is_capped_below_high_and_never_auto_applies():
    mock = MockLocationProvider(lambda qs, known: {"suggestions": [
        {"original": q.original, "location": "MONKEY ISLAND", "confidence": 0.99, "reason": "x"} for q in qs]})
    rows = [_Row("Wheelhouse Top")]
    _svc(mock).annotate(rows, KnownLocations(REAL))
    li = rows[0].location_intel
    assert li["ai_suggestion"] == "MONKEY ISLAND" and li["ai_confidence"] == 0.89 and li["ai_confidence_band"] == "medium"
    assert li["review_status"] == "pending" and li["applied_location"] == "Wheelhouse Top" and li["confidence_band"] == "low"


@pytest.mark.parametrize("answer,agreement,method", [
    ("BOAT DECK AREA", "disagrees", "ai_suggestion"),
    ("FUNNEL DECK AREA", "agrees", "ai_suggestion"),
    (None, "abstained", "spelling"),
])
def test_rule_candidate_is_kept_next_to_the_ai_answer(answer, agreement, method):
    mock = MockLocationProvider(lambda qs, known: {"suggestions": [
        {"original": q.original, "location": answer, "confidence": 0.7, "reason": "x"} for q in qs]})
    rows = [_Row("Funel dek arae")]                                     # low-confidence spelling match: sent with its candidate
    summary = _svc(mock).annotate(rows, KnownLocations(REAL))
    li = rows[0].location_intel
    assert li["rule_method"] == "spelling" and li["rule_confidence"] < 0.9
    assert li["rule_candidate"] == li["normalized_location"] == "FUNNEL DECK AREA"          # never silently replaced
    assert li["ai_agreement"] == agreement and li["normalization_method"] == method and li["review_required"]
    assert summary["counts"]["ai_disagreements"] == (agreement == "disagrees")
    if agreement == "disagrees":
        assert "differs from the rule candidate" in li["review_reason"]


@pytest.mark.parametrize("responder,status", [
    (RuntimeError("provider exploded"), "failed"),
    ("{not json", "invalid"),
    ({"suggestions": "nope"}, "invalid"),
])
def test_provider_failures_fall_back_to_rules_and_review(responder, status):
    rows = [_Row("Foc'sle"), _Row("Forward section")]
    summary = _svc(MockLocationProvider(responder)).annotate(rows, KnownLocations(REAL))
    assert summary["ai"]["used"] == status and summary["ai"]["problems"]
    assert rows[0].location_intel["applied_location"] == "FORECASTLE DECK AREA"          # rules still work
    assert rows[1].location_intel["ai_status"] == status and rows[1].location_intel["review_required"]


def test_timeout_falls_back():
    def slow(qs, known):
        time.sleep(1.0)
        return {"suggestions": []}

    rows = [_Row("Forward section")]
    summary = _svc(MockLocationProvider(slow), timeout=0.1).annotate(rows, KnownLocations(REAL))
    assert summary["ai"]["used"] == "failed" and "did not answer" in summary["ai"]["problems"][0]


def test_disabled_by_default_and_rules_still_apply():
    svc = LocationIntelligence(Settings())
    rows = [_Row("hold 3"), _Row("Forward section")]
    summary = svc.annotate(rows, KnownLocations(REAL))
    assert svc.status()["enabled"] is False and summary["ai"]["used"] == "disabled"
    assert rows[0].location_intel["applied_location"] == "CARGO HOLD NO. 3"
    assert rows[1].location_intel["ai_status"] == "disabled"


# ===================================================================== configuration separation
def test_live_provider_requires_key_allow_live_and_an_adapter(monkeypatch):
    # A live provider name with no adapter ("otherai"). Gemini now has an adapter with its own key handling
    # (user decision 2026-11-20): see test_location_gemini.py.
    s = Settings(location_ai_provider="otherai")
    assert "SYNERGY_LOCATION_AI_API_KEY is not configured" in build_provider(s).message and not build_provider(s).available
    monkeypatch.setenv(KEY_ENV, "test-secret-value-123")
    assert "not enabled" in build_provider(s).message
    p = build_provider(Settings(location_ai_provider="otherai", location_ai_allow_live=True))
    assert not p.available and "No live adapter" in p.message
    status = LocationIntelligence(Settings(location_ai_provider="otherai", location_ai_allow_live=True)).status()
    assert status["key_configured"] is True and "test-secret-value-123" not in json.dumps(status)


def test_location_settings_are_independent_of_the_comparison_ai(monkeypatch):
    for k in ("SYNERGY_AI_PROVIDER", "SYNERGY_AI_MODEL", "SYNERGY_AI_ALLOW_LIVE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SYNERGY_LOCATION_AI_PROVIDER", "mock")
    monkeypatch.setenv("SYNERGY_LOCATION_AI_MODEL", "some-model-1")
    monkeypatch.setenv("SYNERGY_LOCATION_AI_ALLOW_LIVE", "1")
    s = get_settings()
    assert (s.location_ai_provider, s.location_ai_model, s.location_ai_allow_live) == ("mock", "some-model-1", True)
    assert (s.ai_provider, s.ai_model, s.ai_allow_live) == ("disabled", None, False)        # comparison AI untouched
    monkeypatch.setenv("SYNERGY_AI_PROVIDER", "mock")
    for k in ("SYNERGY_LOCATION_AI_PROVIDER", "SYNERGY_LOCATION_AI_MODEL", "SYNERGY_LOCATION_AI_ALLOW_LIVE"):
        monkeypatch.delenv(k)
    s = get_settings()
    assert s.ai_provider == "mock" and (s.location_ai_provider, s.location_ai_allow_live) == ("disabled", False)
    assert not hasattr(s, "location_ai_api_key")                                             # key never in settings


def test_location_intel_does_not_use_the_comparison_ai_code():
    import pathlib

    files = list(pathlib.Path("app/location_intel").glob("*.py"))
    imports = [ln.strip() for f in files for ln in f.read_text(encoding="utf-8").splitlines()
               if ln.strip().startswith(("import ", "from "))]
    assert imports and not [ln for ln in imports if ".ai" in ln or "automation" in ln]
    code = "\n".join(f.read_text(encoding="utf-8") for f in files)
    for banned in ("GROQ_API_KEY", 'environ.get("SYNERGY_AI_', "SYNERGY_AI_PROVIDER", "groq_provider"):
        assert banned not in code
    # 2026-11-20 (user decision): the Gemini location adapter reuses the project's Gemini key (GEMINI_API_KEY via the
    # automation loader). Only gemini.py may do that (providers.py only mentions it in comments).
    others = "\n".join(f.read_text(encoding="utf-8") for f in files if f.name not in ("gemini.py", "providers.py"))
    for banned in ("load_provider_environment", "GEMINI_API_KEY"):
        assert banned not in others


# ===================================================================== API: preview, commit, review decisions
SEED = [row(1, "W43-0001.jpg", loc="FORECASTLE DECK AREA", photo=jpeg_bytes(401)),
        row(2, "W43-0002.jpg", loc="CARGO HOLD NO. 3", photo=jpeg_bytes(402)),
        row(3, "W43-0003.jpg", loc="CARGO HOLD NO. 2", photo=jpeg_bytes(403))]


def scripted(qs, known):
    table = {"forward section": (None, 0.3), key(HATCH): ("CARGO HOLD NO. 3", 0.72)}
    return {"suggestions": [{"original": q.original, "location": table.get(key(q.original), (None, 0.1))[0],
                             "confidence": table.get(key(q.original), (None, 0.1))[1], "reason": "scripted"} for q in qs]}


def approve_all(c, iid):
    """Phase 10.2: an imported location becomes trusted (usable for automatic matching) only once a reviewer approves
    it -- here every imported row of the import is confirmed manually with its own text."""
    rows = [r for r in c.get(f"/api/imports/{iid}").json()["rows"] if r["status"] == "imported"]
    for r in rows:
        res = c.post(f"/api/imports/{iid}/locations",
                     json={"row_number": r["row_number"], "action": "manual", "location": r["official_location"],
                           "confirm_new_location": True})
        assert res.status_code == 200, res.text
    return iid


@pytest.fixture
def client(make):
    c = make()
    r = commit(c, workbook(SEED))
    assert r.status_code == 200
    approve_all(c, r.json()["import_id"])                                       # makes these locations trusted
    c.app.state.imports.location_intel = LocationIntelligence(c.app.state.imports.settings,
                                                              provider=MockLocationProvider(scripted))
    return c


DECK = [("NOV 16 2026 ", BANNER, {4: "Foc'sle", 8: "Forward section", 12: HATCH, 16: "hold no 2"},
         [("A5", jpeg_bytes(411)), ("A9", jpeg_bytes(412)), ("A13", jpeg_bytes(413)), ("A17", jpeg_bytes(414))])]


def committed_deck(c):
    r = commit(c, deck(DECK), name=DECK_NAME)
    assert r.status_code == 200, r.text
    return r.json()["import_id"]


def recs(c, iid):
    return {r.record_id.rsplit("-R", 1)[1]: r for r in c.app.state.store.records if r.import_id == iid}


def test_preview_shows_location_intelligence_and_writes_nothing(client, tmp_path):
    before = tree(tmp_path)
    p = preview(client, deck(DECK), name=DECK_NAME).json()
    li = [r["location_intel"] for r in rows_of(p)]
    assert [x["original_location"] for x in li] == ["Foc'sle", "Forward section", HATCH, "hold no 2"]
    assert [x["normalization_method"] for x in li] == ["synonym", "unknown", "ai_suggestion", "normalized"]
    assert [x["review_required"] for x in li] == [False, True, True, False]
    assert li[2]["ai_suggestion"] == "CARGO HOLD NO. 3" and li[2]["ai_confidence_band"] == "medium"
    assert [r["official_location"] for r in rows_of(p)] == ["Foc'sle", "Forward section", HATCH, "hold no 2"]  # original kept
    assert p["location_intelligence"]["counts"]["review_required"] == 2 and p["location_intelligence"]["ai"]["simulated"]
    assert tree(tmp_path) == before
    assert client.get("/api/imports/location-ai").json()["provider"] == "mock"


def test_commit_applies_only_high_confidence_rules_and_keeps_originals(client):
    iid = committed_deck(client)
    r = recs(client, iid)
    assert (r["0001"].official_location, r["0001"].original_location, r["0001"].location_review) == \
        ("FORECASTLE DECK AREA", "Foc'sle", "auto_applied")
    assert (r["0002"].official_location, r["0002"].location_review) == ("Forward section", "pending")
    assert (r["0003"].official_location, r["0003"].location_review) == (HATCH, "pending")    # AI not applied
    assert r["0004"].official_location == "CARGO HOLD NO. 2"
    manifest = json.loads((client.app.state.imports.dir / iid / "manifest.json").read_text())
    assert manifest["rows"][0]["official_location"] == "Foc'sle"                                     # source text preserved
    hits = client.get("/api/inspections/search", params={"location": "FORECASTLE DECK AREA"}).json()
    assert hits["count"] == 2  # seed record + the standardised "Foc'sle" record


def test_review_decisions_accept_reject_keep_manual(client):
    iid = committed_deck(client)
    post = lambda **b: client.post(f"/api/imports/{iid}/locations", json=b)  # noqa: E731
    assert post(row_number=3, action="accept").json()["effective_location"]["location"] == "CARGO HOLD NO. 3"
    assert recs(client, iid)["0003"].official_location == "CARGO HOLD NO. 3"
    assert recs(client, iid)["0003"].location_review == "accepted"
    assert post(row_number=3, action="reject").status_code == 200                                  # decisions can change
    assert recs(client, iid)["0003"].official_location == HATCH
    assert post(row_number=2, action="accept").json()["code"] == "no_suggestion"
    r = post(row_number=2, action="manual", location="forecastle deck area")
    assert r.json()["decision"]["location"] == "FORECASTLE DECK AREA" and r.json()["decision"]["new_location"] is False
    r = post(row_number=2, action="manual", location="Forward Mast House")                     # untrusted: refused
    assert (r.status_code, r.json()["code"]) == (422, "untrusted_location")
    assert recs(client, iid)["0002"].official_location == "FORECASTLE DECK AREA"                  # nothing changed
    r = post(row_number=2, action="manual", location="Forward Mast House", confirm_new_location=True)
    assert r.json()["decision"]["new_location"] is True and recs(client, iid)["0002"].official_location == "Forward Mast House"
    assert post(row_number=1, action="keep_original").status_code == 200                          # undo an automatic match
    assert recs(client, iid)["0001"].official_location == "Foc'sle"
    assert post(row_number=2, action="manual", location="x").status_code == 400
    assert post(row_number=99, action="keep_original").status_code == 404
    assert post(row_number=1, action="delete").status_code == 422
    detail = client.get(f"/api/imports/{iid}").json()
    assert detail["location_decisions"]["3"]["action"] == "reject"
    assert [r["effective_location"]["review_status"] for r in detail["rows"]][:3] == ["kept_original", "manual", "rejected"]
    stored = json.loads((client.app.state.imports.dir / iid / "locations.json").read_text())
    assert len(stored["events"]) == 5                                                      # successful decisions only
    assert client.get(f"/api/imports/{iid}/verify").json()["valid"] is True
    events = [e["event"] for e in client.app.state.auth.audit.entries(limit=50)]
    assert "import_location_decided" in events and "import_location_decision_failed" in events


def test_review_requires_admin(client):
    iid = committed_deck(client)
    sign_in(client, "reviewer")
    assert client.post(f"/api/imports/{iid}/locations", json={"row_number": 3, "action": "accept"}).status_code == 403
    client.headers.pop("Authorization")
    assert client.post(f"/api/imports/{iid}/locations", json={"row_number": 3, "action": "accept"}).status_code == 401


def test_decisions_survive_withdraw_reinstate_and_are_blocked_while_withdrawn(client):
    iid = committed_deck(client)
    client.post(f"/api/imports/{iid}/locations", json={"row_number": 3, "action": "accept"})
    body = {"reason": "location test", "confirm_import_id": iid}
    assert client.post(f"/api/imports/{iid}/withdraw", json=body).status_code == 200
    assert recs(client, iid) == {}
    r = client.post(f"/api/imports/{iid}/locations", json={"row_number": 2, "action": "keep_original"})
    assert r.status_code == 409 and r.json()["code"] == "import_withdrawn"
    assert client.post(f"/api/imports/{iid}/reinstate", json=body).status_code == 200
    assert recs(client, iid)["0003"].official_location == "CARGO HOLD NO. 3"


def test_decision_rolls_back_when_reload_fails(client):
    iid = committed_deck(client)
    svc = client.app.state.imports
    real, calls = svc._reload, {"n": 0}

    def fail_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("reload broke")
        real()

    svc._reload = fail_once
    r = client.post(f"/api/imports/{iid}/locations", json={"row_number": 3, "action": "accept"})
    svc._reload = real
    assert r.status_code == 500 and r.json()["code"] == "decision_failed"
    assert not (svc.dir / iid / "locations.json").exists() and recs(client, iid)["0003"].official_location == HATCH


def test_unreadable_decisions_file_is_ignored_and_reported(client):
    iid = committed_deck(client)
    (client.app.state.imports.dir / iid / "locations.json").write_text("{bad", encoding="utf-8")
    client.app.state.imports._reload()
    assert recs(client, iid)["0001"].official_location == "FORECASTLE DECK AREA"                 # automatic match kept
    assert client.get(f"/api/imports/{iid}").json()["location_problems"]
    assert client.get(f"/api/imports/{iid}/verify").json()["valid"] is False
    r = client.post(f"/api/imports/{iid}/locations", json={"row_number": 3, "action": "accept"})
    assert r.json()["code"] == "decisions_unreadable"


def test_import_works_when_the_ai_fails_and_duplicates_are_still_detected(client):
    client.app.state.imports.location_intel = LocationIntelligence(client.app.state.imports.settings,
                                                                   provider=MockLocationProvider(RuntimeError("down")))
    data = deck(DECK)
    p = preview(client, data, name=DECK_NAME).json()
    assert p["can_import"] and p["location_intelligence"]["ai"]["used"] == "failed"
    assert all(r["location_intel"]["ai_status"] in ("failed", "not_needed") for r in rows_of(p))
    assert commit(client, data, name=DECK_NAME).status_code == 200
    again = deck(DECK, extra_sheets=["Notes"])                                                     # same photos, new bytes
    p = preview(client, again, name=DECK_NAME).json()
    assert [r["status"] for r in rows_of(p)] == ["duplicate"] * 4


def test_no_key_or_secret_reaches_responses_audit_or_manifest(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "location-secret-xyz-987")
    iid = committed_deck(client)
    client.post(f"/api/imports/{iid}/locations", json={"row_number": 3, "action": "accept"})
    blobs = [client.get("/api/imports/location-ai").text, client.get(f"/api/imports/{iid}").text,
             json.dumps(client.app.state.auth.audit.entries(limit=100)),
             (client.app.state.imports.dir / iid / "manifest.json").read_text()]
    assert all("location-secret-xyz-987" not in b for b in blobs)
    assert client.get("/api/imports/location-ai").json()["key_configured"] is True


def test_disagreement_needs_an_explicit_choice_and_keeps_both_suggestions(client):
    extra = [row(1, "W43-0011.jpg", loc="BOAT DECK AREA", photo=jpeg_bytes(431)),
             row(2, "W43-0012.jpg", loc="FUNNEL DECK AREA", photo=jpeg_bytes(432))]
    r = commit(client, workbook(extra), name="Week_43_extra_Image_Register.xlsx")
    assert r.status_code == 200
    approve_all(client, r.json()["import_id"])
    client.app.state.imports.location_intel = LocationIntelligence(client.app.state.imports.settings, provider=MockLocationProvider(
        lambda qs, known: {"suggestions": [{"original": q.original, "location": "BOAT DECK AREA", "confidence": 0.95,
                                            "reason": "x"} for q in qs]}))
    r = commit(client, deck([("NOV 17 2026 ", BANNER, {4: "Funel dek arae"}, [("A5", jpeg_bytes(433))])]), name=DECK_NAME)
    assert r.status_code == 200, r.text
    iid = r.json()["import_id"]
    li = client.get(f"/api/imports/{iid}").json()["rows"][0]["location_intel"]
    assert (li["rule_candidate"], li["ai_suggestion"], li["ai_agreement"]) == ("FUNNEL DECK AREA", "BOAT DECK AREA", "disagrees")
    assert li["ai_confidence"] == 0.89 and li["review_status"] == "pending"
    post = lambda **b: client.post(f"/api/imports/{iid}/locations", json=b)  # noqa: E731
    assert post(row_number=1, action="accept").json()["code"] == "choose_suggestion"            # AI never wins silently
    assert post(row_number=1, action="accept", location="BRIDGE DECK").json()["code"] == "not_a_suggestion"
    d = post(row_number=1, action="accept", location="FUNNEL DECK AREA").json()
    assert d["effective_location"]["location"] == "FUNNEL DECK AREA"
    assert d["decision"]["disagreement"] == {"rule_candidate": "FUNNEL DECK AREA", "ai_suggestion": "BOAT DECK AREA"}
    assert post(row_number=1, action="accept", location="BOAT DECK AREA").json()["effective_location"]["location"] == "BOAT DECK AREA"
