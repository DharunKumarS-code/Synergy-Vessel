"""Phase 9.1 -- Groq location AI adapter. All HTTP goes through httpx.MockTransport; the Phase 9 network guard
(autouse, imported below) fails the test on any real connection. The key used here is a fake test value."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.config import Settings
from app.location_intel import groq
from app.location_intel.groq import ENDPOINT, GroqLocationProvider, clean_key
from app.location_intel.providers import KEY_ENV, LocationQuery, build_provider
from app.location_intel.rules import KnownLocations
from app.location_intel.service import LocationIntelligence
from test_deck_photos_import import DECK_NAME, deck, rows_of
from test_imports import commit, make, preview, workbook  # noqa: F401
from test_location_intel import DECK, HATCH, REAL, SEED, _Row, client, no_network  # noqa: F401  (autouse guard + fixtures)

FAKE_KEY = "gsk_" + "TESTONLY" * 6
MODEL = "test-model/location-v1"


def answer(items, status=200):
    """A Groq-style chat completion whose message content is the JSON suggestions document."""
    return httpx.Response(status, json={"choices": [{"message": {"content": json.dumps({"suggestions": items})}}]})


def provider(handler, timeout=5.0):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    p = GroqLocationProvider(MODEL, FAKE_KEY, timeout, client=httpx.Client(transport=httpx.MockTransport(wrapped)))
    return p, seen


def svc(p, timeout=5.0):
    return LocationIntelligence(Settings(location_ai_timeout_s=timeout), provider=p)


ROWS = lambda: [_Row("Foc'sle"), _Row(HATCH), _Row("cargo hold 3  hatch COVER"), _Row("Forward section"), _Row(None)]  # noqa: E731


# ===================================================================== request construction
def test_request_is_well_formed_and_contains_only_the_needed_context():
    p, seen = provider(lambda r: answer([]))
    rows = ROWS() + [_Row("Cargo Hold"), _Row("TBC")]                      # ambiguous / placeholder: never sent
    svc(p).annotate(rows, KnownLocations(REAL))
    assert len(seen) == 1                                                 # one request for all distinct names
    req = seen[0]
    assert str(req.url) == ENDPOINT and req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}" and req.headers["content-type"] == "application/json"
    body = json.loads(req.content)
    assert body["model"] == MODEL and body["temperature"] == 0 and body["response_format"] == {"type": "json_object"}
    user = json.loads(body["messages"][1]["content"])
    assert set(user) == {"known_locations", "items"} and "FORECASTLE DECK AREA" in user["known_locations"]
    assert sorted(i["original"] for i in user["items"]) == [HATCH, "Forward section"]   # unresolved, distinct
    assert all(set(i) == {"original", "workbook", "worksheet", "source_cell", "candidates"} for i in user["items"])
    hold = next(i for i in user["items"] if i["original"] == HATCH)
    assert hold["worksheet"] == "NOV 16 2026" and hold["source_cell"] == "A5" and hold["candidates"] == []
    assert "TBC" not in [i["original"] for i in user["items"]]
    prompt = body["messages"][0]["content"]                                 # Phase 9.4 restrictions are in the prompt
    assert "weak hints" in prompt and "Do NOT pick the nearest" in prompt and "Never change or guess a number" in prompt
    assert "Foc'sle" not in req.content.decode() and "a.jpg" not in req.content.decode()      # resolved / filename not sent


def test_nothing_is_sent_when_every_location_is_resolved_or_missing():
    p, seen = provider(lambda r: answer([]))
    svc(p).annotate([_Row("Foc'sle"), _Row(None), _Row("Not Labelled"), _Row("CARGO HOLD NO. 1")], KnownLocations(REAL))
    assert seen == []


def test_large_batches_are_split():
    p, seen = provider(lambda r: answer([]))
    p.suggest([LocationQuery(f"Place {i}") for i in range(45)], REAL)
    assert [len(json.loads(json.loads(r.content)["messages"][1]["content"])["items"]) for r in seen] == [40, 5]


# ===================================================================== responses
def test_valid_suggestions_are_validated_and_need_review():
    p, _ = provider(lambda r: answer([
        {"original": HATCH, "location": "cargo hold no. 3", "confidence": 0.74, "reason": "Holds numbered; context " + "x" * 400},
        {"original": "Forward section", "location": None, "confidence": 0.2, "reason": "No evidence."}]))
    rows = ROWS()
    summary = svc(p).annotate(rows, KnownLocations(REAL))
    foc, hold, hold2, fwd, missing = (r.location_intel for r in rows)
    assert summary["ai"]["used"] == "ok" and summary["ai"]["live"] is True and summary["ai"]["simulated"] is False
    assert hold["ai_suggestion"] == "CARGO HOLD NO. 3" and hold["ai_confidence"] == 0.74 and len(hold["ai_reason"]) <= 300
    assert hold["review_required"] and hold["applied_location"] == HATCH         # never applied automatically
    assert hold2["ai_suggestion"] == "CARGO HOLD NO. 3"
    assert fwd["ai_suggestion"] is None and fwd["review_required"] and fwd["applied_location"] == "Forward section"
    assert foc["applied_location"] == "FORECASTLE DECK AREA" and foc["ai_status"] == "not_needed"
    assert missing["ai_status"] == "not_needed" and missing["applied_location"] is None


@pytest.mark.parametrize("handler,status,problem", [
    (lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "not json {"}}]}), "invalid", "not valid JSON"),
    (lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "{\"answer\": 1}"}}]}), "invalid", "no 'suggestions'"),
    (lambda r: httpx.Response(200, json={"unexpected": True}), "failed", "unavailable"),
    (lambda r: httpx.Response(200, content=b"<html>"), "failed", "unavailable"),
    (lambda r: httpx.Response(401, json={"error": {"message": "Invalid API Key"}}), "failed", "unavailable"),
    (lambda r: httpx.Response(429, json={"error": "rate limited"}), "failed", "unavailable"),
    (lambda r: httpx.Response(500, text="boom"), "failed", "unavailable"),
])
def test_bad_responses_and_api_errors_fall_back(handler, status, problem):
    p, _ = provider(handler)
    rows = ROWS()
    summary = svc(p).annotate(rows, KnownLocations(REAL))
    assert summary["ai"]["used"] == status and problem in summary["ai"]["problems"][0]
    assert rows[0].location_intel["applied_location"] == "FORECASTLE DECK AREA"          # rules unaffected
    assert all(r.location_intel["review_required"] for r in rows[1:])


def test_invalid_items_are_discarded():
    p, _ = provider(lambda r: answer([
        {"original": "Cargo Hold", "location": "CARGO HOLD NO. 3", "confidence": 1.7},            # confidence > 1
        {"original": "Forward section", "location": "Forward Deck Area", "confidence": 0.9},     # invented
        {"original": "Engine room", "location": "BRIDGE DECK", "confidence": 0.9},               # not requested
        {"original": "Cargo Hold", "location": "CARGO HOLD NO. 2", "confidence": "high"},       # not a number
    ]))
    rows = ROWS()
    summary = svc(p).annotate(rows, KnownLocations(REAL))
    assert summary["ai"]["used"] == "ok" and len(summary["ai"]["problems"]) == 4
    assert all(r.location_intel["ai_suggestion"] is None for r in rows)
    assert any("not an existing location" in x for x in summary["ai"]["problems"])


def test_transport_timeout_and_slow_answer_fall_back():
    def raise_timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    p, _ = provider(raise_timeout)
    assert svc(p).annotate(ROWS(), KnownLocations(REAL))["ai"]["used"] == "failed"

    import time

    def slow(request):
        time.sleep(1.0)
        return answer([])

    p, _ = provider(slow, timeout=0.1)
    summary = svc(p, timeout=0.1).annotate(ROWS(), KnownLocations(REAL))
    assert summary["ai"]["used"] == "failed" and "did not answer" in summary["ai"]["problems"][0]


# ===================================================================== configuration: no request unless fully configured
@pytest.mark.parametrize("settings,key,fragment", [
    (dict(location_ai_provider="groq", location_ai_model=MODEL, location_ai_allow_live=True), None, "is not configured"),
    (dict(location_ai_provider="groq", location_ai_model=MODEL, location_ai_allow_live=False), FAKE_KEY, "not enabled"),
    (dict(location_ai_provider="groq", location_ai_model=None, location_ai_allow_live=True), FAKE_KEY, "needs SYNERGY_LOCATION_AI_MODEL"),
    (dict(location_ai_provider="groq", location_ai_model=MODEL, location_ai_allow_live=True), "sk-not-groq-12345678901234", "gsk_"),
    (dict(location_ai_provider="groq", location_ai_model=MODEL, location_ai_allow_live=True), "gsk_has space inside 1234567", "unexpected"),
    (dict(location_ai_provider="disabled", location_ai_model=MODEL, location_ai_allow_live=True), FAKE_KEY, "disabled"),
])
def test_incomplete_configuration_never_builds_a_live_provider(monkeypatch, settings, key, fragment):
    if key is None:
        monkeypatch.delenv(KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(KEY_ENV, key)
    monkeypatch.setattr(groq.GroqLocationProvider, "_post", lambda *a, **k: pytest.fail("a request was attempted"))
    p = build_provider(Settings(**settings))
    assert not p.available and fragment in p.message and FAKE_KEY not in p.message
    rows = [_Row("Forward section")]
    assert LocationIntelligence(Settings(**settings), provider=p).annotate(rows, KnownLocations(REAL))["ai"]["used"] == "disabled"


def test_full_configuration_builds_groq_with_the_selected_model(monkeypatch):
    monkeypatch.setenv(KEY_ENV, f'  "{FAKE_KEY}"\n')                                   # quotes / whitespace tolerated
    p = build_provider(Settings(location_ai_provider="groq", location_ai_model=MODEL, location_ai_allow_live=True))
    assert isinstance(p, GroqLocationProvider) and p.available and p.model == MODEL and p.requests_made == 0
    assert clean_key(f'"{FAKE_KEY}"') == (FAKE_KEY, None)


# ===================================================================== secret isolation
def test_key_never_appears_in_status_repr_errors_logs_or_responses(monkeypatch, caplog, client):
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    caplog.set_level(logging.DEBUG)
    p, _ = provider(lambda r: httpx.Response(401, json={"error": {"message": "Invalid API Key"}}))
    s = LocationIntelligence(Settings(location_ai_provider="groq", location_ai_model=MODEL, location_ai_allow_live=True), provider=p)
    client.app.state.imports.location_intel = s
    out = preview(client, deck(DECK), name=DECK_NAME)
    texts = [repr(p), json.dumps(s.status()), out.text, caplog.text,
             json.dumps(client.app.state.auth.audit.entries(limit=100)), client.get("/api/imports/location-ai").text]
    try:
        p.suggest([LocationQuery("Forward section")], REAL)
    except groq.GroqError as exc:
        texts.append(str(exc))
    assert all(FAKE_KEY not in t and "TESTONLY" not in t for t in texts)
    assert out.status_code == 200 and out.json()["location_intelligence"]["ai"]["used"] == "failed"


# ===================================================================== end-to-end fallback through the import API
def test_groq_failure_never_blocks_preview_or_commit(client):
    def down(request):
        raise httpx.ConnectError("no route", request=request)

    p, seen = provider(down)
    client.app.state.imports.location_intel = LocationIntelligence(client.app.state.imports.settings, provider=p)
    data = deck(DECK)
    pv = preview(client, data, name=DECK_NAME).json()
    assert pv["can_import"] and pv["location_intelligence"]["ai"]["used"] == "failed" and len(seen) == 1
    r = commit(client, data, name=DECK_NAME)
    assert r.status_code == 200
    recs = {x.original_location: x for x in client.app.state.store.records if x.import_id == r.json()["import_id"]}
    assert recs["Foc'sle"].official_location == "FORECASTLE DECK AREA" and recs[HATCH].official_location == HATCH


def test_groq_suggestion_flows_to_review_and_needs_approval(client):
    p, _ = provider(lambda r: answer([{"original": HATCH, "location": "CARGO HOLD NO. 3", "confidence": 0.8,
                                        "reason": "Numbered holds."}]))
    client.app.state.imports.location_intel = LocationIntelligence(client.app.state.imports.settings, provider=p)
    r = commit(client, deck(DECK), name=DECK_NAME)
    iid = r.json()["import_id"]
    rec = next(x for x in client.app.state.store.records if x.import_id == iid and x.original_location == HATCH)
    assert rec.official_location == HATCH and rec.location_review == "pending"
    row = next(x for x in r.json()["rows"] if x["official_location"] == HATCH)["row_number"]
    assert client.post(f"/api/imports/{iid}/locations", json={"row_number": row, "action": "accept"}).status_code == 200
    rec = next(x for x in client.app.state.store.records if x.import_id == iid and x.original_location == HATCH)
    assert rec.official_location == "CARGO HOLD NO. 3" and rec.location_review == "accepted"
