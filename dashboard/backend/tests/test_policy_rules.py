"""Phase 8.1 -- downgrade-only text rules. Offline: synthetic assessments only."""

from __future__ import annotations

import json

import pytest

from app.ai.providers import MOCK_SCENARIOS, MockConditionProvider
from app.ai.schema import POLICY_VERSION, InvalidAIResponse, apply_policy, validate_assessment
from test_ai_analysis import make_client, pair  # noqa: F401  (fixture reuse)

EV = {"region": "deck plating, lower left", "earlier_appearance": "Light rust staining along the weld.",
      "later_appearance": "Rust staining extends 20 cm further along the weld.", "change": "Corrosion staining has spread."}


def make(base="deteriorated", **changes):
    raw = json.loads(json.dumps(MOCK_SCENARIOS[base]))
    raw.update(changes)
    return validate_assessment(raw)


def ev(**kw):
    return [{**EV, **kw}]


def rules(a):
    return apply_policy(a).rules_triggered


# --------------------------------------------------------------------------- baselines (no false positives)
@pytest.mark.parametrize("name", ["deteriorated", "ameliorated", "stable", "needs_review"])
def test_reference_scenarios_trigger_no_downgrade_rules(name):
    out = apply_policy(validate_assessment(MOCK_SCENARIOS[name]))
    assert not [r for r in out.rules_triggered if r in {"photographic_change_only", "maintenance_claim_unsupported",
                                                         "explanation_contradiction", "no_visual_observations",
                                                         "identical_descriptions", "vague_regions"}]
    assert out.policy_version == POLICY_VERSION


def test_stable_high_confidence_is_untouched():
    out = apply_policy(make("stable"))
    assert out.classification.value == "STABLE" and out.confidence.value == "HIGH"
    assert out.adjustments == [] and not out.human_review_required


# --------------------------------------------------------------------------- 1 photographic factors
def test_change_attributed_only_to_lighting_is_downgraded():
    a = make(deterioration_evidence=ev(change="The area looks darker because of the shadow and lower sun angle."),
             explanation="The later photograph is darker in this region.")
    out = apply_policy(a)
    assert out.classification.value == "NEEDS_REVIEW" and "photographic_change_only" in out.rules_triggered
    assert out.adjustments[0].startswith("Changed from DETERIORATED to NEEDS_REVIEW")


def test_physical_change_mentioning_lighting_is_not_downgraded():
    a = make(deterioration_evidence=ev(change="Rust staining has spread despite brighter lighting."))
    assert "photographic_change_only" not in rules(a)


def test_negated_photographic_wording_is_ignored():
    a = make(deterioration_evidence=ev(change="Not a lighting effect: corrosion has spread along the weld."))
    assert "photographic_change_only" not in rules(a)


def test_mixed_evidence_only_requires_review():
    a = make(deterioration_evidence=[EV, {**EV, "region": "railing base", "change": "Glare makes it look worse."}])
    out = apply_policy(a)
    assert "photographic_change_partly" in out.rules_triggered and out.classification.value == "DETERIORATED"
    assert out.human_review_required


# --------------------------------------------------------------------------- 2 / 3 maintenance & recommendations
def test_unsupported_maintenance_claim_is_downgraded():
    a = make("stable", explanation="The hatch coaming was repainted since the earlier inspection.")
    out = apply_policy(a)
    assert out.classification.value == "NEEDS_REVIEW" and "maintenance_claim_unsupported" in out.rules_triggered


def test_maintenance_claim_with_improvement_evidence_only_requires_review():
    a = make("ameliorated", explanation="The coaming has been repainted; the fresh coating is visible over the old rust.")
    out = apply_policy(a)
    assert out.classification.value == "AMELIORATED" and "maintenance_claim" in out.rules_triggered
    assert out.human_review_required


def test_negated_maintenance_is_ignored():
    a = make("stable", explanation="The coating was not repainted; no significant visible change can be established.")
    assert not {"maintenance_claim", "maintenance_claim_unsupported"} & set(rules(a))


def test_recommendation_requires_review_but_does_not_downgrade():
    a = make(visual_observations=["Both photographs show the same winch foundation.", "The plating should be chipped and recoated."])
    out = apply_policy(a)
    assert "recommendation" in out.rules_triggered and out.classification.value == "DETERIORATED"
    assert "maintenance_claim_unsupported" not in out.rules_triggered  # a suggestion is not a claim of work done
    assert any("recommendations" in r for r in out.review_reasons)


# --------------------------------------------------------------------------- 4 contradictions
@pytest.mark.parametrize("base,explanation", [
    ("stable", "Corrosion has spread along the weld seam since the earlier inspection."),
    ("stable", "There is new corrosion on the plating."),
    ("deteriorated", "There is no visible change between the two photographs."),
    ("ameliorated", "The coating remains the same in both photographs."),
    ("ameliorated", "The plating has deteriorated with more corrosion."),
])
def test_explanation_contradicting_classification_is_downgraded(base, explanation):
    out = apply_policy(make(base, explanation=explanation))
    assert out.classification.value == "NEEDS_REVIEW" and "explanation_contradiction" in out.rules_triggered


@pytest.mark.parametrize("base,explanation", [
    ("stable", "There is no new corrosion and no new damage visible."),
    ("stable", "No significant visible change in coating or structure can be established."),
    ("deteriorated", "Corrosion staining is more extensive; there is no improvement anywhere."),
])
def test_consistent_explanations_are_not_flagged(base, explanation):
    assert "explanation_contradiction" not in rules(make(base, explanation=explanation))


# --------------------------------------------------------------------------- 5 / 6 / 7 evidence quality
def test_empty_observations_are_rejected_by_schema():
    with pytest.raises(InvalidAIResponse):
        validate_assessment({**MOCK_SCENARIOS["stable"], "visual_observations": []})


def test_placeholder_observations_are_downgraded():
    out = apply_policy(make("stable", visual_observations=["N/A"]))
    assert out.classification.value == "NEEDS_REVIEW" and "no_visual_observations" in out.rules_triggered


def test_identical_descriptions_are_downgraded():
    out = apply_policy(make(deterioration_evidence=ev(later_appearance=EV["earlier_appearance"])))
    assert out.classification.value == "NEEDS_REVIEW" and "identical_descriptions" in out.rules_triggered


@pytest.mark.parametrize("region", ["whole image", "Entire photograph", "general", "N/A"])
def test_vague_regions_are_downgraded(region):
    out = apply_policy(make(deterioration_evidence=ev(region=region)))
    assert out.classification.value == "NEEDS_REVIEW" and "vague_regions" in out.rules_triggered


def test_specific_region_is_accepted():
    assert "vague_regions" not in rules(make(deterioration_evidence=ev(region="whole image of the port bollard base")))


# --------------------------------------------------------------------------- 8 / 9 confidence caps
def test_high_confidence_with_partial_overlap_is_capped():
    out = apply_policy(make("stable", same_area="PARTIAL_OVERLAP"))
    assert out.confidence.value == "MEDIUM" and out.classification.value == "STABLE"
    assert any(adj.startswith("Confidence lowered from HIGH to MEDIUM") for adj in out.adjustments)


def test_photographic_limitations_cap_high_confidence():
    out = apply_policy(make("stable", limitations=["Strong glare and a different camera angle in the later photograph."]))
    assert out.confidence.value == "MEDIUM" and out.human_review_required


def test_photographic_limitations_with_medium_confidence_only_require_review():
    out = apply_policy(make("deteriorated", limitations=["Different camera angle."]))
    assert out.confidence.value == "MEDIUM" and "photographic_limitations" in out.rules_triggered


def test_limitation_saying_no_lighting_issue_is_ignored():
    assert "photographic_limitations" not in rules(make("stable", limitations=["No lighting or angle differences."]))


# --------------------------------------------------------------------------- never upgrade / immutability
@pytest.mark.parametrize("name", ["deteriorated", "ameliorated", "stable", "needs_review"])
@pytest.mark.parametrize("conf", ["LOW", "MEDIUM", "HIGH"])
def test_never_upgrades(name, conf):
    a = make(name, confidence=conf, limitations=["Different camera angle."], same_area="PARTIAL_OVERLAP")
    out = apply_policy(a)
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    assert order[out.confidence.value] <= order[conf]
    assert out.classification.value in (name.upper(), "NEEDS_REVIEW")
    if name == "needs_review":
        assert out.classification.value == "NEEDS_REVIEW"


def test_saved_record_keeps_original_ai_response_and_policy_version(make_client):
    raw = {**MOCK_SCENARIOS["stable"], "explanation": "The plating was repainted."}
    c = make_client(MockConditionProvider(lambda e, l, p: raw))
    a = c.post("/api/ai-analysis/compare", json=pair(c)).json()["analysis"]
    assert a["policy_version"] == POLICY_VERSION
    assert a["ai_result"]["explanation"] == "The plating was repainted."     # original preserved verbatim
    assert a["model_classification"] == "STABLE" and a["classification"] == "NEEDS_REVIEW"
    assert a["ai_result"]["confidence"] == "HIGH"


def test_existing_records_are_not_re_evaluated(make_client, tmp_path):
    c = make_client(MockConditionProvider("stable"), analysis_dir=tmp_path / "a")
    a = c.post("/api/ai-analysis/compare", json=pair(c)).json()["analysis"]
    path = tmp_path / "a" / f"{a['analysis_id']}.json"
    data = json.loads(path.read_text())
    data.pop("policy_version")            # simulate a pre-Phase-8 record
    data["ai_result"]["explanation"] = "The plating was repainted."  # would trigger v2 rules if re-evaluated
    path.write_text(json.dumps(data))
    c2 = make_client(MockConditionProvider("stable"), analysis_dir=tmp_path / "a")
    loaded = c2.get(f"/api/ai-analysis/{a['analysis_id']}").json()
    assert loaded["policy_version"] is None and loaded["classification"] == "STABLE"
