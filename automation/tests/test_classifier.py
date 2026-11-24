import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import classifier
from src.vision_client import (GeminiQuotaExhaustedError, GeminiRequestTimeoutError,
                               GeminiServiceUnavailableError, VisionClient)


class StubClient(VisionClient):
    def __init__(self, response=None, raise_error=False):
        self._response = response
        self._raise_error = raise_error

    def classify(self, image_bytes: bytes) -> dict:
        if self._raise_error:
            raise RuntimeError("simulated API failure")
        return self._response


def test_well_formed_response_passes_through():
    client = StubClient({
        "person_visible": "No",
        "activity_identified": "No activity observed",
        "activity_confidence": "High",
        "ai_suggested_location": "Main Deck",
        "location_confidence": "High",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.activity_identified == "No activity observed"
    assert result.activity_confidence == "High"
    assert result.ai_suggested_location == "Main Deck"
    assert result.location_confidence == "High"
    assert result.error is None


def test_api_error_degrades_to_needs_review_on_everything():
    client = StubClient(raise_error=True)
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.activity_identified == classifier.NEEDS_REVIEW
    assert result.activity_confidence == classifier.NEEDS_REVIEW
    assert result.ai_suggested_location == classifier.NEEDS_REVIEW
    assert result.location_confidence == classifier.NEEDS_REVIEW
    assert result.person_visible == "Uncertain"
    assert result.error is not None


def test_uncertain_person_forces_needs_review_activity_even_if_model_claimed_otherwise():
    # the model should never be trusted to self-police this -- even if it names an activity,
    # an uncertain person_visible must force activity back to Needs Review.
    client = StubClient({
        "person_visible": "Uncertain",
        "activity_identified": "A worker is welding a pipe",
        "activity_confidence": "High",
        "ai_suggested_location": "Main Deck",
        "location_confidence": "Medium",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.activity_identified == classifier.NEEDS_REVIEW
    assert result.activity_confidence == classifier.NEEDS_REVIEW
    # location is independent of the person/activity uncertainty
    assert result.ai_suggested_location == "Main Deck"
    assert result.location_confidence == "Medium"


def test_person_visible_no_forces_no_activity_text():
    client = StubClient({
        "person_visible": "No",
        "activity_identified": "Routine maintenance inferred from equipment",
        "activity_confidence": "Medium",
        "ai_suggested_location": "Hatch Coaming",
        "location_confidence": "High",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.activity_identified == classifier.NO_ACTIVITY


def test_person_visible_yes_with_unclear_task_preserves_uncertainty_not_invented_task():
    client = StubClient({
        "person_visible": "Yes",
        "activity_identified": "No activity observed",  # model collapsed presence into "nothing"
        "activity_confidence": "High",
        "ai_suggested_location": "Main Deck",
        "location_confidence": "High",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.activity_identified == classifier.PERSON_VISIBLE_UNCONFIRMED_TASK
    assert result.activity_confidence == classifier.NEEDS_REVIEW
    # location confidence must stay independent and untouched
    assert result.location_confidence == "High"


def test_invalid_confidence_vocabulary_downgrades_to_low():
    client = StubClient({
        "person_visible": "No",
        "activity_identified": "No activity observed",
        "activity_confidence": "High",
        "ai_suggested_location": "Main Deck",
        "location_confidence": "Very Sure",  # not in the closed vocabulary
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.location_confidence == "Low"


def test_location_needs_review_forces_confidence_needs_review():
    client = StubClient({
        "person_visible": "No",
        "activity_identified": "No activity observed",
        "activity_confidence": "High",
        "ai_suggested_location": "Needs Review",
        "location_confidence": "High",  # inconsistent with the model's own location value
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.location_confidence == classifier.NEEDS_REVIEW


def test_groq_not_visible_response_normalizes_to_unconfirmed_task():
    # regression test for the live Week 30/2026-08-02_001.jpg Groq call (2026-11-12): the model
    # returned activity_identified="Not visible", which the original exact-string-only guardrail
    # let pass through unnormalized.
    client = StubClient({
        "person_visible": "Yes",
        "activity_identified": "Not visible",
        "activity_confidence": "Needs Review",
        "ai_suggested_location": "Industrial Vessel Deck",
        "location_confidence": "High",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.activity_identified == classifier.PERSON_VISIBLE_UNCONFIRMED_TASK
    assert result.activity_confidence == classifier.NEEDS_REVIEW
    # location text is never rewritten or discarded -- only its confidence is downgraded, by the
    # Phase D vocabulary guardrail, since "Industrial Vessel Deck" isn't a recognized location.
    assert result.ai_suggested_location == "Industrial Vessel Deck"
    assert result.location_confidence == classifier.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# Phase D: location vocabulary guardrail.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "Main Deck",
        "main deck",  # case-insensitive
        "MAIN DECK",
        "Hatch Coaming",
        "CARGO HOLD NO. 3",
        "Boat Deck Area",
        "Bridge Deck",
        "Forecastle Deck Area",
        "Funnel Deck Area",
        "Gangway Entrance",
        "Monkey Island",
        "Poop Deck and Mooring Winch",
        "Port and Starboard Main Deck Facing Forward View",
        "Rescue Boat and Garbage Storage Deck",
        "TOP VIEW MAIN DECK",
    ],
)
def test_known_location_terms_pass_through_with_confidence_unchanged(location):
    client = StubClient({
        "person_visible": "No",
        "activity_identified": "No activity observed",
        "activity_confidence": "High",
        "ai_suggested_location": location,
        "location_confidence": "Medium",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.ai_suggested_location == location
    assert result.location_confidence == "Medium"  # unchanged -- recognized, so not downgraded


@pytest.mark.parametrize(
    "location",
    [
        "Cross deck Fwd of # 1",
        "Cross deck Fwd of # 9",  # hold number never seen in this project's real data
        "cross deck fwd of #9",  # case-insensitive, no space before number
        "Cross deck Between # 1&2",
        "Cross deck Between # 8&9",  # never-seen pair, still a valid pattern
    ],
)
def test_cross_deck_numbered_patterns_are_accepted(location):
    client = StubClient({
        "person_visible": "No",
        "activity_identified": "No activity observed",
        "activity_confidence": "High",
        "ai_suggested_location": location,
        "location_confidence": "High",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.ai_suggested_location == location
    assert result.location_confidence == "High"  # unchanged -- pattern recognized


@pytest.mark.parametrize(
    "location",
    ["Industrial Vessel Deck", "Cargo Deck (LNG/Tanker Terminal)"],
)
def test_invented_locations_are_downgraded_but_text_preserved(location):
    # the two real observed Groq failures (2026-11-12 live calls).
    client = StubClient({
        "person_visible": "No",
        "activity_identified": "No activity observed",
        "activity_confidence": "High",
        "ai_suggested_location": location,
        "location_confidence": "High",  # model's own (unwarranted) confidence
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.ai_suggested_location == location  # never rewritten, never defaulted to Main Deck
    assert result.location_confidence == classifier.NEEDS_REVIEW


def test_existing_needs_review_location_confidence_is_unaffected():
    client = StubClient({
        "person_visible": "No",
        "activity_identified": "No activity observed",
        "activity_confidence": "High",
        "ai_suggested_location": "Needs Review",
        "location_confidence": "Medium",  # inconsistent with the model's own location value
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.ai_suggested_location == classifier.NEEDS_REVIEW
    assert result.location_confidence == classifier.NEEDS_REVIEW


def test_unrecognized_location_does_not_discard_activity_classification():
    client = StubClient({
        "person_visible": "Yes",
        "activity_identified": "A crew member is coiling a mooring line",
        "activity_confidence": "High",
        "ai_suggested_location": "Industrial Vessel Deck",
        "location_confidence": "High",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    # activity is untouched by the location guardrail -- the two are independent.
    assert result.activity_identified == "A crew member is coiling a mooring line"
    assert result.activity_confidence == "High"
    assert result.location_confidence == classifier.NEEDS_REVIEW


@pytest.mark.parametrize(
    "unsupported_phrase",
    ["Unclear", "unclear", "Unknown", "Task unclear", "Not confirmed", "  Not Visible  "],
)
def test_person_visible_yes_normalizes_every_known_ambiguous_task_variant(unsupported_phrase):
    client = StubClient({
        "person_visible": "Yes",
        "activity_identified": unsupported_phrase,
        "activity_confidence": "High",  # even if the model over-states confidence
        "ai_suggested_location": "Main Deck",
        "location_confidence": "Medium",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.activity_identified == classifier.PERSON_VISIBLE_UNCONFIRMED_TASK
    assert result.activity_confidence == classifier.NEEDS_REVIEW


def test_person_visible_yes_with_genuine_specific_activity_is_preserved():
    # a real, specific, visually-observable action must NOT be swept into the ambiguous-task
    # normalization just because the string doesn't exactly match "No activity observed" etc.
    client = StubClient({
        "person_visible": "Yes",
        "activity_identified": "A crew member is coiling a mooring line",
        "activity_confidence": "High",
        "ai_suggested_location": "Main Deck",
        "location_confidence": "High",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.activity_identified == "A crew member is coiling a mooring line"
    assert result.activity_confidence == "High"


def test_false_person_scenario_cannot_produce_a_confirmed_activity():
    # person_visible="Uncertain" (a false/ambiguous person detection) must never let a
    # high-confidence invented task through, no matter how convincingly the model states one --
    # mirrors the live Groq call's person_visible=Yes false positive, but for the explicitly
    # uncertain case the schema already supports.
    client = StubClient({
        "person_visible": "Uncertain",
        "activity_identified": "A worker is operating a crane",
        "activity_confidence": "High",
        "ai_suggested_location": "Main Deck",
        "location_confidence": "High",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.activity_identified == classifier.NEEDS_REVIEW
    assert result.activity_confidence == classifier.NEEDS_REVIEW


def test_missing_person_visible_treated_as_uncertain():
    client = StubClient({
        "activity_identified": "Something",
        "activity_confidence": "High",
        "ai_suggested_location": "Main Deck",
        "location_confidence": "High",
    })
    result = classifier.classify_image(client, b"fake-bytes")
    assert result.person_visible == "Uncertain"
    assert result.activity_identified == classifier.NEEDS_REVIEW


# Phase AE3: service_unavailable is set only for GeminiServiceUnavailableError, and never
# together with quota_exhausted.
class _RaisingClient(VisionClient):
    def __init__(self, exc):
        self._exc = exc

    def classify(self, image_bytes: bytes) -> dict:
        raise self._exc


def test_service_unavailable_error_sets_only_service_unavailable_flag():
    result = classifier.classify_image(_RaisingClient(GeminiServiceUnavailableError("503 UNAVAILABLE")), b"x")
    assert result.service_unavailable is True
    assert result.quota_exhausted is False
    assert result.error is not None
    assert result.ai_suggested_location == classifier.NEEDS_REVIEW


def test_quota_error_does_not_set_service_unavailable():
    result = classifier.classify_image(_RaisingClient(GeminiQuotaExhaustedError("quota", 10.0)), b"x")
    assert result.quota_exhausted is True
    assert result.service_unavailable is False


def test_ordinary_failure_and_success_do_not_set_service_unavailable():
    assert classifier.classify_image(_RaisingClient(RuntimeError("other")), b"x").service_unavailable is False
    ok = classifier.classify_image(StubClient(response={
        "person_visible": "No", "activity_identified": "No activity observed",
        "activity_confidence": "High", "ai_suggested_location": "Main Deck",
        "location_confidence": "High",
    }), b"x")
    assert ok.service_unavailable is False and ok.error is None


# Phase AE5: request_timeout is set only for GeminiRequestTimeoutError, never together with the
# other two failure flags.
def test_request_timeout_error_sets_only_request_timeout_flag():
    result = classifier.classify_image(_RaisingClient(GeminiRequestTimeoutError("timed out")), b"x")
    assert result.request_timeout is True
    assert result.service_unavailable is False and result.quota_exhausted is False
    assert result.error is not None and result.ai_suggested_location == classifier.NEEDS_REVIEW


def test_other_failures_do_not_set_request_timeout():
    for exc in (GeminiServiceUnavailableError("503"), GeminiQuotaExhaustedError("q", 1.0), RuntimeError("x")):
        assert classifier.classify_image(_RaisingClient(exc), b"x").request_timeout is False
