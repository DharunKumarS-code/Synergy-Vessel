"""Phase 5 -- AI condition analysis. Offline only: every provider here is a mock or a stub."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ai.providers import (
    MOCK_SCENARIOS,
    MockConditionProvider,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.ai.records import AnalysisRecord
from app.ai.schema import InvalidAIResponse, apply_policy, validate_assessment
from app.ai.store import AnalysisStore
from app.config import Settings
from app.main import create_app
from conftest import sign_in

AUTOMATION_ROOT = Path(__file__).resolve().parents[3] / "automation"


@pytest.fixture
def make_client(data_root, tmp_path):
    clients = []

    def _make(provider=None, analysis_dir=None):
        settings = Settings(data_root=data_root, analysis_dir=analysis_dir or tmp_path / "analyses")
        c = TestClient(create_app(settings, condition_provider=provider or MockConditionProvider("stable")))
        c.__enter__()
        clients.append(c)
        sign_in(c, "reviewer")
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def rid(client, image_id):
    """Record id of the first JSON-report record showing image_id."""
    store = client.app.state.store
    return next(r.record_id for r in store.records if r.image_id == image_id and r.source == "json_report")


def pair(client, earlier="W30-0001", later="W34-0001", **extra):
    # The default fixture pair has MIXED location evidence (a Phase 5A warning); tests that are not about
    # pair validation acknowledge it explicitly. Validation itself is covered in test_pair_validation.py.
    extra.setdefault("acknowledge_warnings", True)
    return {"earlier_record_id": rid(client, earlier), "later_record_id": rid(client, later), **extra}


# --------------------------------------------------------------------------- schema & policy
def scenario(name, **changes):
    raw = json.loads(json.dumps(MOCK_SCENARIOS[name]))
    raw.update(changes)
    return raw


@pytest.mark.parametrize("name,expected", [
    ("deteriorated", "DETERIORATED"),
    ("ameliorated", "AMELIORATED"),
    ("stable", "STABLE"),
    ("needs_review", "NEEDS_REVIEW"),
])
def test_each_classification_validates(name, expected):
    a = validate_assessment(scenario(name))
    assert apply_policy(a).classification.value == expected


def test_malformed_json_and_shapes_are_rejected():
    for bad in ("{not json", "[]", 42, None, ""):
        with pytest.raises(InvalidAIResponse):
            validate_assessment(bad)
    with pytest.raises(InvalidAIResponse, match="classification"):
        validate_assessment(scenario("stable", classification="BETTER"))
    with pytest.raises(InvalidAIResponse, match="invented_field"):
        validate_assessment(scenario("stable", invented_field="x"))
    raw = scenario("stable")
    del raw["explanation"]
    with pytest.raises(InvalidAIResponse, match="explanation"):
        validate_assessment(raw)
    with pytest.raises(InvalidAIResponse, match="visual_observations"):
        validate_assessment(scenario("stable", visual_observations=[]))
    with pytest.raises(InvalidAIResponse, match="deterioration_evidence"):
        validate_assessment(scenario("deteriorated", deterioration_evidence=[{"region": "x", "change": "y"}]))
    # JSON text is accepted when valid
    assert validate_assessment(json.dumps(scenario("stable"))).classification.value == "STABLE"


def test_confidence_values_are_validated():
    for bad in ("VERY HIGH", "high-ish", 0.9, None):
        with pytest.raises(InvalidAIResponse, match="confidence"):
            validate_assessment(scenario("stable", confidence=bad))


def test_policy_downgrades_unsupported_or_unreliable_results():
    cases = [
        scenario("deteriorated", deterioration_evidence=[]),
        scenario("ameliorated", improvement_evidence=[]),
        scenario("stable", deterioration_evidence=MOCK_SCENARIOS["deteriorated"]["deterioration_evidence"]),
        scenario("deteriorated", same_area="DIFFERENT_AREA"),
        scenario("ameliorated", confidence="LOW"),
        scenario("stable", comparison_reliability="LOW"),
        scenario("stable", later_image_quality="POOR"),
    ]
    for raw in cases:
        out = apply_policy(validate_assessment(raw))
        assert out.classification.value == "NEEDS_REVIEW"
        assert out.adjustments and out.adjustments[0].startswith(f"Changed from {raw['classification']}")
        assert out.human_review_required


def test_review_requirement_rules():
    assert apply_policy(validate_assessment(scenario("deteriorated"))).human_review_required  # always
    assert apply_policy(validate_assessment(scenario("needs_review"))).human_review_required
    assert not apply_policy(validate_assessment(scenario("stable"))).human_review_required  # HIGH/HIGH
    assert apply_policy(validate_assessment(scenario("stable", confidence="MEDIUM"))).human_review_required


# --------------------------------------------------------------------------- endpoints
def test_status_never_exposes_secrets_and_defaults_to_disabled(data_root, tmp_path):
    with TestClient(create_app(Settings(data_root=data_root, analysis_dir=tmp_path / "a"))) as c:
        sign_in(c, "viewer")                                       # SV-02: status needs an account
        body = c.get("/api/ai-analysis/status").json()
    assert body["available"] is False and body["provider"] == "disabled"
    assert "key" not in json.dumps(body).lower()


def test_valid_pair_is_analysed_and_persisted(make_client, tmp_path):
    provider = MockConditionProvider("deteriorated")
    c = make_client(provider)
    r = c.post("/api/ai-analysis/compare", json=pair(c, location="Main Deck",
               earlier_inspection_date="2026-08-02", later_inspection_date="2026-09-06"))
    assert r.status_code == 200, r.text
    body = r.json()
    a = body["analysis"]
    assert body["reused"] is False
    assert a["earlier"]["image_id"] == "W30-0001" and a["later"]["image_id"] == "W34-0001"
    assert a["earlier"]["inspection_date"] == "2026-08-02" and a["later"]["inspection_date"] == "2026-09-06"
    assert a["later"]["date_source"] == "worksheet_name"
    assert a["interval_days"] == 35
    assert a["classification"] == "DETERIORATED" and a["confidence"] == "MEDIUM"
    assert a["simulated"] is True and a["provider"] == "mock" and a["prompt_version"]
    assert a["review"]["status"] == "PENDING_REVIEW" and a["human_review_required"] is True
    assert provider.calls == [("W30-0001", "W34-0001")]  # order preserved: earlier, later
    # persisted as its own file, readable back by id and after a reload
    f = tmp_path / "analyses" / f"{a['analysis_id']}.json"
    assert f.is_file()
    assert c.get(f"/api/ai-analysis/{a['analysis_id']}").json()["classification"] == "DETERIORATED"
    fresh = AnalysisStore(tmp_path / "analyses")
    fresh.load()
    assert fresh.get(a["analysis_id"]).ai_result.deterioration_evidence[0].region


def test_get_endpoints_never_trigger_analysis(make_client):
    provider = MockConditionProvider("stable")
    c = make_client(provider)
    c.get("/api/ai-analysis/status")
    c.get("/api/ai-analysis/history")
    assert c.get("/api/ai-analysis/CA-20260101-0123456789").status_code == 404
    assert provider.calls == []


@pytest.mark.parametrize("body,status", [
    ({"earlier_record_id": "../secret", "later_record_id": "x"}, 422),
    ({"earlier_record_id": "", "later_record_id": "x"}, 422),
    ({"earlier_record_id": "a", "later_record_id": "a"}, 422),
    ({"earlier_record_id": "nope-1", "later_record_id": "nope-2"}, 404),
    ({"earlier_record_id": "a", "later_record_id": "b", "unexpected": 1}, 422),
])
def test_invalid_identifiers(make_client, body, status):
    c = make_client()
    assert c.post("/api/ai-analysis/compare", json=body).status_code == status
    assert c.get("/api/ai-analysis/not-an-id").status_code == 400


def test_invalid_dates(make_client):
    provider = MockConditionProvider()
    c = make_client(provider)
    # reversed order
    r = c.post("/api/ai-analysis/compare", json=pair(c, "W34-0001", "W30-0001"))
    assert r.status_code == 400 and r.json()["code"] == "invalid_date_order"
    # same date
    r = c.post("/api/ai-analysis/compare", json=pair(c, "W30-0001", "W30-0003"))
    assert r.status_code == 400 and r.json()["code"] == "same_inspection_date"  # Phase 5A: specific code
    # dates that disagree with the records
    r = c.post("/api/ai-analysis/compare", json=pair(c, earlier_inspection_date="2026-08-03"))
    assert r.status_code == 400 and r.json()["code"] == "date_mismatch"
    # request dates in the wrong order / malformed
    assert c.post("/api/ai-analysis/compare", json=pair(c, earlier_inspection_date="2026-09-06",
                  later_inspection_date="2026-08-02")).status_code == 422
    assert c.post("/api/ai-analysis/compare", json=pair(c, later_inspection_date="06/09/2026")).status_code == 422
    assert provider.calls == []


def test_same_image_comparison_is_refused(make_client):
    provider = MockConditionProvider()
    c = make_client(provider)
    r = c.post("/api/ai-analysis/compare", json=pair(c, "W30-0002", "W34-0001"))  # identical SHA-256
    assert r.status_code == 400 and r.json()["code"] == "same_image"
    assert provider.calls == []


def test_missing_photograph(make_client):
    provider = MockConditionProvider()
    c = make_client(provider)
    store = c.app.state.store
    missing = next(r for r in store.records if r.image_status == "missing" and r.inspection_date)
    r = c.post("/api/ai-analysis/compare", json={"earlier_record_id": rid(c, "W30-0001"), "later_record_id": missing.record_id})
    assert r.status_code == 422 and r.json()["code"] == "photograph_unavailable"
    assert provider.calls == []


def test_duplicate_requests_reuse_the_saved_result(make_client):
    provider = MockConditionProvider("stable")
    c = make_client(provider)
    first = c.post("/api/ai-analysis/compare", json=pair(c)).json()
    second = c.post("/api/ai-analysis/compare", json=pair(c)).json()
    assert second["reused"] is True and second["analysis"]["analysis_id"] == first["analysis"]["analysis_id"]
    assert len(provider.calls) == 1
    # explicit re-analysis is refused until a reviewer rejects / asks for reassessment
    r = c.post("/api/ai-analysis/compare", json=pair(c, reanalyse=True))
    assert r.status_code == 409 and r.json()["code"] == "already_analysed"
    aid = first["analysis"]["analysis_id"]
    c.post(f"/api/ai-analysis/{aid}/review", json={"status": "NEEDS_REASSESSMENT", "reviewer": "Inspector A"})
    r = c.post("/api/ai-analysis/compare", json=pair(c, reanalyse=True)).json()
    assert r["reused"] is False and r["analysis"]["supersedes"] == aid
    assert len(provider.calls) == 2


def test_concurrent_duplicate_is_blocked(make_client):
    gate, started = threading.Event(), threading.Event()

    def slow(e, l, p):
        started.set()
        gate.wait(5)
        return MOCK_SCENARIOS["stable"]

    c = make_client(MockConditionProvider(slow))
    body = pair(c)
    results = {}
    t = threading.Thread(target=lambda: results.setdefault("first", c.post("/api/ai-analysis/compare", json=body)))
    t.start()
    assert started.wait(5)
    second = c.post("/api/ai-analysis/compare", json=body)
    gate.set()
    t.join(5)
    assert second.status_code == 409 and second.json()["code"] == "analysis_in_progress"
    assert results["first"].status_code == 200


@pytest.mark.parametrize("raw", ["{not json", {"classification": "STABLE"}, {**MOCK_SCENARIOS["stable"], "extra": 1}])
def test_malformed_ai_response_is_not_saved(make_client, tmp_path, raw):
    c = make_client(MockConditionProvider(raw))
    r = c.post("/api/ai-analysis/compare", json=pair(c))
    assert r.status_code == 502 and r.json()["code"] == "invalid_ai_response"
    assert "No result was saved" in r.json()["detail"]
    assert not (tmp_path / "analyses").exists() or not list((tmp_path / "analyses").glob("*.json"))


@pytest.mark.parametrize("exc,status,code", [
    (ProviderQuotaExhausted(retry_after_seconds=3600), 429, "quota_exhausted"),
    (ProviderRateLimited(retry_after_seconds=30), 429, "rate_limited"),
    (ProviderUnavailable("internal detail: host=10.0.0.1 key=abc"), 503, "ai_unavailable"),
    (ProviderTimeout(), 504, "ai_timeout"),
    (RuntimeError("boom with secret"), 500, None),
])
def test_provider_failures_are_friendly_and_save_nothing(make_client, tmp_path, exc, status, code):
    c = make_client(MockConditionProvider(exc))
    if code is None:
        with pytest.raises(RuntimeError):  # unexpected errors are not swallowed into a result
            c.post("/api/ai-analysis/compare", json=pair(c))
        return
    r = c.post("/api/ai-analysis/compare", json=pair(c))
    assert r.status_code == status and r.json()["code"] == code
    assert "10.0.0.1" not in r.text and "key=" not in r.text
    if code == "quota_exhausted":
        assert r.headers["Retry-After"] == "3600"
    assert not list((tmp_path / "analyses").glob("*.json")) if (tmp_path / "analyses").exists() else True


def test_unavailable_provider_returns_503(data_root, tmp_path):
    with TestClient(create_app(Settings(data_root=data_root, analysis_dir=tmp_path / "a"))) as c:
        sign_in(c, "reviewer")
        r = c.post("/api/ai-analysis/compare", json=pair(c))
    assert r.status_code == 503 and r.json()["code"] == "ai_unavailable"


def test_human_review_preserves_original_ai_result(make_client, tmp_path):
    c = make_client(MockConditionProvider("ameliorated"))
    a = c.post("/api/ai-analysis/compare", json=pair(c)).json()["analysis"]
    aid = a["analysis_id"]
    me = c.get("/api/auth/me").json()
    r = c.post(f"/api/ai-analysis/{aid}/review", json={"status": "REJECTED", "reviewer": "Inspector A", "note": "Different hatch."})
    assert r.status_code == 200
    # SV-03: the decision is final for reviewers; only an administrator can correct it, with a reason.
    assert c.post(f"/api/ai-analysis/{aid}/review", json={"status": "APPROVED"}).json()["code"] == "review_already_decided"
    sign_in(c, "admin")
    r = c.post(f"/api/ai-analysis/{aid}/review", json={"status": "APPROVED", "reviewer": "Inspector B",
                                                        "note": "Same hatch after all; approved."}).json()
    assert r["review"]["status"] == "APPROVED"
    assert [h["status"] for h in r["review"]["history"]] == ["REJECTED", "APPROVED"]
    assert r["review"]["history"][0]["note"] == "Different hatch."
    for k in ("ai_result", "classification", "model_classification", "confidence", "policy_adjustments"):
        assert r[k] == a[k]
    stored = AnalysisRecord.model_validate_json((tmp_path / "analyses" / f"{aid}.json").read_text(encoding="utf-8"))
    assert stored.review.status.value == "APPROVED" and stored.ai_result.classification.value == "AMELIORATED"
    # validation
    assert c.post(f"/api/ai-analysis/{aid}/review", json={"status": "VERIFIED", "reviewer": "x"}).status_code == 422
    # Phase 8.2: the request's "reviewer" text is ignored; identity always comes from the session.
    first, correction = r["review"]["history"]
    assert first["reviewer"] == me["display_name"] and first["reviewer_id"] == me["user_id"] and first["reviewer_role"] == "reviewer"
    assert correction["reviewer"] != "Inspector B" and correction["reviewer_role"] == "admin" and correction["correction"] is True
    assert c.post("/api/ai-analysis/CA-20260101-0000000000/review", json={"status": "APPROVED", "reviewer": "x"}).status_code == 404


def test_history_filters(make_client):
    c = make_client(MockConditionProvider("deteriorated"))
    c.post("/api/ai-analysis/compare", json=pair(c, location="Main Deck"))
    h = c.get("/api/ai-analysis/history").json()
    assert h["total"] == 1
    assert c.get("/api/ai-analysis/history", params={"classification": "DETERIORATED"}).json()["total"] == 1
    assert c.get("/api/ai-analysis/history", params={"classification": "STABLE"}).json()["total"] == 0
    assert c.get("/api/ai-analysis/history", params={"review_status": "PENDING_REVIEW"}).json()["total"] == 1
    assert c.get("/api/ai-analysis/history", params={"image_id": "W34-0001"}).json()["total"] == 1
    assert c.get("/api/ai-analysis/history", params={"location": "  main   DECK "}).json()["total"] == 1
    assert c.get("/api/ai-analysis/history", params={"earlier_record_id": rid(c, "W30-0001"),
                                                      "later_record_id": rid(c, "W34-0001")}).json()["total"] == 1
    assert c.get("/api/ai-analysis/history", params={"classification": "WORSE"}).status_code == 422


def test_store_recovers_from_interrupted_and_corrupt_files(make_client, tmp_path):
    d = tmp_path / "analyses"
    c = make_client(MockConditionProvider("stable"), analysis_dir=d)
    aid = c.post("/api/ai-analysis/compare", json=pair(c)).json()["analysis"]["analysis_id"]
    (d / f"{aid}.deadbeef.tmp").write_text('{"partial": ', encoding="utf-8")  # interrupted write
    (d / "CA-20260101-ffffffffff.json").write_text("{broken", encoding="utf-8")
    s = AnalysisStore(d)
    s.load()
    assert s.get(aid) is not None
    assert s.status() == {"stored_analyses": 1, "unreadable_files": 1, "interrupted_writes": 1}
    assert (d / "CA-20260101-ffffffffff.json").exists()  # reported, never deleted


def test_inspection_data_is_untouched(make_client, data_root):
    before = {p: p.read_bytes() for p in data_root.rglob("*") if p.is_file() and "_analyses" not in p.parts}
    c = make_client(MockConditionProvider("deteriorated"))
    a = c.post("/api/ai-analysis/compare", json=pair(c)).json()["analysis"]
    sign_in(c, "reviewer")  # SV-03: a different reviewer approves
    assert c.post(f"/api/ai-analysis/{a['analysis_id']}/review", json={"status": "APPROVED", "reviewer": "x"}).status_code == 200
    after = {p: p.read_bytes() for p in data_root.rglob("*") if p.is_file() and "_analyses" not in p.parts}
    assert before == after
    rec = c.app.state.store.by_id[rid(c, "W30-0001")]
    assert rec.ai_suggested_location == "Main Deck"  # existing AI location unchanged


# The Groq adapter is covered in test_groq_condition_provider.py; the Gemini adapter (restored 2026-11-23) here.
# --------------------------------------------------------------------------- Gemini adapter (stubbed, offline)
genai = pytest.importorskip("google.genai")


class _FakeResponse:
    def __init__(self, parsed=None, text=""):
        self.parsed, self.text = parsed, text
        self.usage_metadata = None


@pytest.fixture
def gemini(monkeypatch):
    if not (AUTOMATION_ROOT / "src" / "vision_client.py").is_file():
        pytest.skip("automation code not present")
    from app.ai import gemini_provider as gp

    vc, cfg = gp._automation_modules(AUTOMATION_ROOT)
    # Never read automation/.env in tests: stub the loader and use a fake key (no network).
    monkeypatch.setattr(cfg, "load_provider_environment", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    return gp, vc


def _images():
    from app.ai.providers import ImageInput

    return (ImageInput("W41-0089", b"\xff\xd8e", "image/jpeg", "2026-10-18"),
            ImageInput("W42-0089", b"\xff\xd8l", "image/jpeg", "2026-10-25"))


def test_gemini_requires_explicit_live_approval(gemini, monkeypatch):
    gp, vc = gemini
    called = []
    monkeypatch.setattr(gp.GeminiConditionProvider, "_get_client", lambda *a: called.append(1))
    p = gp.GeminiConditionProvider(AUTOMATION_ROOT, allow_live=False, max_image_dimension=0)
    ok, msg = p.available()
    assert not ok and "approval" in msg
    assert p.model  # read from automation settings.yaml (no .env access)
    with pytest.raises(ProviderUnavailable):
        p.analyse(*_images(), "prompt")
    assert called == []


def test_gemini_adapter_sends_labelled_pair_and_maps_errors(gemini, monkeypatch):
    gp, vc = gemini
    p = gp.GeminiConditionProvider(AUTOMATION_ROOT, allow_live=True, max_image_dimension=0)
    sent = {}

    def fake_generate(model, contents, config):
        sent.update(model=model, contents=contents, config=config)
        return _FakeResponse(text=json.dumps(MOCK_SCENARIOS["stable"]))

    vc_mod, cfg = gp._automation_modules(AUTOMATION_ROOT)
    client = p._get_client(vc_mod, cfg)
    client._pace_sleep = lambda s: None
    monkeypatch.setattr(client._client.models, "generate_content", fake_generate)
    result = p.analyse(*_images(), "PROMPT TEXT")
    assert validate_assessment(result.raw).classification.value == "STABLE"
    c = sent["contents"]
    assert c[0].startswith("EARLIER") and "2026-10-18" in c[0]
    assert c[2].startswith("LATER") and "2026-10-25" in c[2]
    assert c[1].inline_data.data == b"\xff\xd8e" and c[3].inline_data.data == b"\xff\xd8l"  # originals, in order
    assert c[4] == "PROMPT TEXT"
    assert sent["config"].response_mime_type == "application/json"
    assert sent["config"].response_schema.__name__ == "ConditionAssessment"

    for raised, expected in [
        (vc.GeminiQuotaExhaustedError("quota", 60.0), ProviderQuotaExhausted),
        (vc.GeminiRateLimitedError("rpm", 5.0, "per_minute"), ProviderRateLimited),
        (vc.GeminiServiceUnavailableError("503"), ProviderUnavailable),
        (vc.GeminiRequestTimeoutError("slow"), ProviderTimeout),
    ]:
        monkeypatch.setattr(client, "_call_and_handle_errors", lambda payload, e=raised: (_ for _ in ()).throw(e))
        with pytest.raises(expected):
            p.analyse(*_images(), "PROMPT TEXT")


def test_importing_adapter_writes_nothing_into_automation():
    assert sys.dont_write_bytecode is False  # restored after the guarded import
