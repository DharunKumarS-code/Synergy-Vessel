"""Phase 5D -- dashboard-only Gemini model override (SYNERGY_AI_MODEL). Offline: fake key, stubbed client.

Restored 2026-11-23 with the Gemini condition provider; parse_ai_model is now provider-aware (Groq rules are
covered in test_groq_condition_provider.py)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.ai.providers import MOCK_SCENARIOS
from app.config import Settings, get_settings, parse_ai_model
from app.main import build_provider, create_app
from conftest import sign_in

AUTOMATION_ROOT = Path(__file__).resolve().parents[3] / "automation"
SETTINGS_YAML = AUTOMATION_ROOT / "config" / "settings.yaml"
pytestmark = pytest.mark.skipif(not SETTINGS_YAML.is_file(), reason="automation code not present")


def _yaml_model() -> str:
    return yaml.safe_load(SETTINGS_YAML.read_text(encoding="utf-8"))["model"]


@pytest.fixture
def offline(monkeypatch):
    from app.ai import gemini_provider as gp

    vc, cfg = gp._automation_modules(AUTOMATION_ROOT)
    monkeypatch.setattr(cfg, "load_provider_environment", lambda: None)  # never read automation/.env
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    return gp, vc, cfg


# --------------------------------------------------------------------------- parsing
@pytest.mark.parametrize("value", [None, "", "   "])
def test_absent_or_empty_override_keeps_default(value):
    assert parse_ai_model(value, "gemini") is None


@pytest.mark.parametrize("value,expected", [("gemini-3.6-flash", "gemini-3.6-flash"),
                                            ("  GEMINI-3.6-Flash ", "gemini-3.6-flash"),
                                            ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite")])
def test_valid_override_is_normalised(value, expected):
    assert parse_ai_model(value, "gemini") == expected


@pytest.mark.parametrize("value", ["gpt-4o", "gemini", "gemini-", "gemini-3.6 flash", "gemini-3.6-flash;rm",
                                   "../gemini-3.6-flash", "gemini-3.1-flash-image-preview", "gemini-" + "x" * 80])
def test_invalid_override_is_ignored(value):
    assert parse_ai_model(value, "gemini") is None


def test_get_settings_reads_environment(monkeypatch):
    monkeypatch.setenv("SYNERGY_AI_PROVIDER", "gemini")
    monkeypatch.delenv("SYNERGY_AI_MODEL", raising=False)
    assert get_settings().ai_model is None
    monkeypatch.setenv("SYNERGY_AI_MODEL", "gemini-3.6-flash")
    assert get_settings().ai_model == "gemini-3.6-flash"
    monkeypatch.setenv("SYNERGY_AI_MODEL", "not-a-model")
    assert get_settings().ai_model is None


# --------------------------------------------------------------------------- provider wiring
def test_default_model_unchanged_without_override(offline):
    p = build_provider(Settings(ai_provider="gemini"))
    assert p.model == _yaml_model()


def test_override_reaches_the_adapter_and_automation_is_untouched(offline):
    before = hashlib.sha256(SETTINGS_YAML.read_bytes()).hexdigest()
    p = build_provider(Settings(ai_provider="gemini", ai_allow_live=True, ai_model="gemini-3.6-flash"))
    assert p.model == "gemini-3.6-flash"
    assert p.available() == (True, "Gemini live analysis enabled (gemini-3.6-flash).")
    gp, vc, cfg = offline
    assert cfg.load_settings()["model"] == _yaml_model()          # automation still uses its own model
    assert hashlib.sha256(SETTINGS_YAML.read_bytes()).hexdigest() == before


def test_invalid_override_falls_back_to_default(offline):
    p = build_provider(Settings(ai_provider="gemini", ai_model="gemini-3.1-flash-image-preview"))
    assert p.model == _yaml_model()


def test_override_without_live_approval_stays_disabled(offline):
    p = build_provider(Settings(ai_provider="gemini", ai_model="gemini-3.6-flash"))
    ok, msg = p.available()
    assert not ok and "awaiting approval" in msg


def test_override_is_ignored_by_non_gemini_providers():
    assert build_provider(Settings(ai_provider="disabled", ai_model="gemini-3.6-flash")).name == "disabled"


# --------------------------------------------------------------------------- end-to-end (stubbed network)
class _Resp:
    def __init__(self, text):
        self.parsed, self.text, self.usage_metadata = None, text, None


def test_selected_model_is_sent_with_ordered_pair_and_unchanged_schema(offline, data_root, tmp_path):
    gp, vc, cfg = offline
    settings = Settings(data_root=data_root, analysis_dir=tmp_path / "analyses", ai_provider="gemini",
                        ai_allow_live=True, ai_model="gemini-3.6-flash")
    sent = []
    with TestClient(create_app(settings)) as c:
        sign_in(c, "reviewer")
        provider = c.app.state.analysis.provider
        client = provider._get_client(vc, cfg)
        client._pace_sleep = lambda s: None

        def fake_generate(model, contents, config):
            sent.append(dict(model=model, contents=contents, config=config))
            return _Resp(json.dumps(MOCK_SCENARIOS["stable"]))

        client._client.models.generate_content = fake_generate
        store = c.app.state.store
        rid = lambda iid: next(r.record_id for r in store.records if r.image_id == iid and r.source == "json_report")  # noqa: E731
        e, l = store.by_id[rid("W30-0001")], store.by_id[rid("W34-0001")]
        r = c.post("/api/ai-analysis/compare", json={"earlier_record_id": e.record_id, "later_record_id": l.record_id,
                                                     "acknowledge_warnings": True})
        assert r.status_code == 200, r.text
        a = r.json()["analysis"]

    assert len(sent) == 1 and sent[0]["model"] == "gemini-3.6-flash"
    contents = sent[0]["contents"]
    assert contents[0].startswith("EARLIER") and e.inspection_date.isoformat() in contents[0]
    assert contents[2].startswith("LATER") and l.inspection_date.isoformat() in contents[2]
    assert contents[1].inline_data is not None and contents[3].inline_data is not None
    assert sent[0]["config"].response_mime_type == "application/json"
    assert sent[0]["config"].response_schema.__name__ == "ConditionAssessment"
    assert a["model"] == "gemini-3.6-flash" and a["provider"] == "gemini" and a["simulated"] is False
    assert a["prompt_version"] == "condition-compare-v1"
    assert a["earlier"]["image_id"] == "W30-0001" and a["later"]["image_id"] == "W34-0001"
