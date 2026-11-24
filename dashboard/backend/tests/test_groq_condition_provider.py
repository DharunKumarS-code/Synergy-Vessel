"""AI Condition Analysis on Groq (replaces the Gemini adapter). Fully offline: every Groq request goes to an
httpx.MockTransport, a socket guard fails the test on any real network access, and automation/.env is never read
(the automation key loader is stubbed). Synthetic data and temporary folders only."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai import groq_provider as gp
from app.ai.providers import (
    MOCK_SCENARIOS,
    DisabledProvider,
    ImageInput,
    ProviderError,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.ai.schema import ConditionAssessment
from app.config import GROQ_VISION_MODELS, Settings, get_settings, parse_ai_model
from app.main import build_provider, create_app
from conftest import jpeg_bytes, sign_in
from test_location_intel import no_network  # noqa: F401  (autouse socket guard: no real network access)

AUTOMATION_ROOT = Path(__file__).resolve().parents[3] / "automation"
REAL_LOADER = gp._load_automation_environment          # kept before the autouse stub replaces it
FAKE_KEY = "gsk_" + "t" * 48           # test-only, never a real key
STABLE = json.dumps(MOCK_SCENARIOS["stable"])


@pytest.fixture(autouse=True)
def no_env_file(monkeypatch):
    """Never read automation/.env in tests: the loader is replaced; calls are recorded."""
    calls = []
    monkeypatch.setattr(gp, "_load_automation_environment", lambda root: calls.append(root))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(gp, "MIN_GAP_SECONDS", 0.0)
    return calls


class Groq:
    """A scripted stand-in for Groq's chat-completions endpoint."""

    def __init__(self, *responses):
        self.responses = list(responses) or [self.ok(STABLE)]
        self.requests: list[httpx.Request] = []

    @staticmethod
    def ok(content, usage=None):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}],
                                         "usage": usage or {"prompt_tokens": 4500, "completion_tokens": 300, "total_tokens": 4800}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)

    def body(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


def provider(groq=None, *, allow_live=True, model=None, sleeps=None, **kw):
    return gp.GroqConditionProvider(AUTOMATION_ROOT, allow_live=allow_live, model=model,
                                    transport=(groq or Groq()).transport,
                                    sleep=(sleeps.append if sleeps is not None else (lambda s: None)), **kw)


def images(e=b"\xff\xd8earlier", l=b"\xff\xd8later"):
    return ImageInput("W41-0047", e, "image/jpeg", "2026-10-18"), ImageInput("W42-0047", l, "image/jpeg", "2026-10-25")


# =========================================================================== configuration & enablement
def test_disabled_by_default_and_gemini_selects_the_gemini_provider(monkeypatch):
    for var in ("SYNERGY_AI_PROVIDER", "SYNERGY_AI_ALLOW_LIVE", "SYNERGY_AI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    s = get_settings()
    assert (s.ai_provider, s.ai_allow_live) == ("disabled", False)
    assert isinstance(build_provider(s), DisabledProvider)
    monkeypatch.setenv("SYNERGY_AI_PROVIDER", "gemini")                         # restored 2026-11-23
    assert get_settings().ai_provider == "gemini"
    monkeypatch.setenv("SYNERGY_AI_PROVIDER", "openai")                         # unknown -> disabled
    assert get_settings().ai_provider == "disabled"
    monkeypatch.setenv("SYNERGY_AI_PROVIDER", "groq")
    monkeypatch.setenv("SYNERGY_AI_ALLOW_LIVE", "1")
    s = get_settings()
    assert (s.ai_provider, s.ai_allow_live) == ("groq", True)
    p = build_provider(s)
    assert isinstance(p, gp.GroqConditionProvider) and (p.name, p.model, p.live, p.simulated) == ("groq", "qwen/qwen3.8-27b", True, False)


def test_live_requests_need_explicit_approval(no_env_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    groq = Groq()
    p = provider(groq, allow_live=False)
    ok, msg = p.available()
    assert not ok and "awaiting approval" in msg and "SYNERGY_AI_ALLOW_LIVE" in msg
    with pytest.raises(ProviderUnavailable):
        p.analyse(*images(), "prompt")
    assert groq.requests == [] and no_env_file == []                            # no request, .env never loaded


def test_missing_or_malformed_key_reports_unavailable_without_a_request(no_env_file, monkeypatch):
    groq = Groq()
    p = provider(groq)
    ok, msg = p.available()
    assert not ok and msg == "Groq is selected but its API key is not configured (GROQ_API_KEY)."
    assert no_env_file == [AUTOMATION_ROOT]                                     # the existing automation loader was asked
    with pytest.raises(ProviderUnavailable):
        p.analyse(*images(), "prompt")
    for bad in ("", "   ", "not-a-groq-key", "gsk_short", "gsk_" + "a" * 30 + " x"):
        monkeypatch.setenv("GROQ_API_KEY", bad)
        assert p.available()[0] is False
    assert groq.requests == []


def test_key_from_environment_or_loader_and_tidy_values(no_env_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", f'  "{FAKE_KEY}"\n')
    assert provider().available() == (True, "Groq live analysis enabled (qwen/qwen3.8-27b).")
    assert no_env_file == []                                                    # already in the environment: no .env read
    monkeypatch.delenv("GROQ_API_KEY")
    monkeypatch.setattr(gp, "_load_automation_environment", lambda root: monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY))
    assert provider().available()[0] is True                                     # found through the existing loader


def test_status_endpoint_shows_groq_and_never_the_key(data_root, tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    s = Settings(data_root=data_root, analysis_dir=tmp_path / "a", ai_provider="groq", ai_allow_live=True)
    with TestClient(create_app(s)) as c:
        sign_in(c, "viewer")                                       # SV-02
        r = c.get("/api/ai-analysis/status")
    body = r.json()
    assert (body["available"], body["provider"], body["model"], body["live"], body["simulated"]) == (True, "groq", "qwen/qwen3.8-27b", True, False)
    assert FAKE_KEY not in r.text and "gsk_" not in r.text
    monkeypatch.delenv("GROQ_API_KEY")
    with TestClient(create_app(s)) as c:
        sign_in(c, "viewer")
        body = c.get("/api/ai-analysis/status").json()
    assert body["available"] is False and "not configured" in body["message"]


@pytest.mark.parametrize("value,expected", [(None, None), ("", None), ("  QWEN/qwen3.8-27b ", "qwen/qwen3.8-27b"),
                                            ("meta-models/muse-spark-open", "meta-models/muse-spark-open"),
                                            ("openai/gpt-oss-20b", None),          # text-only: never assumed to see images
                                            ("llama-3.3-70b-versatile", None), ("gemini-3.8-flash", None), ("../x", None)])
def test_model_override_accepts_documented_vision_models_only(value, expected):
    assert parse_ai_model(value) == expected
    assert "qwen/qwen3.8-27b" in GROQ_VISION_MODELS


def test_model_override_reaches_the_request(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    p = build_provider(Settings(ai_provider="groq", ai_allow_live=True, ai_model="meta-models/muse-spark-open"))
    assert p.model == "meta-models/muse-spark-open"
    groq = Groq()
    p._transport = groq.transport
    p.analyse(*images(), "prompt")
    assert groq.body()["model"] == "meta-models/muse-spark-open" and "reasoning_effort" not in groq.body()
    assert build_provider(Settings(ai_provider="groq", ai_model="openai/gpt-oss-20b")).model == "qwen/qwen3.8-27b"


# =========================================================================== the request
def test_request_sends_the_labelled_pair_in_order_with_strict_json(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    groq = Groq()
    result = provider(groq).analyse(*images(), "PROMPT TEXT")
    assert len(groq.requests) == 1
    req = groq.requests[0]
    assert str(req.url) == "https://api.groq.com/openai/v1/chat/completions" and req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = groq.body()
    assert (body["model"], body["response_format"], body["reasoning_effort"], body["stream"]) == (
        "qwen/qwen3.8-27b", {"type": "json_object"}, "none", False)
    parts = body["messages"][0]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url", "text", "image_url", "text"]
    assert parts[0]["text"].startswith("EARLIER") and "2026-10-18" in parts[0]["text"]
    assert parts[2]["text"].startswith("LATER") and "2026-10-25" in parts[2]["text"]
    decode = lambda p: base64.b64decode(p["image_url"]["url"].split(",", 1)[1])  # noqa: E731
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert (decode(parts[1]), decode(parts[3])) == (b"\xff\xd8earlier", b"\xff\xd8later")   # originals, earlier first
    assert parts[4]["text"].startswith("PROMPT TEXT")
    assert json.loads(parts[4]["text"].split("JSON Schema (all listed fields, no additional fields):\n", 1)[1]) \
        == ConditionAssessment.model_json_schema()                            # unchanged response schema
    assert sum(p["type"] == "image_url" for p in parts) == 2                   # within Groq's 3-image limit
    assert result.raw == STABLE and result.usage == {"input_tokens": 4500, "output_tokens": 300, "total_tokens": 4800}


def test_large_images_are_downscaled_copies_small_ones_untouched(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    big, small = jpeg_bytes(7, size=(2400, 1800)), jpeg_bytes(8, size=(850, 637))
    groq = Groq()
    provider(groq).analyse(*images(big, small), "p")
    parts = groq.body()["messages"][0]["content"]
    sent = [base64.b64decode(parts[i]["image_url"]["url"].split(",", 1)[1]) for i in (1, 3)]
    assert max(Image.open(io.BytesIO(sent[0])).size) == 1024 and sent[1] == small
    assert big == jpeg_bytes(7, size=(2400, 1800))                             # the input bytes are not modified


# =========================================================================== failures
@pytest.mark.parametrize("response,expected", [
    (httpx.Response(429, headers={"retry-after": "17"}), ProviderRateLimited),
    (httpx.Response(401), ProviderRejected),
    (httpx.Response(403), ProviderRejected),
    (httpx.Response(404, json={"error": {"message": "model not found"}}), ProviderRejected),
    (httpx.Response(400, json={"error": {"code": "json_validate_failed"}}), ProviderError),
    (httpx.Response(200, json={"unexpected": True}), ProviderError),
    (httpx.Response(200, text="not json"), ProviderError),
    (httpx.TimeoutException("slow"), ProviderTimeout),
    (httpx.ConnectError("down"), ProviderUnavailable),
])
def test_provider_failures_map_to_safe_errors_without_retrying(monkeypatch, response, expected):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    groq = Groq(response)
    with pytest.raises(expected) as info:
        provider(groq).analyse(*images(), "p")
    assert len(groq.requests) == 1                                              # 4xx / timeouts are never retried
    assert FAKE_KEY not in str(info.value) and "gsk_" not in str(info.value)
    if expected is ProviderRateLimited:
        assert info.value.retry_after_seconds == 17.0


def test_server_errors_are_retried_a_bounded_number_of_times(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    sleeps = []
    groq = Groq(httpx.Response(503), httpx.Response(500), httpx.Response(502))
    with pytest.raises(ProviderUnavailable):
        provider(groq, sleeps=sleeps).analyse(*images(), "p")
    assert len(groq.requests) == 3 and sleeps == [2.0, 4.0]
    groq = Groq(httpx.Response(503), Groq.ok(STABLE))
    assert provider(groq).analyse(*images(), "p").raw == STABLE and len(groq.requests) == 2


def test_errors_and_logs_never_contain_the_key(monkeypatch, caplog):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    for r in (httpx.Response(401), httpx.Response(500), httpx.Response(400)):
        with pytest.raises(ProviderError):
            provider(Groq(r)).analyse(*images(), "p")
    assert FAKE_KEY not in caplog.text and "gsk_" not in caplog.text


# =========================================================================== end to end through the API
@pytest.fixture
def groq_app(data_root, tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    groq = Groq()
    s = Settings(data_root=data_root, analysis_dir=tmp_path / "analyses", ai_provider="groq", ai_allow_live=True)
    with TestClient(create_app(s)) as c:
        c.app.state.analysis.provider._transport = groq.transport
        yield c, groq, tmp_path / "analyses"


def _pair(c, earlier="W30-0001", later="W34-0001"):
    store = c.app.state.store
    rid = lambda iid: next(r.record_id for r in store.records if r.image_id == iid and r.source == "json_report")  # noqa: E731
    return {"earlier_record_id": rid(earlier), "later_record_id": rid(later), "acknowledge_warnings": True}


def _tree_hashes(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.relative_to(root).as_posix().startswith("_")}


def test_end_to_end_analysis_is_stored_separately_and_needs_human_review(groq_app, data_root):
    c, groq, analyses = groq_app
    before = _tree_hashes(data_root)
    records_before = [r.model_dump(mode="json") for r in c.app.state.store.records]
    sign_in(c, "reviewer")
    body = _pair(c)
    r = c.post("/api/ai-analysis/compare", json=body)
    assert r.status_code == 200, r.text
    a = r.json()["analysis"]
    assert (a["provider"], a["model"], a["simulated"], a["review"]["status"]) == ("groq", "qwen/qwen3.8-27b", False, "PENDING_REVIEW")
    assert a["human_review_required"] is True                                   # pair has location warnings -> review
    assert a["earlier"]["record_id"] == body["earlier_record_id"] and a["later"]["record_id"] == body["later_record_id"]
    assert [p.name for p in analyses.glob("*.json")] == [f"{a['analysis_id']}.json"]
    # de-duplication: the same pair is never sent again
    again = c.post("/api/ai-analysis/compare", json=body)
    assert again.json()["reused"] is True and len(groq.requests) == 1
    # a human review keeps the original AI result (SV-03: by a different reviewer than the one who ran it)
    sign_in(c, "reviewer")
    rv = c.post(f"/api/ai-analysis/{a['analysis_id']}/review", json={"status": "APPROVED", "note": "checked"})
    assert rv.status_code == 200 and rv.json()["ai_result"] == a["ai_result"]
    # historical inspection data and photographs are untouched
    assert _tree_hashes(data_root) == before
    assert [r.model_dump(mode="json") for r in c.app.state.store.records] == records_before
    audit = c.get("/api/admin/audit?limit=50")
    sign_in(c, "admin")
    assert "ai_analysis_run" in c.get("/api/admin/audit?limit=50").text and audit.status_code == 403


def test_invalid_model_output_is_rejected_and_nothing_is_saved(groq_app):
    c, groq, analyses = groq_app
    groq.responses = [Groq.ok('{"classification": "STABLE"}')]
    sign_in(c, "reviewer")
    r = c.post("/api/ai-analysis/compare", json=_pair(c))
    assert (r.status_code, r.json()["code"]) == (502, "invalid_ai_response")
    groq.responses = [Groq.ok("Sure! Here is the JSON: {...}")]
    r = c.post("/api/ai-analysis/compare", json=_pair(c))
    assert (r.status_code, r.json()["code"]) == (502, "invalid_ai_response")
    assert not analyses.exists() or not list(analyses.glob("*.json"))


def test_provider_failure_through_the_api_saves_nothing(groq_app):
    c, groq, analyses = groq_app
    groq.responses = [httpx.TimeoutException("slow")]
    sign_in(c, "reviewer")
    r = c.post("/api/ai-analysis/compare", json=_pair(c))
    assert (r.status_code, r.json()["code"]) == (504, "ai_timeout")
    assert not analyses.exists() or not list(analyses.glob("*.json"))


def test_authentication_is_required_and_no_request_is_made_without_it(groq_app):
    c, groq, _ = groq_app
    c.headers.pop("Authorization", None)
    r = c.post("/api/ai-analysis/compare", json=_pair(c))
    assert r.status_code == 401 and groq.requests == []


def test_pair_validation_still_blocks_before_any_request(groq_app):
    c, groq, _ = groq_app
    sign_in(c, "reviewer")
    p = _pair(c)
    same_record = {**p, "later_record_id": p["earlier_record_id"]}
    assert c.post("/api/ai-analysis/compare", json=same_record).status_code == 422          # request validation
    same_file = _pair(c, "W30-0002", "W34-0001")                                            # identical SHA-256
    r = c.post("/api/ai-analysis/compare", json=same_file)
    assert (r.status_code, r.json()["code"]) == (400, "same_image")                           # server-side pair check
    unacknowledged = {**p, "acknowledge_warnings": False}
    assert c.post("/api/ai-analysis/compare", json=unacknowledged).json()["code"] == "validation_warnings"
    assert groq.requests == []


def test_key_loader_reuses_the_automation_loader_and_writes_nothing(monkeypatch):
    """The real loader imports automation's src.config and calls its load_provider_environment -- here a stand-in
    module, so automation/.env is never read -- without writing bytecode into automation/."""
    seen = {}

    class FakeConfig:
        @staticmethod
        def load_provider_environment():
            seen["called"] = True
            seen["no_bytecode_during_import"] = sys.dont_write_bytecode

    def fake_import(name):
        seen["module"], seen["flag"] = name, sys.dont_write_bytecode
        return FakeConfig

    monkeypatch.setattr(gp.importlib, "import_module", fake_import)
    REAL_LOADER(AUTOMATION_ROOT)
    assert seen == {"module": "src.config", "flag": True, "called": True, "no_bytecode_during_import": False}
    assert sys.dont_write_bytecode is False


def test_automation_settings_and_pipeline_config_are_untouched(monkeypatch):
    """Switching condition analysis to Groq never changes the automation pipeline's own provider settings."""
    settings_yaml = AUTOMATION_ROOT / "config" / "settings.yaml"
    if not settings_yaml.is_file():
        pytest.skip("automation code not present")
    before = hashlib.sha256(settings_yaml.read_bytes()).hexdigest()
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    p = build_provider(Settings(ai_provider="groq", ai_allow_live=True, ai_model="qwen/qwen3.8-27b"))
    p._transport = Groq().transport
    p.analyse(*images(), "p")
    assert hashlib.sha256(settings_yaml.read_bytes()).hexdigest() == before
