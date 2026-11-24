"""Gemini live adapter for Excel Location Intelligence. Fully offline: every request goes to an httpx.MockTransport,
the no_network guard (autouse, imported below) fails the test on any real connection, and automation/.env is never
read (the automation key loader is replaced by a stub). The key used here is a fake test value."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys

import httpx
import pytest

from app.config import Settings, get_settings
from app.location_intel import gemini
from app.location_intel.gemini import ENDPOINT, GeminiLocationProvider, clean_key, resolve_model
from app.location_intel.providers import LocationQuery, build_provider, validate_response
from app.location_intel.rules import KnownLocations
from app.location_intel.service import LocationIntelligence
from app.main import build_provider as build_condition_provider
from test_deck_photos_import import DECK_NAME, deck
from test_imports import commit, make, preview, workbook  # noqa: F401
from test_location_intel import DECK, HATCH, REAL, SEED, _Row, client, no_network  # noqa: F401  (autouse guard + fixtures)

FAKE_KEY = "AIza" + "TESTONLYtestonly0123456789_-ABCDEFG"[:35]     # Google key shape, not a real key
MODEL = "gemini-3.8-flash"
REAL_LOADER = gemini.load_automation_environment    # kept before the autouse stub replaces it


@pytest.fixture(autouse=True)
def no_env_file(monkeypatch):
    """The automation .env loader is never run for real; calls are recorded. Gemini key variables start unset."""
    calls = []
    monkeypatch.setattr(gemini, "load_automation_environment", lambda root: calls.append(root))
    for v in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "SYNERGY_LOCATION_AI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    return calls


def answer(items, status=200):
    """A Gemini generateContent response whose text part is the JSON suggestions document."""
    return httpx.Response(status, json={"candidates": [{"content": {"parts": [{"text": json.dumps({"suggestions": items})}]}}]})


def provider(handler, timeout=5.0):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    p = GeminiLocationProvider(MODEL, FAKE_KEY, timeout, client=httpx.Client(transport=httpx.MockTransport(wrapped)))
    return p, seen


def svc(p, timeout=5.0):
    return LocationIntelligence(Settings(location_ai_timeout_s=timeout), provider=p)


ROWS = lambda: [_Row("Foc'sle"), _Row(HATCH), _Row("Forward section"), _Row(None)]  # noqa: E731
LIVE = dict(location_ai_provider="gemini", location_ai_allow_live=True)


# ===================================================================== configuration and enablement
def test_location_ai_is_disabled_by_default(monkeypatch):
    for v in ("SYNERGY_LOCATION_AI_PROVIDER", "SYNERGY_LOCATION_AI_ALLOW_LIVE", "SYNERGY_LOCATION_AI_MODEL"):
        monkeypatch.delenv(v, raising=False)
    s = get_settings()
    p = build_provider(s)
    assert (s.location_ai_provider, s.location_ai_allow_live, p.available) == ("disabled", False, False)


def test_gemini_needs_explicit_live_approval_and_never_loads_the_key_before(no_env_file, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    p = build_provider(Settings(location_ai_provider="gemini"))
    assert not p.available and "SYNERGY_LOCATION_AI_ALLOW_LIVE" in p.message
    assert no_env_file == []                                                   # .env loader not run


def test_gemini_initialises_with_the_existing_key_and_default_model(no_env_file, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    p = build_provider(Settings(**LIVE))
    assert isinstance(p, GeminiLocationProvider) and p.available and p.model == "gemini-3.8-flash"
    assert p.message == "Live location AI: Gemini (gemini-3.8-flash)." and no_env_file == []   # already in env: no .env read
    assert FAKE_KEY not in repr(p)
    status = LocationIntelligence(Settings(**LIVE), provider=p).status()
    assert (status["enabled"], status["provider"], status["model"], status["live"], status["key_configured"]) == (
        True, "gemini", "gemini-3.8-flash", True, True)
    assert FAKE_KEY not in json.dumps(status)


def test_key_is_loaded_through_the_existing_automation_loader(monkeypatch):
    monkeypatch.setattr(gemini, "load_automation_environment", lambda root: monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY))
    assert build_provider(Settings(**LIVE)).available


@pytest.mark.parametrize("value", [None, "", "   ", "PASTE_YOUR_GEMINI_API_KEY_HERE", "AIza-too-short", "gsk_" + "x" * 40,
                                   "AIza" + "x" * 34 + " y"])
def test_missing_placeholder_or_malformed_key_never_builds_a_live_provider(no_env_file, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("GEMINI_API_KEY", value)
    p = build_provider(Settings(**LIVE))
    assert not p.available and "GEMINI_API_KEY is not configured" in p.message
    assert no_env_file and LocationIntelligence(Settings(**LIVE), provider=p).status()["key_configured"] is False


def test_key_shape_and_model_override():
    assert clean_key(f'  "{FAKE_KEY}"\n') == FAKE_KEY and clean_key("AIza") is None
    assert resolve_model(None) == "gemini-3.8-flash" and resolve_model("Gemini-3.8-Pro") == "gemini-3.8-pro"
    assert resolve_model("qwen/qwen3.8-27b") == "gemini-3.8-flash"            # only Gemini model names are used
    assert resolve_model("gemini-x/../../evil") == "gemini-3.8-flash"


def test_condition_analysis_groq_configuration_is_unaffected(monkeypatch):
    monkeypatch.setenv("SYNERGY_LOCATION_AI_PROVIDER", "gemini")
    monkeypatch.setenv("SYNERGY_LOCATION_AI_ALLOW_LIVE", "1")
    monkeypatch.setenv("SYNERGY_AI_PROVIDER", "groq")
    monkeypatch.setenv("SYNERGY_AI_ALLOW_LIVE", "1")
    monkeypatch.delenv("SYNERGY_AI_MODEL", raising=False)
    s = get_settings()
    assert (s.ai_provider, s.ai_allow_live, s.location_ai_provider) == ("groq", True, "gemini")
    cond = build_condition_provider(s)
    assert (cond.name, cond.model) == ("groq", "qwen/qwen3.8-27b")             # condition AI stays on Groq
    monkeypatch.setenv("SYNERGY_LOCATION_AI_PROVIDER", "disabled")
    assert build_condition_provider(get_settings()).name == "groq"


# ===================================================================== the request
def test_request_contains_only_location_context_and_keeps_the_key_out_of_the_url():
    p, seen = provider(lambda r: answer([]))
    svc(p).annotate(ROWS() + [_Row("Cargo Hold"), _Row("TBC")], KnownLocations(REAL))
    assert len(seen) == 1
    req = seen[0]
    assert str(req.url) == ENDPOINT.format(model=MODEL) and req.method == "POST"
    assert req.headers["x-goog-api-key"] == FAKE_KEY and FAKE_KEY not in str(req.url) and "key=" not in str(req.url)
    body = json.loads(req.content)
    cfg = body["generationConfig"]
    assert cfg["temperature"] == 0 and cfg["responseMimeType"] == "application/json" and "suggestions" in cfg["responseSchema"]["properties"]
    user = json.loads(body["contents"][0]["parts"][0]["text"])
    assert set(user) == {"known_locations", "items"} and set(user["known_locations"]) == set(REAL) - {"Not Labelled"}  # placeholder never offered
    assert sorted(i["original"] for i in user["items"]) == [HATCH, "Forward section"]       # unresolved only
    assert all(set(i) == {"original", "workbook", "worksheet", "source_cell", "candidates"} for i in user["items"])
    prompt = body["systemInstruction"]["parts"][0]["text"]
    assert "Never invent" in prompt and "alternatives" in prompt and "Never change or guess a number" in prompt
    data = body["contents"][0]["parts"][0]["text"]                     # the data part (the prompt itself cites "TBC")
    assert "a.jpg" not in data and "Foc'sle" not in data and "TBC" not in data                 # no filenames, resolved or placeholders


def test_large_batches_are_split():
    p, seen = provider(lambda r: answer([]))
    p.suggest([LocationQuery(f"Place {i}") for i in range(45)], REAL)
    sizes = [len(json.loads(json.loads(r.content)["contents"][0]["parts"][0]["text"])["items"]) for r in seen]
    assert sizes == [40, 5]


# ===================================================================== answers, validation and review
def test_successful_suggestion_is_validated_labelled_and_needs_review():
    p, _ = provider(lambda r: answer([{"original": HATCH, "location": "cargo hold no. 3", "confidence": 0.8,
                                       "reason": "Hold 3 named in the text.", "alternatives": []}]))
    rows = ROWS()
    summary = svc(p).annotate(rows, KnownLocations(REAL))
    hold = rows[1].location_intel
    assert summary["ai"]["used"] == "ok" and summary["ai"]["provider"] == "gemini"
    assert hold["ai_suggestion"] == "CARGO HOLD NO. 3" and hold["ai_provider"] == "gemini" and hold["ai_simulated"] is False
    assert hold["review_required"] and hold["review_status"] == "pending" and hold["applied_location"] == HATCH   # never applied
    assert hold["ai_ambiguous"] is False and hold["ai_confidence"] < 0.85                       # capped below "high"


def test_invented_or_wrong_number_locations_are_rejected():
    p, _ = provider(lambda r: answer([
        {"original": HATCH, "location": "CARGO HOLD NO. 9", "confidence": 0.9, "reason": "guess"},
        {"original": "Forward section", "location": "BOSUN STORE", "confidence": 0.9, "reason": "invented"}]))
    rows = ROWS()
    summary = svc(p).annotate(rows, KnownLocations(REAL))
    assert rows[1].location_intel["ai_suggestion"] is None and rows[2].location_intel["ai_suggestion"] is None
    assert rows[1].location_intel["ai_agreement"] == "no_valid_answer" and len(summary["ai"]["problems"]) == 2
    assert rows[2].location_intel["applied_location"] == "Forward section"                       # rules + review fallback


def test_ambiguous_answer_lists_validated_alternatives_and_needs_review():
    p, _ = provider(lambda r: answer([{"original": "Forward section", "location": None, "confidence": 0.4,
                                       "reason": "Forward could be either deck.",
                                       "alternatives": ["forecastle deck area", "TOP VIEW MAIN DECK", "INVENTED ROOM", "CARGO HOLD NO. 1"]}]))
    rows = ROWS()
    svc(p).annotate(rows, KnownLocations(REAL))
    fwd = rows[2].location_intel
    assert fwd["ai_ambiguous"] is True and fwd["ai_alternatives"] == ["FORECASTLE DECK AREA", "TOP VIEW MAIN DECK"]
    assert fwd["ai_suggestion"] is None and fwd["review_required"] and fwd["review_status"] == "pending"
    assert "several plausible locations" in fwd["review_reason"]


def test_low_confidence_answer_is_flagged_for_review():
    p, _ = provider(lambda r: answer([{"original": HATCH, "location": "CARGO HOLD NO. 3", "confidence": 0.3, "reason": "weak"}]))
    rows = ROWS()
    svc(p).annotate(rows, KnownLocations(REAL))
    hold = rows[1].location_intel
    assert hold["ai_low_confidence"] is True and hold["ai_confidence_band"] == "low" and hold["review_required"]
    assert "confidence is low" in hold["review_reason"]


def test_alternatives_must_be_a_list_of_existing_locations():
    q = [LocationQuery("Forward section")]
    found, problems = validate_response({"suggestions": [{"original": "Forward section", "location": None, "confidence": 0.3,
                                                          "reason": "x", "alternatives": "BRIDGE DECK"}]}, q, KnownLocations(REAL))
    assert found[next(iter(found))].alternatives == () and problems


@pytest.mark.parametrize("handler,status", [
    (lambda r: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Sure! Here you go"}]}}]}), "invalid"),
    (lambda r: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{\"nope\": 1}"}]}}]}), "invalid"),
    (lambda r: httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]}), "failed"),       # blocked: no content
    (lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "OTHER"}}), "failed"),
    (lambda r: httpx.Response(200, text="<html>"), "failed"),
    (lambda r: httpx.Response(400, json={"error": {"message": "API key not valid"}}), "failed"),
    (lambda r: httpx.Response(403), "failed"),
    (lambda r: httpx.Response(429), "failed"),
    (lambda r: httpx.Response(503), "failed"),
])
def test_invalid_output_and_provider_errors_fall_back_to_rules(handler, status):
    p, seen = provider(handler)
    rows = ROWS()
    summary = svc(p).annotate(rows, KnownLocations(REAL))
    assert summary["ai"]["used"] == status and len(seen) == 1
    assert rows[0].location_intel["applied_location"] == "FORECASTLE DECK AREA"                   # rules still applied
    assert all(r.location_intel["ai_suggestion"] is None for r in rows)
    assert rows[1].location_intel["review_required"] and rows[1].location_intel["applied_location"] == HATCH


def test_timeouts_fall_back_to_rules():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    p, _ = provider(slow)
    rows = ROWS()
    assert svc(p).annotate(rows, KnownLocations(REAL))["ai"]["used"] == "failed"
    assert rows[1].location_intel["applied_location"] == HATCH
    try:
        p.suggest([LocationQuery("x")], REAL)
    except gemini.GeminiError as exc:
        assert "timed out" in str(exc)


def test_key_never_appears_in_errors_logs_status_or_responses(monkeypatch, caplog, client):
    caplog.set_level(logging.DEBUG)
    p, _ = provider(lambda r: httpx.Response(400, json={"error": {"message": f"API key not valid: {FAKE_KEY}"}}))
    s = LocationIntelligence(Settings(**LIVE), provider=p)
    client.app.state.imports.location_intel = s
    out = preview(client, deck(DECK), name=DECK_NAME)
    texts = [repr(p), json.dumps(s.status()), out.text, caplog.text, client.get("/api/imports/location-ai").text,
             json.dumps(client.app.state.auth.audit.entries(limit=100))]
    try:
        p.suggest([LocationQuery("Forward section")], REAL)
    except gemini.GeminiError as exc:
        texts.append(str(exc))
    assert all(FAKE_KEY not in t and "TESTONLY" not in t for t in texts)
    assert out.status_code == 200 and out.json()["location_intelligence"]["ai"]["used"] == "failed"


# ===================================================================== import preview and commit
def test_import_preview_shows_gemini_suggestions_and_commit_never_applies_them(client, data_root):
    before = {p.relative_to(data_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(data_root.rglob("*")) if p.is_file()}
    p, seen = provider(lambda r: answer([
        {"original": HATCH, "location": "CARGO HOLD NO. 3", "confidence": 0.8, "reason": "Hold 3."},
        {"original": "Forward section", "location": None, "confidence": 0.3, "reason": "Unclear.",
         "alternatives": ["FORECASTLE DECK AREA", "TOP VIEW MAIN DECK"]}]))
    client.app.state.imports.location_intel = LocationIntelligence(client.app.state.imports.settings, provider=p)
    data = deck(DECK)
    pv = preview(client, data, name=DECK_NAME).json()
    rows = {r["official_location"]: r["location_intel"] for r in pv["rows"]}
    assert rows[HATCH]["ai_suggestion"] == "CARGO HOLD NO. 3" and rows[HATCH]["ai_provider"] == "gemini"
    assert rows["Forward section"]["ai_ambiguous"] is True
    r = commit(client, data, name=DECK_NAME)
    assert r.status_code == 200
    iid = r.json()["import_id"]
    recs = {x.original_location: x for x in client.app.state.store.records if x.import_id == iid}
    assert recs[HATCH].official_location == HATCH and recs[HATCH].location_review == "pending"     # suggestion only
    assert recs["Forward section"].official_location == "Forward section"
    # the review queue carries the AI reasoning and ambiguity for the existing workbench
    client.post("/api/auth/logout")
    from conftest import sign_in
    sign_in(client, "admin")
    q = client.get("/api/locations/review").json()
    occ = [o for g in q["groups"] for o in g.get("occurrences", [])]
    fwd = next(o for o in occ if o["original_location"] == "Forward section")
    # TOP VIEW MAIN DECK is not a trusted location in this data set, so validation dropped it
    assert fwd["ai_ambiguous"] is True and fwd["ai_alternatives"] == ["FORECASTLE DECK AREA"]
    assert fwd["ai_provider"] == "gemini" and fwd["ai_reason"] == "Unclear."
    # historical inspection data and photographs are untouched
    after = {p.relative_to(data_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(data_root.rglob("*")) if p.is_file()}
    assert {k: v for k, v in after.items() if k in before} == before


def test_the_loader_reuses_the_automation_loader_and_writes_no_bytecode(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", False)   # known starting value (CI sets PYTHONDONTWRITEBYTECODE=1)
    seen = {}

    class FakeConfig:
        @staticmethod
        def load_provider_environment():
            seen["called"] = True

    def fake_import(name):
        seen["module"], seen["flag"] = name, sys.dont_write_bytecode
        return FakeConfig

    monkeypatch.setattr(gemini.importlib, "import_module", fake_import)
    REAL_LOADER(os.getcwd())
    assert seen == {"module": "src.config", "flag": True, "called": True} and sys.dont_write_bytecode is False
