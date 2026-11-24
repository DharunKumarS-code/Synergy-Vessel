"""Offline-only checks for the Gemini and Groq clients: construction, schema shape, request
construction, and prompt-length regression guard. No network call is made anywhere in this file
(genai.Client() and groq.Groq() do not connect at construction time) -- see README "Known
limitations" for what still needs a live key.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import vision_client as vc

FAKE_KEY = "fake-key-for-offline-test"


def test_schema_has_expected_fields_and_enum_values():
    fields = vc.ClassificationSchema.model_fields
    assert set(fields.keys()) == {
        "person_visible", "activity_identified", "activity_confidence",
        "ai_suggested_location", "location_confidence",
    }
    assert {m.value for m in vc.PersonVisible} == {"Yes", "No", "Uncertain"}
    assert {m.value for m in vc.Confidence} == {"High", "Medium", "Low", "Needs Review"}


def test_prompt_length_regression_guard():
    # Phase 3b's prose-only prompt was 3464 chars and produced ~77s/image average latency.
    # The Phase 3c schema-based prompt should be meaningfully shorter; this guards against
    # future edits silently reintroducing that bloat (the rule content itself now lives
    # primarily in ClassificationSchema's field descriptions, not here).
    assert len(vc.PROMPT) < 1200


def test_client_construction_does_not_make_a_network_call():
    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY)
    assert client._max_image_dimension == 768  # image_prep.DEFAULT_MAX_DIMENSION
    assert client._generate_config.response_mime_type == "application/json"
    assert client._generate_config.response_schema is vc.ClassificationSchema


def test_invalid_thinking_level_rejected_without_a_network_call():
    with pytest.raises(vc.VisionAPIError, match="Unknown thinking_level"):
        vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, thinking_level="ULTRA")


def test_valid_thinking_level_accepted():
    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, thinking_level="low")
    assert client._generate_config.thinking_config.thinking_level.name == "LOW"


def test_max_image_dimension_zero_disables_resizing():
    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0)
    assert client._max_image_dimension == 0


class _FakeModalityTokenCount:
    def __init__(self, modality, token_count):
        self.modality = modality
        self.token_count = token_count


class _FakeUsageMetadata:
    def __init__(self, text_tokens, image_tokens, output_tokens, thoughts_tokens, total_tokens):
        self.prompt_tokens_details = [
            _FakeModalityTokenCount("MediaModality.TEXT", text_tokens),
            _FakeModalityTokenCount("MediaModality.IMAGE", image_tokens),
        ]
        self.candidates_token_count = output_tokens
        self.thoughts_token_count = thoughts_tokens
        self.total_token_count = total_tokens


class _FakeResponse:
    def __init__(self, parsed, usage_metadata):
        self.parsed = parsed
        self.usage_metadata = usage_metadata
        self.text = None


def test_usage_info_extracts_token_breakdown_from_a_fake_response():
    fake_meta = _FakeUsageMetadata(
        text_tokens=150, image_tokens=258, output_tokens=25, thoughts_tokens=100, total_tokens=533
    )
    fake_response = _FakeResponse(parsed=None, usage_metadata=fake_meta)
    usage = vc.GeminiVisionClient._usage_info(fake_response, latency_seconds=4.2)
    assert usage.latency_seconds == 4.2
    assert usage.input_text_tokens == 150
    assert usage.input_image_tokens == 258
    assert usage.output_tokens == 25
    assert usage.thoughts_tokens == 100
    assert usage.total_tokens == 533


def test_usage_info_handles_missing_usage_metadata_gracefully():
    fake_response = _FakeResponse(parsed=None, usage_metadata=None)
    usage = vc.GeminiVisionClient._usage_info(fake_response, latency_seconds=1.0)
    assert usage.latency_seconds == 1.0
    assert usage.total_tokens is None


def test_classify_with_usage_makes_exactly_one_underlying_call(monkeypatch):
    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY)
    fake_result = vc.ClassificationSchema(
        person_visible=vc.PersonVisible.NO,
        activity_identified="No activity observed",
        activity_confidence=vc.Confidence.HIGH,
        ai_suggested_location="Main Deck",
        location_confidence=vc.Confidence.HIGH,
    )
    fake_meta = _FakeUsageMetadata(100, 258, 20, 0, 378)
    fake_response = _FakeResponse(parsed=fake_result, usage_metadata=fake_meta)

    call_count = 0

    def fake_call_and_handle_errors(image_bytes):
        nonlocal call_count
        call_count += 1
        return fake_response

    monkeypatch.setattr(client, "_call_and_handle_errors", fake_call_and_handle_errors)
    result_dict, usage = client.classify_with_usage(b"fake-image-bytes")

    assert call_count == 1  # critical: must never make two API calls to get usage + result
    assert result_dict["ai_suggested_location"] == "Main Deck"
    assert usage.total_tokens == 378
    assert usage.latency_seconds >= 0


def test_is_retryable_distinguishes_server_vs_client_errors():
    from google.genai import errors

    server_err = errors.ServerError(code=503, response_json={"error": {"message": "x", "status": "UNAVAILABLE"}})
    client_err = errors.ClientError(code=429, response_json={"error": {"message": "x", "status": "RESOURCE_EXHAUSTED"}})
    assert vc._is_retryable(server_err) is True
    assert vc._is_retryable(client_err) is False
    assert vc._is_retryable(RuntimeError("unrelated")) is False


# ---------------------------------------------------------------------------
# Phase V: confirmed Gemini daily-quota-exhaustion detection and global stop signaling.
# ---------------------------------------------------------------------------


def _daily_quota_exhausted_body(retry_delay: str | None = "6843s") -> dict:
    # Shaped exactly like the real response captured during this project's first live quota
    # exhaustion (Phase U/T investigation) -- status RESOURCE_EXHAUSTED, a QuotaFailure
    # violation naming the per-day quota, and a RetryInfo with the suggested delay.
    details = [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "description": "Generate content requests per day limit exceeded on Free Tier.",
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        }]},
    ]
    if retry_delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota... Generate content requests per day limit exceeded on Free Tier.",
        "details": details,
    }}


def test_confirmed_daily_quota_exhaustion_is_detected():
    from google.genai import errors

    exc = errors.ClientError(code=429, response_json=_daily_quota_exhausted_body())
    assert vc._is_confirmed_daily_quota_exhausted(exc) is True
    assert vc._extract_gemini_retry_delay_seconds(exc) == 6843.0


def test_ordinary_429_without_daily_quota_signature_is_not_confirmed():
    from google.genai import errors

    # a generic RESOURCE_EXHAUSTED with no per-day evidence at all (e.g. a per-minute limit)
    exc = errors.ClientError(code=429, response_json={
        "error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "rate limit exceeded", "details": []}
    })
    assert vc._is_confirmed_daily_quota_exhausted(exc) is False


def test_non_resource_exhausted_429_is_not_confirmed():
    from google.genai import errors

    exc = errors.ClientError(code=429, response_json={"error": {"status": "FAILED_PRECONDITION", "message": "x"}})
    assert vc._is_confirmed_daily_quota_exhausted(exc) is False


def test_503_server_error_is_never_confirmed_as_quota_exhaustion():
    from google.genai import errors

    exc = errors.ServerError(code=503, response_json={"error": {"message": "x", "status": "UNAVAILABLE"}})
    assert vc._is_confirmed_daily_quota_exhausted(exc) is False


def test_plain_exception_is_never_confirmed_as_quota_exhaustion():
    assert vc._is_confirmed_daily_quota_exhausted(RuntimeError("unrelated")) is False


@pytest.mark.parametrize("malformed_body", [
    {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": None, "details": "not-a-list"}},
    {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "x", "details": [{"violations": "not-a-list"}]}},
    {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},  # no message, no details key at all
])
def test_malformed_quota_metadata_handled_safely_without_raising(malformed_body):
    from google.genai import errors

    exc = errors.ClientError(code=429, response_json=malformed_body)
    assert vc._is_confirmed_daily_quota_exhausted(exc) is False  # never raises, never guesses True
    assert vc._extract_gemini_retry_delay_seconds(exc) is None


def test_retry_delay_extraction_handles_missing_retry_info():
    from google.genai import errors

    exc = errors.ClientError(code=429, response_json=_daily_quota_exhausted_body(retry_delay=None))
    assert vc._extract_gemini_retry_delay_seconds(exc) is None


def test_call_and_handle_errors_raises_quota_exhausted_error_type(monkeypatch):
    from google.genai import errors

    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY)
    exc = errors.ClientError(code=429, response_json=_daily_quota_exhausted_body())
    monkeypatch.setattr(client, "_call", lambda image_bytes: (_ for _ in ()).throw(exc))

    with pytest.raises(vc.GeminiQuotaExhaustedError) as exc_info:
        client._call_and_handle_errors(b"fake-image-bytes")
    assert exc_info.value.retry_delay_seconds == 6843.0
    assert FAKE_KEY not in str(exc_info.value)


def test_call_and_handle_errors_raises_plain_runtime_error_for_non_quota_429(monkeypatch):
    from google.genai import errors

    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY)
    exc = errors.ClientError(code=429, response_json={
        "error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "rate limit exceeded", "details": []}
    })
    monkeypatch.setattr(client, "_call", lambda image_bytes: (_ for _ in ()).throw(exc))

    with pytest.raises(RuntimeError) as exc_info:
        client._call_and_handle_errors(b"fake-image-bytes")
    assert not isinstance(exc_info.value, vc.GeminiQuotaExhaustedError)


def test_call_and_handle_errors_503_still_raises_plain_runtime_error_not_quota(monkeypatch):
    # 503s are retried internally by _call()'s own tenacity decorator; this test exercises
    # _call_and_handle_errors() directly (bypassing that retry) to confirm that even a raw
    # ServerError reaching this layer is never mistaken for quota exhaustion.
    from google.genai import errors

    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY)
    exc = errors.ServerError(code=503, response_json={"error": {"message": "x", "status": "UNAVAILABLE"}})
    monkeypatch.setattr(client, "_call", lambda image_bytes: (_ for _ in ()).throw(exc))

    with pytest.raises(RuntimeError) as exc_info:
        client._call_and_handle_errors(b"fake-image-bytes")
    assert not isinstance(exc_info.value, vc.GeminiQuotaExhaustedError)


def test_503_is_actually_retried_by_the_real_tenacity_decorator_and_recovers(monkeypatch):
    # Phase X: prior 503 tests only exercised _is_retryable() in isolation, or bypassed the
    # @retry decorator by monkeypatching _call() itself -- neither proves the real decorated
    # _call() actually loops and recovers. This mocks the underlying SDK call two layers down
    # and lets the genuine tenacity wrapper run (wait overridden to 0 so the test stays fast).
    from google.genai import errors

    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0)
    client._call.retry.wait = lambda retry_state: 0
    call_count = 0
    sentinel = object()

    def flaky_generate_content(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise errors.ServerError(code=503, response_json={"error": {"message": "x", "status": "UNAVAILABLE"}})
        return sentinel

    monkeypatch.setattr(client._client.models, "generate_content", flaky_generate_content)
    result = client._call(b"fake-image-bytes")

    assert call_count == 3  # 2 failed attempts + 1 success -- the decorator genuinely retried
    assert result is sentinel


def test_503_exhausts_all_five_attempts_then_reraises_the_server_error(monkeypatch):
    from google.genai import errors

    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0)
    client._call.retry.wait = lambda retry_state: 0
    call_count = 0

    def always_503(**kwargs):
        nonlocal call_count
        call_count += 1
        raise errors.ServerError(code=503, response_json={"error": {"message": "x", "status": "UNAVAILABLE"}})

    monkeypatch.setattr(client._client.models, "generate_content", always_503)
    with pytest.raises(errors.ServerError):
        client._call(b"fake-image-bytes")
    assert call_count == 5  # stop_after_attempt(5) -- confirmed exactly, not "at least"


def test_classify_image_sets_quota_exhausted_fields_on_classification_result():
    from src import classifier

    class _QuotaExhaustedClient(vc.VisionClient):
        def classify(self, image_bytes):
            raise vc.GeminiQuotaExhaustedError("quota exhausted while processing x.jpg", 6843.0)

    result = classifier.classify_image(_QuotaExhaustedClient(), b"fake-image-bytes")
    assert result.quota_exhausted is True
    assert result.retry_delay_seconds == 6843.0
    assert result.activity_identified == classifier.NEEDS_REVIEW
    assert result.ai_suggested_location == classifier.NEEDS_REVIEW
    assert result.error is not None


def test_classify_image_ordinary_error_does_not_set_quota_exhausted():
    from src import classifier

    class _OrdinaryFailureClient(vc.VisionClient):
        def classify(self, image_bytes):
            raise RuntimeError("some other transient failure")

    result = classifier.classify_image(_OrdinaryFailureClient(), b"fake-image-bytes")
    assert result.quota_exhausted is False
    assert result.retry_delay_seconds is None


# ---------------------------------------------------------------------------
# GroqVisionClient -- same offline-only guarantees as the Gemini tests above.
# ---------------------------------------------------------------------------

GROQ_FAKE_KEY = "fake-groq-key-for-offline-test"


def _groq_http_error(error_cls, status_code: int):
    import httpx

    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(status_code, request=req)
    return error_cls("boom", response=resp, body=None)


class _FakeGroqMessage:
    def __init__(self, content):
        self.content = content


class _FakeGroqChoice:
    def __init__(self, content):
        self.message = _FakeGroqMessage(content)


class _FakeGroqUsage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _FakeGroqResponse:
    def __init__(self, content, usage=None):
        self.choices = [_FakeGroqChoice(content)]
        self.usage = usage


_VALID_SCHEMA_JSON = (
    '{"person_visible": "No", "activity_identified": "No activity observed", '
    '"activity_confidence": "High", "ai_suggested_location": "Main Deck", '
    '"location_confidence": "High"}'
)


def test_groq_client_construction_does_not_make_a_network_call():
    client = vc.GroqVisionClient(model="qwen/qwen3.8-27b", api_key=GROQ_FAKE_KEY)
    assert client._model == "qwen/qwen3.8-27b"
    # Phase G: explicit instruct-mode pinning is now the default, not an omitted parameter.
    assert client._reasoning_effort == "none"
    assert client._reasoning_format == "hidden"


def test_groq_missing_api_key_raises_without_a_network_call(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(vc.VisionAPIError, match="No Groq API key found"):
        vc.GroqVisionClient(model="qwen/qwen3.8-27b")


def test_groq_request_uses_base64_data_uri_schema_and_reasoning_effort(monkeypatch):
    client = vc.GroqVisionClient(
        model="qwen/qwen3.8-27b", api_key=GROQ_FAKE_KEY, reasoning_effort="low"
    )
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeGroqResponse(_VALID_SCHEMA_JSON)

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    client.classify(b"fake-image-bytes")

    assert captured["model"] == "qwen/qwen3.8-27b"
    assert captured["reasoning_effort"] == "low"
    content = captured["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image_url")
    assert image_block["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert captured["response_format"]["json_schema"]["schema"] == vc._build_groq_strict_schema()


def test_groq_prompt_includes_addendum_and_gemini_prompt_is_untouched(monkeypatch):
    # Phase B isolation check: the Groq-only addendum must never leak into Gemini's request, and
    # PROMPT itself (Gemini's already-tuned text) must be byte-identical to what it was before --
    # the addendum is concatenated only inside GroqVisionClient._call.
    client = vc.GroqVisionClient(model="qwen/qwen3.8-27b", api_key=GROQ_FAKE_KEY)
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeGroqResponse(_VALID_SCHEMA_JSON)

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    client.classify(b"fake-image-bytes")

    text_block = next(b for b in captured["messages"][0]["content"] if b["type"] == "text")
    assert text_block["text"] == f"{vc.PROMPT}\n\n{vc.GROQ_PROMPT_ADDENDUM}"
    assert "GROQ_PROMPT_ADDENDUM" not in vc.PROMPT  # sanity: addendum text never merged into PROMPT itself


def test_groq_addendum_covers_person_activity_and_location_rules():
    # regression guard for the specific failure modes the live Week 30/001 Groq call exposed:
    # false person detection, non-vocabulary activity wording, and an invented location label.
    addendum = vc.GROQ_PROMPT_ADDENDUM
    assert "shadows" in addendum and "railings" in addendum  # never infer a person from these
    assert "A person is visible; specific task not confirmed" in addendum
    assert "Industrial Vessel Deck" in addendum  # named explicitly as a forbidden invented label
    assert "Main Deck" in addendum
    assert "High confidence" in addendum


def test_groq_addendum_instructs_json_only_response():
    # Phase E: regression guard for the live 400 json_validate_failed call (2026-11-12), where
    # the model emitted a free-text "note" instead of the schema -- the addendum must explicitly
    # forbid any commentary outside the JSON object.
    addendum = vc.GROQ_PROMPT_ADDENDUM
    assert "ONLY the JSON object" in addendum
    assert "commentary" in addendum


# ---------------------------------------------------------------------------
# Phase E: Groq strict-mode JSON Schema compatibility.
# ---------------------------------------------------------------------------


def test_groq_strict_schema_has_no_dollar_ref_or_defs():
    # strict mode (the OpenAI-compatible contract Groq's API mirrors) does not support a "$ref"
    # with sibling keys (e.g. "description") alongside it -- Pydantic's model_json_schema()
    # produces exactly that for every enum field, so _build_groq_strict_schema() must inline them.
    schema = vc._build_groq_strict_schema()
    assert "$defs" not in schema
    for prop in schema["properties"].values():
        assert "$ref" not in prop


def test_groq_strict_schema_has_additional_properties_false():
    schema = vc._build_groq_strict_schema()
    assert schema["additionalProperties"] is False


def test_groq_strict_schema_requires_every_property():
    schema = vc._build_groq_strict_schema()
    assert set(schema["required"]) == set(schema["properties"].keys())


def test_groq_strict_schema_preserves_all_five_fields_and_enum_values():
    schema = vc._build_groq_strict_schema()
    assert set(schema["properties"].keys()) == {
        "person_visible", "activity_identified", "activity_confidence",
        "ai_suggested_location", "location_confidence",
    }
    assert set(schema["properties"]["person_visible"]["enum"]) == {"Yes", "No", "Uncertain"}
    assert set(schema["properties"]["activity_confidence"]["enum"]) == {
        "High", "Medium", "Low", "Needs Review",
    }
    assert set(schema["properties"]["location_confidence"]["enum"]) == {
        "High", "Medium", "Low", "Needs Review",
    }
    # free-text fields keep their full prose description, not just a bare {"type": "string"}
    assert len(schema["properties"]["activity_identified"]["description"]) > 100
    assert len(schema["properties"]["ai_suggested_location"]["description"]) > 100


def test_groq_strict_schema_has_no_nullable_fields():
    # none of the five fields are optional in this project's design -- confirms there is no
    # anyOf-with-null construct to translate for strict-mode nullable support.
    schema = vc._build_groq_strict_schema()
    for prop in schema["properties"].values():
        assert "anyOf" not in prop
        assert prop.get("type") != "null"


def test_gemini_request_construction_is_unaffected_by_groq_strict_schema(monkeypatch):
    # Phase E must not touch Gemini at all -- it still passes the raw ClassificationSchema
    # class (native response_schema), never the Groq-specific strict dict.
    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY)
    assert client._generate_config.response_schema is vc.ClassificationSchema


def test_groq_reasoning_effort_and_format_pinned_by_default(monkeypatch):
    # Phase G: explicit instruct-mode configuration is now the default -- pinning, not a fix for
    # the Phase F structured-output failures (Groq's own docs already state "none" is this
    # model's default; this only makes that pin explicit and immune to a future default change).
    client = vc.GroqVisionClient(model="qwen/qwen3.8-27b", api_key=GROQ_FAKE_KEY)
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeGroqResponse(_VALID_SCHEMA_JSON)

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    client.classify(b"fake-image-bytes")

    assert captured["reasoning_effort"] == "none"
    assert captured["reasoning_format"] == "hidden"
    # strict structured output must remain unchanged by this phase
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert captured["response_format"]["json_schema"]["schema"] == vc._build_groq_strict_schema()


def test_groq_reasoning_params_omitted_when_explicitly_set_to_none(monkeypatch):
    client = vc.GroqVisionClient(
        model="qwen/qwen3.8-27b", api_key=GROQ_FAKE_KEY,
        reasoning_effort=None, reasoning_format=None,
    )
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeGroqResponse(_VALID_SCHEMA_JSON)

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    client.classify(b"fake-image-bytes")

    assert "reasoning_effort" not in captured
    assert "reasoning_format" not in captured


def test_groq_parse_response_validates_against_classification_schema():
    response = _FakeGroqResponse(_VALID_SCHEMA_JSON)
    result = vc.GroqVisionClient._parse_response(response)
    assert result["ai_suggested_location"] == "Main Deck"
    assert result["person_visible"] == "No"


def test_groq_parse_response_rejects_malformed_json():
    response = _FakeGroqResponse('{"not": "schema-shaped"}')
    with pytest.raises(Exception):  # noqa: B017 -- any pydantic ValidationError is the point
        vc.GroqVisionClient._parse_response(response)


def test_groq_usage_info_extracts_available_fields_and_leaves_rest_none():
    usage = _FakeGroqUsage(prompt_tokens=120, completion_tokens=30, total_tokens=150)
    response = _FakeGroqResponse(_VALID_SCHEMA_JSON, usage=usage)
    info = vc.GroqVisionClient._usage_info(response, latency_seconds=2.5)
    assert info.latency_seconds == 2.5
    assert info.output_tokens == 30
    assert info.total_tokens == 150
    assert info.input_text_tokens is None
    assert info.input_image_tokens is None
    assert info.thoughts_tokens is None


def test_groq_usage_info_handles_missing_usage_gracefully():
    response = _FakeGroqResponse(_VALID_SCHEMA_JSON, usage=None)
    info = vc.GroqVisionClient._usage_info(response, latency_seconds=1.0)
    assert info.latency_seconds == 1.0
    assert info.total_tokens is None


def test_groq_classify_with_usage_makes_exactly_one_underlying_call(monkeypatch):
    client = vc.GroqVisionClient(model="qwen/qwen3.8-27b", api_key=GROQ_FAKE_KEY)
    fake_response = _FakeGroqResponse(
        _VALID_SCHEMA_JSON, usage=_FakeGroqUsage(100, 20, 120)
    )
    call_count = 0

    def fake_call_and_handle_errors(image_bytes):
        nonlocal call_count
        call_count += 1
        return fake_response

    monkeypatch.setattr(client, "_call_and_handle_errors", fake_call_and_handle_errors)
    result_dict, usage = client.classify_with_usage(b"fake-image-bytes")

    assert call_count == 1  # critical: must never make two API calls to get usage + result
    assert result_dict["ai_suggested_location"] == "Main Deck"
    assert usage.total_tokens == 120
    assert usage.latency_seconds >= 0


def test_is_retryable_groq_distinguishes_server_vs_client_errors():
    import groq

    server_err = _groq_http_error(groq.InternalServerError, 503)
    rate_limit_err = _groq_http_error(groq.RateLimitError, 429)
    bad_request_err = _groq_http_error(groq.BadRequestError, 400)
    assert vc._is_retryable_groq(server_err) is True
    assert vc._is_retryable_groq(rate_limit_err) is False
    assert vc._is_retryable_groq(bad_request_err) is False
    assert vc._is_retryable_groq(RuntimeError("unrelated")) is False


def test_groq_400_json_validate_failed_is_not_retried_and_propagates(monkeypatch):
    # regression test for the live Week 30/2026-08-02_001.jpg call (2026-11-12) that failed with
    # HTTP 400 json_validate_failed after the model emitted a free-text note instead of JSON.
    import groq

    client = vc.GroqVisionClient(model="qwen/qwen3.8-27b", api_key=GROQ_FAKE_KEY)
    call_count = 0

    def fake_create(**kwargs):
        nonlocal call_count
        call_count += 1
        raise _groq_http_error(groq.BadRequestError, 400)

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    with pytest.raises(RuntimeError):
        client.classify(b"fake-image-bytes")
    assert call_count == 1  # critical: a 400 must never be retried


def test_groq_api_key_never_appears_in_exception_text(monkeypatch):
    client = vc.GroqVisionClient(model="qwen/qwen3.8-27b", api_key=GROQ_FAKE_KEY)

    def fake_create(**kwargs):
        raise RuntimeError(f"request failed, key={GROQ_FAKE_KEY}")

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    with pytest.raises(RuntimeError) as exc_info:
        client.classify(b"fake-image-bytes")
    assert GROQ_FAKE_KEY not in str(exc_info.value)
    assert "REDACTED" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Provider selection (build_vision_client) -- Gemini stays the default.
# ---------------------------------------------------------------------------


def test_build_vision_client_dry_run_returns_null_client_regardless_of_provider():
    assert isinstance(vc.build_vision_client("gemini", "unused-model", dry_run=True), vc.NullVisionClient)
    assert isinstance(vc.build_vision_client("groq", "unused-model", dry_run=True), vc.NullVisionClient)


def test_build_vision_client_selects_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    client = vc.build_vision_client("gemini", "gemini-3.8-flash", dry_run=False)
    assert isinstance(client, vc.GeminiVisionClient)


def test_build_vision_client_selects_groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", GROQ_FAKE_KEY)
    # P2-9: a live Groq client now requires rate_limit.groq_requests_per_minute
    client = vc.build_vision_client("groq", "qwen/qwen3.8-27b", dry_run=False,
                                    rate_limit={"groq_requests_per_minute": 30})
    assert isinstance(client, vc.GroqVisionClient)


def test_build_vision_client_rejects_unknown_provider():
    with pytest.raises(vc.VisionAPIError, match="Unsupported provider"):
        vc.build_vision_client("some-other-provider", "x", dry_run=False)


# ---------------------------------------------------------------------------
# Phase AE3: configurable 5xx retry policy with jitter + GeminiServiceUnavailableError.
# Every test here mocks generate_content two layers down and overrides the per-instance wait to
# 0 -- the real tenacity wrapper runs, nothing sleeps for real, nothing touches the network.
# ---------------------------------------------------------------------------


def _server_error_503():
    from google.genai import errors

    return errors.ServerError(code=503, response_json={"error": {"message": "x", "status": "UNAVAILABLE"}})


def _counting_generate_content(fail_with, succeed_after=None):
    """Returns (fn, calls, sentinel): fn raises fail_with() on every call, or only on the first
    succeed_after calls and then returns the sentinel."""
    calls = {"n": 0}
    sentinel = object()

    def fn(**kwargs):
        calls["n"] += 1
        if succeed_after is not None and calls["n"] > succeed_after:
            return sentinel
        raise fail_with()

    return fn, calls, sentinel


def _client_no_wait(**kwargs):
    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0, **kwargs)
    client._call.retry.wait = lambda retry_state: 0
    return client


def test_default_retry_policy_is_exactly_five_attempts():
    policy = vc.RetryPolicy()
    assert policy.max_retries == 4
    assert policy.max_attempts == 5
    assert (policy.backoff_seconds, policy.max_backoff_seconds, policy.jitter_seconds) == (4, 60, 4)


def test_default_client_makes_exactly_five_attempts_on_persistent_503(monkeypatch):
    client = _client_no_wait()
    fn, calls, _ = _counting_generate_content(_server_error_503)
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiServiceUnavailableError):
        client._call_and_handle_errors(b"fake-image-bytes")
    assert calls["n"] == 5


def test_project_settings_yaml_also_yields_exactly_five_attempts(monkeypatch):
    import yaml

    settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    rate_limit = yaml.safe_load(settings_path.read_text(encoding="utf-8"))["rate_limit"]
    policy = vc.RetryPolicy.from_settings(rate_limit)
    assert policy == vc.RetryPolicy()  # the shipped config matches the approved defaults
    assert rate_limit["circuit_breaker_threshold"] == 2
    client = _client_no_wait(retry_policy=policy)
    fn, calls, _ = _counting_generate_content(_server_error_503)
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiServiceUnavailableError):
        client._call_and_handle_errors(b"fake-image-bytes")
    assert calls["n"] == 5


def test_503_recovery_after_temporary_failure_returns_the_response(monkeypatch):
    client = _client_no_wait()
    fn, calls, sentinel = _counting_generate_content(_server_error_503, succeed_after=3)
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    assert client._call_and_handle_errors(b"fake-image-bytes") is sentinel
    assert calls["n"] == 4


def test_max_retries_from_settings_controls_attempt_count(monkeypatch):
    client = _client_no_wait(retry_policy=vc.RetryPolicy.from_settings({"max_retries": 2}))
    fn, calls, _ = _counting_generate_content(_server_error_503)
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiServiceUnavailableError):
        client._call_and_handle_errors(b"fake-image-bytes")
    assert calls["n"] == 3


def test_retry_policy_from_settings_uses_defaults_and_ignores_unrelated_keys():
    assert vc.RetryPolicy.from_settings(None) == vc.RetryPolicy()
    assert vc.RetryPolicy.from_settings({}) == vc.RetryPolicy()
    policy = vc.RetryPolicy.from_settings({"requests_per_minute": 15, "circuit_breaker_threshold": 2,
                                           "backoff_seconds": 1})
    assert policy.backoff_seconds == 1 and policy.max_retries == 4


@pytest.mark.parametrize("bad", [
    {"max_retries": -1}, {"max_retries": 2.5}, {"max_retries": True}, {"backoff_seconds": "4"},
    {"jitter_seconds": -0.1}, {"backoff_seconds": 10, "max_backoff_seconds": 5},
])
def test_invalid_retry_settings_rejected_without_a_network_call(bad):
    with pytest.raises(vc.VisionAPIError):
        vc.RetryPolicy.from_settings(bad)


def test_jittered_backoff_waits_stay_within_bounds():
    import random
    from types import SimpleNamespace

    wait = vc.RetryPolicy().wait_strategy()
    random.seed(12345)
    for attempt in range(1, 9):
        base = 4 * 2 ** (attempt - 1)
        for _ in range(50):
            w = wait(SimpleNamespace(attempt_number=attempt))
            assert min(base, 60) <= w <= min(base + 4, 60)


def test_jitter_actually_randomizes_the_wait():
    from types import SimpleNamespace

    wait = vc.RetryPolicy().wait_strategy()
    samples = {wait(SimpleNamespace(attempt_number=1)) for _ in range(20)}
    assert len(samples) > 1


def test_retry_configuration_is_per_instance():
    a = vc.GeminiVisionClient(model="m", api_key=FAKE_KEY, retry_policy=vc.RetryPolicy(max_retries=1))
    b = vc.GeminiVisionClient(model="m", api_key=FAKE_KEY, retry_policy=vc.RetryPolicy(max_retries=6))
    assert a._call.retry is not b._call.retry
    a._call.retry.wait = lambda retry_state: 0  # must not leak into b or the class decorator
    assert b._call.retry.wait is not a._call.retry.wait
    assert vc.GeminiVisionClient._call.retry.wait is not a._call.retry.wait


def test_daily_quota_429_is_never_retried_and_still_raises_quota_error(monkeypatch):
    from google.genai import errors

    client = _client_no_wait()
    fn, calls, _ = _counting_generate_content(
        lambda: errors.ClientError(code=429, response_json=_daily_quota_exhausted_body()))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiQuotaExhaustedError) as exc_info:
        client._call_and_handle_errors(b"fake-image-bytes")
    assert calls["n"] == 1
    assert exc_info.value.retry_delay_seconds == 6843.0
    assert not isinstance(exc_info.value, vc.GeminiServiceUnavailableError)


def test_400_is_not_retried_and_not_reported_as_service_unavailable(monkeypatch):
    from google.genai import errors

    client = _client_no_wait()
    fn, calls, _ = _counting_generate_content(
        lambda: errors.ClientError(code=400, response_json={"error": {"message": "bad", "status": "INVALID_ARGUMENT"}}))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(RuntimeError) as exc_info:
        client._call_and_handle_errors(b"fake-image-bytes")
    assert calls["n"] == 1
    assert not isinstance(exc_info.value, (vc.GeminiServiceUnavailableError, vc.GeminiQuotaExhaustedError))


def test_service_unavailable_error_redacts_the_api_key(monkeypatch):
    from google.genai import errors

    client = _client_no_wait()
    fn, _, _ = _counting_generate_content(lambda: errors.ServerError(
        code=503, response_json={"error": {"message": "echo " + FAKE_KEY, "status": "UNAVAILABLE"}}))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiServiceUnavailableError) as exc_info:
        client._call_and_handle_errors(b"fake-image-bytes")
    assert FAKE_KEY not in str(exc_info.value)
    assert "***REDACTED***" in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_keyboard_interrupt_during_backoff_sleep_propagates_unwrapped(monkeypatch):
    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0)

    def interrupted_sleep(seconds):
        raise KeyboardInterrupt()

    client._call.retry.sleep = interrupted_sleep
    fn, calls, _ = _counting_generate_content(_server_error_503)
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(KeyboardInterrupt):
        client._call_and_handle_errors(b"fake-image-bytes")
    assert calls["n"] == 1  # interrupted during the first backoff wait, never retried


def test_build_vision_client_passes_rate_limit_to_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    client = vc.build_vision_client("gemini", "gemini-3.8-flash", dry_run=False,
                                    rate_limit={"max_retries": 1, "backoff_seconds": 1})
    assert client._retry_policy == vc.RetryPolicy(max_retries=1, backoff_seconds=1)
    assert vc.build_vision_client("gemini", "gemini-3.8-flash", dry_run=False)._retry_policy == vc.RetryPolicy()


# ---------------------------------------------------------------------------
# Phase AE5: dry-run validation (R1) and request timeout (R4). The SDK-level tests below replace
# the SDK's own httpx2 client send() -- the real google-genai request pipeline runs, but nothing
# can leave the machine.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["gemini", "groq"])
@pytest.mark.parametrize("bad", [{"max_retries": -1}, {"request_timeout_seconds": 5}])
def test_dry_run_rejects_invalid_retry_and_timeout_settings(provider, bad):
    with pytest.raises(vc.VisionAPIError):
        vc.build_vision_client(provider, "unused", dry_run=True, rate_limit=bad)


@pytest.mark.parametrize("rate_limit", [None, {}, {"max_retries": 2, "request_timeout_seconds": 120}])
def test_dry_run_with_valid_or_missing_settings_still_returns_null_client(rate_limit, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client = vc.build_vision_client("gemini", "unused", dry_run=True, rate_limit=rate_limit)
    assert isinstance(client, vc.NullVisionClient)


@pytest.mark.parametrize("bad", [0, -1, 29, "300", True, False, 300.0, 300.5, None, [300]])
def test_invalid_request_timeout_values_rejected(bad):
    with pytest.raises(vc.VisionAPIError, match="request_timeout_seconds"):
        vc.RetryPolicy.from_settings({"request_timeout_seconds": bad})


@pytest.mark.parametrize("ok", [30, 300, 600])
def test_valid_request_timeout_values_accepted(ok):
    policy = vc.RetryPolicy.from_settings({"request_timeout_seconds": ok})
    assert policy.request_timeout_seconds == ok and policy.request_timeout_ms == ok * 1000


def test_project_settings_yaml_request_timeout_is_300_seconds():
    import yaml

    settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    rate_limit = yaml.safe_load(settings_path.read_text(encoding="utf-8"))["rate_limit"]
    assert rate_limit["request_timeout_seconds"] == 300
    assert vc.RetryPolicy.from_settings(rate_limit).request_timeout_ms == 300_000


def test_gemini_client_http_options_carry_timeout_and_no_sdk_retries():
    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY)
    http_options = client._client._api_client._http_options
    assert http_options.timeout == 300_000
    assert http_options.retry_options is None  # SDK-level retries stay disabled


def _stub_transport(client, behaviour):
    """Replace the SDK's underlying HTTP client send(); returns the list of captured requests."""
    sent = []

    def send(request, stream=False):
        sent.append(request)
        return behaviour(request)

    client._client._api_client._httpx_client.send = send
    return sent


def test_timeout_is_applied_to_the_real_outgoing_request():
    import httpx2

    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0,
                                   retry_policy=vc.RetryPolicy(request_timeout_seconds=120))

    def timeout(request):
        raise httpx2.ReadTimeout("The read operation timed out", request=request)

    sent = _stub_transport(client, timeout)
    with pytest.raises(vc.GeminiRequestTimeoutError):
        client._call_and_handle_errors(b"fake-image-bytes")
    assert sent[0].extensions["timeout"] == {"connect": 120.0, "read": 120.0, "write": 120.0, "pool": 120.0}
    assert sent[0].headers["X-Server-Timeout"] == "120"
    assert FAKE_KEY not in str(sent[0].url)


def test_httpx2_timeout_maps_to_request_timeout_error_after_one_attempt():
    import httpx2

    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0)
    client._call.retry.wait = lambda retry_state: 0

    def timeout(request):
        raise httpx2.ReadTimeout("timed out talking to key " + FAKE_KEY, request=request)

    sent = _stub_transport(client, timeout)
    with pytest.raises(vc.GeminiRequestTimeoutError) as exc_info:
        client._call_and_handle_errors(b"fake-image-bytes")
    assert len(sent) == 1  # never retried within the same image
    assert FAKE_KEY not in str(exc_info.value) and "***REDACTED***" in str(exc_info.value)
    assert "300s client timeout" in str(exc_info.value)
    assert not isinstance(exc_info.value, (vc.GeminiServiceUnavailableError, vc.GeminiQuotaExhaustedError))


@pytest.mark.parametrize("module_name, exc_name", [
    ("httpx", "ReadTimeout"), ("httpx", "ConnectTimeout"), ("httpx", "WriteTimeout"), ("httpx", "PoolTimeout"),
    ("httpx2", "ReadTimeout"), ("httpx2", "ConnectTimeout"), ("httpx2", "WriteTimeout"), ("httpx2", "PoolTimeout"),
])
def test_httpx_and_httpx2_timeout_exceptions_are_mapped(monkeypatch, module_name, exc_name):
    module = pytest.importorskip(module_name)
    client = _client_no_wait()
    calls = {"n": 0}

    def raise_timeout(**kwargs):
        calls["n"] += 1
        raise getattr(module, exc_name)("simulated")

    monkeypatch.setattr(client._client.models, "generate_content", raise_timeout)
    with pytest.raises(vc.GeminiRequestTimeoutError):
        client._call_and_handle_errors(b"fake-image-bytes")
    assert calls["n"] == 1


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_ordinary_connection_error_is_not_a_request_timeout(monkeypatch, module_name):
    module = pytest.importorskip(module_name)
    client = _client_no_wait()
    calls = {"n": 0}

    def raise_connect_error(**kwargs):
        calls["n"] += 1
        raise module.ConnectError("connection refused")

    monkeypatch.setattr(client._client.models, "generate_content", raise_connect_error)
    with pytest.raises(RuntimeError) as exc_info:
        client._call_and_handle_errors(b"fake-image-bytes")
    assert calls["n"] == 1
    assert not isinstance(exc_info.value, (vc.GeminiRequestTimeoutError, vc.GeminiServiceUnavailableError))


def test_503_keeps_five_attempts_through_the_real_sdk_with_timeout_configured():
    import httpx2

    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0)
    client._call.retry.wait = lambda retry_state: 0

    def overloaded(request):
        return httpx2.Response(503, json={"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}},
                               request=request)

    sent = _stub_transport(client, overloaded)
    with pytest.raises(vc.GeminiServiceUnavailableError):
        client._call_and_handle_errors(b"fake-image-bytes")
    assert len(sent) == 5


def test_daily_quota_429_keeps_one_attempt_through_the_real_sdk():
    import httpx2

    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0)
    client._call.retry.wait = lambda retry_state: 0
    sent = _stub_transport(client, lambda request: httpx2.Response(
        429, json=_daily_quota_exhausted_body(), request=request))
    with pytest.raises(vc.GeminiQuotaExhaustedError) as exc_info:
        client._call_and_handle_errors(b"fake-image-bytes")
    assert len(sent) == 1
    assert exc_info.value.retry_delay_seconds == 6843.0


def test_keyboard_interrupt_during_in_flight_request_propagates_unwrapped():
    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0)

    def interrupted(request):
        raise KeyboardInterrupt()

    sent = _stub_transport(client, interrupted)
    with pytest.raises(KeyboardInterrupt):
        client._call_and_handle_errors(b"fake-image-bytes")
    assert len(sent) == 1


def test_is_request_timeout_handles_unrelated_exceptions():
    assert vc._is_request_timeout(RuntimeError("x")) is False
    assert vc._is_request_timeout(_server_error_503()) is False
    assert len(vc._timeout_exception_types()) >= 1


# ---------------------------------------------------------------------------
# Phase P1-1: per-minute vs daily vs ambiguous HTTP 429, bounded per-minute retry, request
# pacing. All offline: the SDK's generate_content is mocked, sleeps are recorded, never real.
# ---------------------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402


def _per_minute_body(retry_delay: str | None = "23s") -> dict:
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
        "description": "Generate content requests per minute limit exceeded on Free Tier.",
        "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
    }]}]
    if retry_delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                      "message": "You exceeded your current quota.", "details": details}}


def _ambiguous_body() -> dict:
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "rate limit exceeded", "details": []}}


def _err(body):
    from google.genai import errors
    return lambda: errors.ClientError(code=429, response_json=body)


def _sequence_generate_content(*failures):
    """Raises failures[i]() on call i+1 (None entries succeed); succeeds once the list is used up."""
    calls = {"n": 0}
    sentinel = object()

    def fn(**kwargs):
        calls["n"] += 1
        idx = calls["n"] - 1
        if idx < len(failures) and failures[idx] is not None:
            raise failures[idx]()
        return sentinel

    return fn, calls, sentinel


def _client_recording_sleeps(**kwargs):
    """Real per-instance tenacity wait (not overridden), but sleeps are recorded, never slept."""
    client = vc.GeminiVisionClient(model="gemini-3.8-flash", api_key=FAKE_KEY, max_image_dimension=0, **kwargs)
    slept = []
    client._call.retry.sleep = slept.append
    return client, slept


def test_p11_429_kinds_are_distinguished():
    from google.genai import errors
    both = _daily_quota_exhausted_body()
    both["error"]["details"][0]["violations"].append(
        {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel", "description": "per minute"})
    assert vc._gemini_429_kind(_err(_per_minute_body())()) == "per_minute"
    assert vc._gemini_429_kind(_err(_daily_quota_exhausted_body())()) == "daily"
    assert vc._gemini_429_kind(_err(_ambiguous_body())()) == "ambiguous"
    assert vc._gemini_429_kind(_err(both)()) == "daily"  # daily always wins
    assert vc._gemini_429_kind(errors.ClientError(
        code=429, response_json={"error": {"status": "FAILED_PRECONDITION", "message": "x"}})) == "ambiguous"
    assert vc._gemini_429_kind(_server_error_503()) is None
    assert vc._gemini_429_kind(RuntimeError("x")) is None
    # per-minute detection never claims a daily response and vice versa
    assert vc._is_confirmed_per_minute_rate_limit(_err(_daily_quota_exhausted_body())()) is False
    assert vc._is_confirmed_daily_quota_exhausted(_err(_per_minute_body())()) is False


@pytest.mark.parametrize("malformed_body", [
    {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": None, "details": "not-a-list"}},
    {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "x", "details": [{"violations": "bad"}]}},
    {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
])
def test_p11_malformed_429_metadata_is_ambiguous_never_guessed(malformed_body):
    exc = _err(malformed_body)()
    assert vc._is_confirmed_per_minute_rate_limit(exc) is False
    assert vc._gemini_429_kind(exc) == "ambiguous"


def test_p11_per_minute_429_waits_provider_delay_and_retries_once(monkeypatch):
    client, slept = _client_recording_sleeps()
    fn, calls, sentinel = _sequence_generate_content(_err(_per_minute_body("23s")))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    assert client._call_and_handle_errors(b"img") is sentinel
    assert calls["n"] == 2 and slept == [23.0]  # Google's retryDelay respected exactly


def test_p11_per_minute_429_without_retry_delay_waits_bounded_maximum(monkeypatch):
    client, slept = _client_recording_sleeps(retry_policy=vc.RetryPolicy(max_backoff_seconds=45))
    fn, calls, sentinel = _sequence_generate_content(_err(_per_minute_body(None)))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    assert client._call_and_handle_errors(b"img") is sentinel
    assert calls["n"] == 2 and slept == [45]  # max_backoff_seconds, never unbounded


def test_p11_per_minute_429_with_delay_above_bound_is_not_retried(monkeypatch):
    client, slept = _client_recording_sleeps()
    fn, calls, _ = _sequence_generate_content(_err(_per_minute_body("120s")))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiRateLimitedError) as exc_info:
        client._call_and_handle_errors(b"img")
    assert calls["n"] == 1 and slept == []
    assert exc_info.value.kind == "per_minute" and exc_info.value.retry_delay_seconds == 120.0


def test_p11_persistent_per_minute_429_retries_only_once_then_raises(monkeypatch):
    client, slept = _client_recording_sleeps()
    fn, calls, _ = _counting_generate_content(_err(_per_minute_body("10s")))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiRateLimitedError) as exc_info:
        client._call_and_handle_errors(b"img")
    assert calls["n"] == 2 and slept == [10.0]
    err = exc_info.value
    assert err.kind == "per_minute" and isinstance(err, RuntimeError)
    assert not isinstance(err, (vc.GeminiQuotaExhaustedError, vc.GeminiServiceUnavailableError))
    assert FAKE_KEY not in str(err)


def test_p11_rate_limit_retry_budget_is_per_image(monkeypatch):
    client, slept = _client_recording_sleeps()
    fn, calls, sentinel = _sequence_generate_content(_err(_per_minute_body("5s")), None,
                                                     _err(_per_minute_body("5s")), None)
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    assert client._call_and_handle_errors(b"img1") is sentinel
    assert client._call_and_handle_errors(b"img2") is sentinel
    assert calls["n"] == 4 and slept == [5.0, 5.0]


def test_p11_ambiguous_429_is_never_retried(monkeypatch):
    client, slept = _client_recording_sleeps()
    fn, calls, _ = _counting_generate_content(_err(_ambiguous_body()))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiRateLimitedError) as exc_info:
        client._call_and_handle_errors(b"img")
    assert calls["n"] == 1 and slept == []
    assert exc_info.value.kind == "ambiguous" and exc_info.value.retry_delay_seconds is None
    assert "rate limit exceeded" in str(exc_info.value)  # original (redacted) error preserved


def test_p11_daily_quota_still_single_request_and_never_rate_limited_error(monkeypatch):
    client, slept = _client_recording_sleeps()
    fn, calls, _ = _counting_generate_content(_err(_daily_quota_exhausted_body()))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiQuotaExhaustedError) as exc_info:
        client._call_and_handle_errors(b"img")
    assert calls["n"] == 1 and slept == []
    assert not isinstance(exc_info.value, vc.GeminiRateLimitedError)


def test_p11_total_attempts_never_exceed_five_with_mixed_429_and_503(monkeypatch):
    client, slept = _client_recording_sleeps()
    seq = [_err(_per_minute_body("5s"))] + [_server_error_503] * 10
    fn, calls, _ = _sequence_generate_content(*seq)
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiServiceUnavailableError):
        client._call_and_handle_errors(b"img")
    assert calls["n"] == 5  # 1 rate-limit attempt + 4 x 503: the 5-attempt cap holds
    assert slept[0] == 5.0 and len(slept) == 4


def test_p11_per_minute_429_on_the_last_attempt_stops_at_five(monkeypatch):
    client, _ = _client_recording_sleeps()
    fn, calls, _ = _sequence_generate_content(*([_server_error_503] * 4 + [_err(_per_minute_body("5s"))]))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiRateLimitedError):
        client._call_and_handle_errors(b"img")
    assert calls["n"] == 5


def test_p11_503_backoff_unchanged_by_new_wait(monkeypatch):
    client, slept = _client_recording_sleeps()
    fn, calls, sentinel = _sequence_generate_content(_server_error_503, _server_error_503)
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    assert client._call_and_handle_errors(b"img") is sentinel
    assert calls["n"] == 3
    assert 4 <= slept[0] <= 8 and 8 <= slept[1] <= 12  # jittered exponential, as before


def test_p11_retry_wait_selects_rate_limit_or_backoff():
    client = vc.GeminiVisionClient(model="m", api_key=FAKE_KEY)

    def state(exc, attempt=1):
        return SimpleNamespace(outcome=SimpleNamespace(failed=True, exception=lambda: exc),
                               attempt_number=attempt)

    assert client._retry_wait(state(_err(_per_minute_body("17s"))())) == 17.0
    assert client._retry_wait(state(_err(_per_minute_body(None))())) == 60
    assert 4 <= client._retry_wait(state(_server_error_503())) <= 8


def test_p11_keyboard_interrupt_during_rate_limit_wait_propagates(monkeypatch):
    client = vc.GeminiVisionClient(model="m", api_key=FAKE_KEY, max_image_dimension=0)
    fn, calls, _ = _counting_generate_content(_err(_per_minute_body("23s")))
    monkeypatch.setattr(client._client.models, "generate_content", fn)

    def interrupted(_seconds):
        raise KeyboardInterrupt()

    client._call.retry.sleep = interrupted
    with pytest.raises(KeyboardInterrupt):
        client._call_and_handle_errors(b"img")
    assert calls["n"] == 1


def test_p11_request_pacing_spaces_request_starts(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(vc.time, "monotonic", lambda: clock["t"])
    client = vc.GeminiVisionClient(model="m", api_key=FAKE_KEY, requests_per_minute=15)
    paced = []
    client._pace_sleep = lambda s: (paced.append(s), clock.__setitem__("t", clock["t"] + s))
    client._pace()            # first request: no wait
    clock["t"] += 1.0
    client._pace()            # 1s later: must wait the remaining 3s of the 4s interval
    clock["t"] += 10.0
    client._pace()            # well past the interval: no wait
    assert paced == [3.0]


def test_p11_pacing_applies_to_retries_and_is_off_by_default(monkeypatch):
    monkeypatch.setattr(vc.time, "monotonic", lambda: 50.0)  # frozen clock
    paced_client, _ = _client_recording_sleeps(requests_per_minute=30)
    paced = []
    paced_client._pace_sleep = paced.append
    fn, calls, sentinel = _sequence_generate_content(_server_error_503)
    monkeypatch.setattr(paced_client._client.models, "generate_content", fn)
    assert paced_client._call_and_handle_errors(b"img") is sentinel
    assert calls["n"] == 2 and paced == [2.0]  # the retry was paced too (60/30)

    default_client, _ = _client_recording_sleeps()
    default_client._pace_sleep = lambda s: pytest.fail("pacing must be off without requests_per_minute")
    fn2, _, _ = _sequence_generate_content(_server_error_503)
    monkeypatch.setattr(default_client._client.models, "generate_content", fn2)
    default_client._call_and_handle_errors(b"img")


@pytest.mark.parametrize("bad", [0, -1, 2.5, True, "15"])
def test_p11_invalid_requests_per_minute_rejected_in_every_mode(bad):
    with pytest.raises(vc.VisionAPIError, match="requests_per_minute"):
        vc.requests_per_minute_from_settings({"requests_per_minute": bad})
    with pytest.raises(vc.VisionAPIError, match="requests_per_minute"):
        vc.build_vision_client("gemini", "m", dry_run=True, rate_limit={"requests_per_minute": bad})


def test_p11_requests_per_minute_wiring(monkeypatch):
    assert vc.requests_per_minute_from_settings(None) is None
    assert vc.requests_per_minute_from_settings({}) is None
    assert vc.requests_per_minute_from_settings({"requests_per_minute": None}) is None
    assert vc.requests_per_minute_from_settings({"requests_per_minute": 15}) == 15
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    client = vc.build_vision_client("gemini", "m", dry_run=False, rate_limit={"requests_per_minute": 15})
    assert client._min_request_interval == 4.0
    assert isinstance(vc.build_vision_client("gemini", "m", dry_run=True,
                                             rate_limit={"requests_per_minute": 15}), vc.NullVisionClient)


# ---------------------------------------------------------------------------
# Phase P1-3: the daily limit is reported exactly as Google's 429 states it -- never assumed.
# ---------------------------------------------------------------------------


def _daily_body_with_quota_value(value="20") -> dict:
    # Same shape as the real 2026-11-12 response: QuotaFailure carries quotaValue "20".
    body = _daily_quota_exhausted_body()
    violation = body["error"]["details"][0]["violations"][0]
    if value is not None:
        violation["quotaValue"] = value
    return body


def test_p13_daily_quota_limit_is_parsed_from_quota_value():
    from google.genai import errors

    exc = errors.ClientError(code=429, response_json=_daily_body_with_quota_value("20"))
    assert vc._extract_gemini_daily_quota_limit(exc) == (
        20, "GenerateRequestsPerDayPerProjectPerModel-FreeTier")


@pytest.mark.parametrize("body", [
    _daily_quota_exhausted_body(),           # no quotaValue at all
    _daily_body_with_quota_value("abc"),     # malformed
    _daily_body_with_quota_value("0"),       # not a usable limit
    _per_minute_body(),                      # a per-minute violation is never a daily limit
    _ambiguous_body(),
])
def test_p13_daily_quota_limit_is_none_when_not_reported(body):
    from google.genai import errors

    limit, _ = vc._extract_gemini_daily_quota_limit(errors.ClientError(code=429, response_json=body))
    assert limit is None
    assert vc._extract_gemini_daily_quota_limit(RuntimeError("no details")) == (None, None)


def test_p13_quota_error_carries_reported_limit_and_retry_policy_unchanged(monkeypatch):
    client, slept = _client_recording_sleeps()
    fn, calls, _ = _counting_generate_content(_err(_daily_body_with_quota_value("20")))
    monkeypatch.setattr(client._client.models, "generate_content", fn)
    with pytest.raises(vc.GeminiQuotaExhaustedError) as exc_info:
        client._call_and_handle_errors(b"img")
    assert calls["n"] == 1 and slept == []  # P1-1/Phase V behaviour: one request, no wait
    assert exc_info.value.quota_limit == 20
    assert exc_info.value.quota_id == "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    assert exc_info.value.retry_delay_seconds == 6843.0
    # and without quotaValue the limit stays unknown rather than defaulting to anything
    client2, _ = _client_recording_sleeps()
    fn2, _, _ = _counting_generate_content(_err(_daily_quota_exhausted_body()))
    monkeypatch.setattr(client2._client.models, "generate_content", fn2)
    with pytest.raises(vc.GeminiQuotaExhaustedError) as exc_info2:
        client2._call_and_handle_errors(b"img")
    assert exc_info2.value.quota_limit is None


def test_p13_classifier_carries_quota_limit_only_for_daily_quota():
    from src import classifier

    class _Raise(vc.VisionClient):
        def __init__(self, exc):
            self.exc = exc

        def classify(self, image_bytes):
            raise self.exc

    daily = classifier.classify_image(_Raise(vc.GeminiQuotaExhaustedError("q", 10.0, quota_limit=20,
                                                                          quota_id="X-PerDay")), b"x")
    assert daily.quota_exhausted and daily.quota_limit == 20 and daily.quota_id == "X-PerDay"
    rpm = classifier.classify_image(_Raise(vc.GeminiRateLimitedError("r", 5.0, "per_minute")), b"x")
    assert rpm.quota_limit is None and rpm.quota_id is None and not rpm.quota_exhausted


# ---------------------------------------------------------------------------
# Phase P2-8: Groq failure handling. No SDK client is created here: groq.Groq is replaced by
# _P28FakeGroqSDK, whose chat.completions.create replays a script. Errors are real groq SDK
# exception objects built on an offline httpx.Response. No network, no key, no .env.
# ---------------------------------------------------------------------------

import math as _p28_math  # noqa: E402


class _P28FakeGroqSDK:
    """Stands in for groq.Groq: records its constructor kwargs and replays `script` (exceptions
    are raised, anything else is returned; an exhausted script returns a valid response)."""

    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = 0
        self.script = []
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.calls += 1
                item = outer.script.pop(0) if outer.script else _FakeGroqResponse(_VALID_SCHEMA_JSON)
                if isinstance(item, BaseException):
                    raise item
                return item

        self.chat = SimpleNamespace(completions=_Completions())
        _P28FakeGroqSDK.last = self


@pytest.fixture
def p28_groq(monkeypatch):
    """(make_client, sleeps): make_client(*script) -> (GroqVisionClient, fake SDK). Backoff
    sleeps of the class-level tenacity retry are recorded instead of slept."""
    import groq

    monkeypatch.setattr(groq, "Groq", _P28FakeGroqSDK)
    sleeps = []
    monkeypatch.setattr(vc.GroqVisionClient._call.retry, "sleep", sleeps.append)

    def make_client(*script):
        client = vc.GroqVisionClient(model="qwen/qwen3.8-27b", api_key=GROQ_FAKE_KEY)
        sdk = _P28FakeGroqSDK.last
        sdk.script = list(script)
        return client, sdk

    return make_client, sleeps


def _p28_status_error(error_cls, status_code, headers=None, message="boom"):
    import httpx

    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(status_code, request=req, headers=headers or {})
    return error_cls(message, response=resp, body=None)


def _p28_timeout():
    import groq
    import httpx

    return groq.APITimeoutError(request=httpx.Request("POST", "https://api.groq.com/x"))


def _p28_connection_error():
    import groq
    import httpx

    return groq.APIConnectionError(request=httpx.Request("POST", "https://api.groq.com/x"))


def _p28_classifier():
    from src import classifier

    return classifier


def _p28_raise(exc):
    class _Raise(vc.VisionClient):
        def classify(self, image_bytes):
            raise exc

    return _Raise()


def test_p28_groq_sdk_hidden_retries_disabled_and_timeout_explicit(p28_groq):
    import httpx

    make_client, _ = p28_groq
    _, sdk = make_client()
    assert sdk.kwargs["max_retries"] == 0  # the SDK's own default is 2 (verified in its source)
    assert sdk.kwargs["api_key"] == GROQ_FAKE_KEY
    timeout = sdk.kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == vc.GROQ_REQUEST_TIMEOUT_SECONDS == 60.0  # unchanged from the SDK default
    assert timeout.connect == vc.GROQ_CONNECT_TIMEOUT_SECONDS == 5.0


def test_p28_groq_success_is_one_request(p28_groq):
    make_client, sleeps = p28_groq
    client, sdk = make_client(_FakeGroqResponse(_VALID_SCHEMA_JSON))
    assert client.classify(b"img")["ai_suggested_location"] == "Main Deck"
    assert sdk.calls == 1 and sleeps == []


def test_p28_groq_ordinary_provider_failure_is_plain_runtime_error_one_request(p28_groq):
    import groq

    make_client, sleeps = p28_groq
    client, sdk = make_client(_p28_status_error(groq.BadRequestError, 400))
    with pytest.raises(RuntimeError) as info:
        client.classify(b"img")
    assert type(info.value) is RuntimeError  # not a stop / breaker signal
    assert sdk.calls == 1 and sleeps == []
    result = _p28_classifier().classify_image(_p28_raise(info.value), b"x")
    assert result.error and not (result.rate_limited or result.service_unavailable
                                 or result.request_timeout or result.quota_exhausted)


@pytest.mark.parametrize("headers, expected_delay", [
    ({"retry-after": "7"}, 7.0),
    ({"retry-after-ms": "1500"}, 1.5),
    ({"retry-after-ms": "garbage", "retry-after": "3"}, 3.0),
    ({}, None),                                                # missing metadata
    ({"retry-after": "soon"}, None),                           # malformed
    ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),  # HTTP date: not converted
    ({"retry-after": "-5"}, None),                             # negative
    ({"retry-after": "inf"}, None),                            # non-finite
    ({"retry-after": "nan"}, None),
])
def test_p28_groq_429_stops_never_retries_and_reads_retry_after(p28_groq, headers, expected_delay):
    import groq

    make_client, sleeps = p28_groq
    client, sdk = make_client(_p28_status_error(groq.RateLimitError, 429, headers,
                                                message=f"rate limited key={GROQ_FAKE_KEY}"))
    with pytest.raises(vc.GroqRateLimitedError) as info:
        client.classify(b"img")
    assert sdk.calls == 1 and sleeps == []  # exactly one request: no SDK or tenacity retry
    assert info.value.kind == "ambiguous"  # the window is never inferred
    assert info.value.retry_delay_seconds == expected_delay
    assert GROQ_FAKE_KEY not in str(info.value) and "REDACTED" in str(info.value)
    assert isinstance(info.value, vc.ProviderRateLimitedError)
    assert not isinstance(info.value, (vc.GeminiRateLimitedError, vc.GeminiQuotaExhaustedError))


def test_p28_groq_any_status_429_counts_as_rate_limited(p28_groq):
    import groq

    make_client, _ = p28_groq
    client, sdk = make_client(_p28_status_error(groq.APIStatusError, 429))
    with pytest.raises(vc.GroqRateLimitedError):
        client.classify(b"img")
    assert sdk.calls == 1


def test_p28_groq_retry_after_helper_never_raises_on_odd_objects():
    assert vc._extract_groq_retry_after_seconds(RuntimeError("no response")) is None
    weird = SimpleNamespace(response=SimpleNamespace(headers=None))
    assert vc._extract_groq_retry_after_seconds(weird) is None

    class _Boom:
        def get(self, name):
            raise TypeError("broken headers")

    broken = SimpleNamespace(response=SimpleNamespace(headers=_Boom()))
    assert vc._extract_groq_retry_after_seconds(broken) is None


def test_p28_groq_timeout_is_one_request_and_a_breaker_signal(p28_groq):
    make_client, sleeps = p28_groq
    client, sdk = make_client(_p28_timeout())
    with pytest.raises(vc.GroqRequestTimeoutError) as info:
        client.classify(b"img")
    assert sdk.calls == 1 and sleeps == []
    assert "60s client timeout" in str(info.value) and "APITimeoutError" in str(info.value)
    result = _p28_classifier().classify_image(_p28_raise(info.value), b"x")
    assert result.request_timeout and not (result.service_unavailable or result.rate_limited)


def test_p28_groq_connection_error_is_ordinary_failure_not_retried(p28_groq):
    make_client, sleeps = p28_groq
    client, sdk = make_client(_p28_connection_error())
    with pytest.raises(RuntimeError) as info:
        client.classify(b"img")
    assert type(info.value) is RuntimeError and sdk.calls == 1 and sleeps == []


def test_p28_groq_5xx_retry_is_bounded_then_service_unavailable(p28_groq):
    import groq

    make_client, sleeps = p28_groq
    client, sdk = make_client(*[_p28_status_error(groq.InternalServerError, 503) for _ in range(10)])
    with pytest.raises(vc.GroqServiceUnavailableError):
        client.classify(b"img")
    assert sdk.calls == 5  # 5 attempts in total -- no longer up to 15 via the SDK's hidden retries
    assert len(sleeps) == 4 and all(_p28_math.isfinite(s) and 0 <= s <= 30 for s in sleeps)
    result = _p28_classifier().classify_image(_p28_raise(vc.GroqServiceUnavailableError("x")), b"x")
    assert result.service_unavailable and not (result.request_timeout or result.rate_limited)


def test_p28_groq_5xx_then_success_recovers_within_bound(p28_groq):
    import groq

    make_client, sleeps = p28_groq
    client, sdk = make_client(_p28_status_error(groq.InternalServerError, 502),
                              _p28_status_error(groq.InternalServerError, 503),
                              _FakeGroqResponse(_VALID_SCHEMA_JSON))
    assert client.classify(b"img")["person_visible"] == "No"
    assert sdk.calls == 3 and len(sleeps) == 2


def test_p28_groq_429_after_a_5xx_stops_immediately(p28_groq):
    import groq

    make_client, _ = p28_groq
    client, sdk = make_client(_p28_status_error(groq.InternalServerError, 503),
                              _p28_status_error(groq.RateLimitError, 429))
    with pytest.raises(vc.GroqRateLimitedError):
        client.classify(b"img")
    assert sdk.calls == 2


def test_p28_classifier_maps_groq_rate_limit_without_claiming_a_daily_quota():
    rl = _p28_classifier().classify_image(_p28_raise(vc.GroqRateLimitedError("r", 7.0, "ambiguous")), b"x")
    assert rl.rate_limited and rl.rate_limit_kind == "ambiguous" and rl.retry_delay_seconds == 7.0
    assert not rl.quota_exhausted and rl.quota_limit is None and rl.quota_id is None
    assert rl.error.startswith("GroqRateLimitedError")


def test_p28_error_hierarchy_keeps_gemini_types_and_isolates_groq():
    # Gemini: same types, still RuntimeError, now also the provider-neutral bases.
    assert issubclass(vc.GeminiServiceUnavailableError, vc.ProviderServiceUnavailableError)
    assert issubclass(vc.GeminiRateLimitedError, vc.ProviderRateLimitedError)
    assert issubclass(vc.GeminiRequestTimeoutError, vc.ProviderRequestTimeoutError)
    for cls in (vc.ProviderServiceUnavailableError, vc.ProviderRateLimitedError,
                vc.ProviderRequestTimeoutError, vc.GeminiQuotaExhaustedError):
        assert issubclass(cls, RuntimeError)
    assert vc.GeminiRateLimitedError.KINDS == ("per_minute", "ambiguous")
    with pytest.raises(ValueError):
        vc.GeminiRateLimitedError("x", None, "daily")
    g = vc.GeminiRateLimitedError("m", 5.0, "per_minute")
    assert (str(g), g.retry_delay_seconds, g.kind) == ("m", 5.0, "per_minute")
    # Groq: never a Gemini type, so Gemini-only paths (daily quota) can never fire for Groq.
    pairs = ((vc.GroqServiceUnavailableError, vc.GeminiServiceUnavailableError),
             (vc.GroqRateLimitedError, vc.GeminiRateLimitedError),
             (vc.GroqRequestTimeoutError, vc.GeminiRequestTimeoutError))
    for groq_cls, gemini_cls in pairs:
        assert not issubclass(groq_cls, gemini_cls)
        assert not issubclass(groq_cls, vc.GeminiQuotaExhaustedError)


def test_p28_gemini_classifiers_ignore_groq_errors():
    import groq

    rl = _p28_status_error(groq.RateLimitError, 429)
    assert vc._gemini_429_kind(rl) is None
    assert vc._is_confirmed_daily_quota_exhausted(rl) is False
    assert vc._is_retryable(rl) is False
    assert vc._is_retryable(_p28_status_error(groq.InternalServerError, 503)) is False


# ---------------------------------------------------------------------------
# Phase P2-9: independent Groq request pacing (rate_limit.groq_requests_per_minute). Mocked
# monotonic clock, recorded sleeps, fake groq SDK (_P28FakeGroqSDK) and a fake genai.Client --
# no real SDK client, no real sleep, no network, no .env.
# ---------------------------------------------------------------------------


class _P29Clock:
    """Fake monotonic clock; sleep() advances it and records the wait."""

    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def p29(monkeypatch):
    """(make_client, clock, retry_sleeps): make_client(rpm, *script) -> (paced GroqVisionClient,
    fake SDK). Pacing waits go to clock.sleeps; tenacity 5xx backoff waits to retry_sleeps."""
    import groq

    clock = _P29Clock()
    monkeypatch.setattr(vc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(groq, "Groq", _P28FakeGroqSDK)
    retry_sleeps = []
    monkeypatch.setattr(vc.GroqVisionClient._call.retry, "sleep", retry_sleeps.append)

    def make_client(rpm, *script):
        client = vc.GroqVisionClient(model="qwen/qwen3.8-27b", api_key=GROQ_FAKE_KEY,
                                     requests_per_minute=rpm)
        client._pace_sleep = clock.sleep
        sdk = _P28FakeGroqSDK.last
        sdk.script = list(script)
        return client, sdk

    return make_client, clock, retry_sleeps


def test_p29_first_request_never_waits(p29):
    make_client, clock, _ = p29
    client, sdk = make_client(12)
    client.classify(b"img")
    assert sdk.calls == 1 and clock.sleeps == []


def test_p29_subsequent_requests_spaced_by_the_configured_interval(p29):
    make_client, clock, _ = p29
    client, sdk = make_client(12)        # 60/12 = 5s between request starts
    client.classify(b"a")                # t=1000, no wait
    clock.t += 1.5                       # 1.5s of "work" between photos
    client.classify(b"b")                # must wait the remaining 3.5s
    clock.t += 9.0                       # well past the interval
    client.classify(b"c")                # no wait
    assert clock.sleeps == [3.5] and sdk.calls == 3


def test_p29_back_to_back_requests_and_no_wait_after_the_last(p29):
    make_client, clock, _ = p29
    client, sdk = make_client(30)        # 2s interval
    for i in range(5):
        client.classify(bytes([i]))
    # 5 requests -> exactly 4 waits, all BEFORE a request; nothing after the final one
    assert clock.sleeps == [2.0] * 4 and sdk.calls == 5
    assert client._last_request_start == 1000.0 + 8.0


def test_p29_retries_are_paced_and_the_retry_count_is_unchanged(p29):
    import groq

    make_client, clock, retry_sleeps = p29
    client, sdk = make_client(60, _p28_status_error(groq.InternalServerError, 503),
                              _p28_status_error(groq.InternalServerError, 503),
                              _FakeGroqResponse(_VALID_SCHEMA_JSON))
    assert client.classify(b"img")["ai_suggested_location"] == "Main Deck"
    assert sdk.calls == 3 and len(retry_sleeps) == 2   # P2-8 retry policy unchanged
    # backoff sleeps do not advance the fake clock, so each retry still waits a full 1s pacing gap
    assert clock.sleeps == [1.0, 1.0]


def test_p29_5xx_still_bounded_to_five_attempts_when_paced(p29):
    import groq

    make_client, clock, _ = p29
    client, sdk = make_client(60, *[_p28_status_error(groq.InternalServerError, 503) for _ in range(10)])
    with pytest.raises(vc.GroqServiceUnavailableError):
        client.classify(b"img")
    assert sdk.calls == 5 and len(clock.sleeps) == 4  # no extra attempts introduced by pacing


def test_p29_429_still_stops_after_exactly_one_request(p29):
    import groq

    make_client, clock, retry_sleeps = p29
    client, sdk = make_client(20, _FakeGroqResponse(_VALID_SCHEMA_JSON),
                              _p28_status_error(groq.RateLimitError, 429, {"retry-after": "30"}))
    client.classify(b"a")
    with pytest.raises(vc.GroqRateLimitedError) as info:
        client.classify(b"b")
    assert sdk.calls == 2 and retry_sleeps == []
    assert clock.sleeps == [3.0]                       # only the normal pre-request gap
    assert info.value.retry_delay_seconds == 30.0      # reported, never slept on


def test_p29_timeout_still_one_request_when_paced(p29):
    make_client, clock, _ = p29
    client, sdk = make_client(60, _p28_timeout())
    with pytest.raises(vc.GroqRequestTimeoutError):
        client.classify(b"img")
    assert sdk.calls == 1 and clock.sleeps == []


def test_p29_wait_is_bounded_even_if_the_clock_goes_backwards(p29):
    make_client, clock, _ = p29
    client, _ = make_client(30)          # 2s interval
    client.classify(b"a")                # start recorded at t=1000
    clock.t -= 500.0                     # a misbehaving clock: never wait more than one interval
    client.classify(b"b")
    assert clock.sleeps == [2.0]


def test_p29_ctrl_c_during_the_pacing_wait_sends_nothing(p29):
    make_client, clock, _ = p29
    client, sdk = make_client(30)
    client.classify(b"a")

    def _interrupt(seconds):
        raise KeyboardInterrupt

    client._pace_sleep = _interrupt
    with pytest.raises(KeyboardInterrupt):
        client.classify(b"b")
    assert sdk.calls == 1


def test_p29_direct_construction_without_a_cap_is_unpaced(p29):
    make_client, clock, _ = p29
    client, sdk = make_client(None)
    client._pace_sleep = lambda s: pytest.fail("no pacing without requests_per_minute")
    for i in range(3):
        client.classify(bytes([i]))
    assert sdk.calls == 3 and client._min_request_interval == 0.0


@pytest.mark.parametrize("bad", [0, -1, 2.5, True, False, "2", "abc", [2], {}])
def test_p29_invalid_groq_rpm_rejected_everywhere(bad, monkeypatch):
    import groq

    monkeypatch.setattr(groq, "Groq", _P28FakeGroqSDK)
    rl = {"groq_requests_per_minute": bad}
    for provider, dry_run in (("groq", False), ("groq", True), ("gemini", False), ("gemini", True)):
        with pytest.raises(vc.VisionAPIError, match="groq_requests_per_minute must be an integer >= 1"):
            vc.validate_client_settings(provider, rl, dry_run=dry_run)
    with pytest.raises(vc.VisionAPIError, match="groq_requests_per_minute"):
        vc.GroqVisionClient(model="m", api_key=GROQ_FAKE_KEY, requests_per_minute=bad)


@pytest.mark.parametrize("rate_limit", [None, {}, {"groq_requests_per_minute": None},
                                        {"requests_per_minute": 15}])
def test_p29_missing_groq_rpm_is_a_config_error_for_live_groq_only(rate_limit, monkeypatch):
    import groq

    monkeypatch.setattr(groq, "Groq", lambda **kw: pytest.fail("no Groq client may be built"))
    monkeypatch.setenv("GROQ_API_KEY", GROQ_FAKE_KEY)
    with pytest.raises(vc.VisionAPIError, match="groq_requests_per_minute is required when provider is groq"):
        vc.validate_client_settings("groq", rate_limit, dry_run=False)
    with pytest.raises(vc.VisionAPIError, match="no default is assumed"):
        vc.build_vision_client("groq", "qwen/qwen3.8-27b", dry_run=False, rate_limit=rate_limit)
    # not required for a dry run (NullVisionClient, nothing paced) or for Gemini
    assert isinstance(vc.build_vision_client("groq", "m", dry_run=True, rate_limit=rate_limit),
                      vc.NullVisionClient)
    assert vc.validate_client_settings("gemini", rate_limit, dry_run=False)[1] == (
        (rate_limit or {}).get("requests_per_minute"))


def test_p29_gemini_and_groq_pacing_are_independent(monkeypatch):
    import groq
    from google import genai

    monkeypatch.setattr(groq, "Groq", _P28FakeGroqSDK)
    monkeypatch.setattr(genai, "Client", lambda **kw: SimpleNamespace(models=None))
    monkeypatch.setenv("GROQ_API_KEY", GROQ_FAKE_KEY)
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    both = {"requests_per_minute": 15, "groq_requests_per_minute": 20}
    gem = vc.build_vision_client("gemini", "gemini-3.8-flash", dry_run=False, rate_limit=both)
    grq = vc.build_vision_client("groq", "qwen/qwen3.8-27b", dry_run=False, rate_limit=both)
    assert gem._min_request_interval == 4.0 and grq._min_request_interval == 3.0
    # Groq's setting never paces Gemini...
    only_groq = vc.build_vision_client("gemini", "gemini-3.8-flash", dry_run=False,
                                       rate_limit={"groq_requests_per_minute": 20})
    assert only_groq._min_request_interval == 0.0
    # ...and Gemini's requests_per_minute is never borrowed for Groq
    with pytest.raises(vc.VisionAPIError, match="groq_requests_per_minute is required"):
        vc.build_vision_client("groq", "qwen/qwen3.8-27b", dry_run=False, rate_limit={"requests_per_minute": 15})
    # separate state: pacing one client never delays the other
    clock = _P29Clock()
    monkeypatch.setattr(vc.time, "monotonic", clock.monotonic)
    gem._pace_sleep = grq._pace_sleep = clock.sleep
    gem._pace()
    grq._pace()
    assert clock.sleeps == []


def test_p29_groq_rpm_is_reported_by_validate_without_changing_its_return_shape():
    policy, rpm = vc.validate_client_settings("groq", {"groq_requests_per_minute": 6,
                                                      "requests_per_minute": 15}, dry_run=False)
    assert rpm == 15 and isinstance(policy, vc.RetryPolicy)  # (RetryPolicy, Gemini rpm) as before
    assert vc.groq_requests_per_minute_from_settings({"groq_requests_per_minute": 6}) == 6
    assert vc.groq_requests_per_minute_from_settings({"groq_requests_per_minute": 6}, required=True) == 6
