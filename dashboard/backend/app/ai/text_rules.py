"""Phase 8.1 -- conservative text checks on a validated assessment (downgrade-only).

Each check returns findings with an action:
  "downgrade"  -> the classification becomes NEEDS_REVIEW
  "confidence" -> HIGH confidence is capped at MEDIUM
  "review"     -> human review is required; nothing is changed
Matches preceded by a negation ("no new corrosion", "not repainted") are ignored, and a check fires
only on explicit wording, to keep false positives low. The AI response itself is never modified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .schema import Classification, Level, SameArea, StrictConditionAssessment

Action = Literal["downgrade", "confidence", "review"]


@dataclass(frozen=True)
class Finding:
    rule: str
    action: Action
    message: str


_NEGATION = re.compile(r"\b(no|not|without|nor|never|neither|cannot|can't|isn't|aren't|wasn't|weren't)\b(\W+\w+){0,3}\W*$")


_MODAL = re.compile(r"\b(should|must|could|may|might|needs?|ought|recommend\w*|requires?|advis\w*)\b(\W+\w+){0,3}\W*$")


def _asserts(text: str, pattern: re.Pattern, *, ignore_modal: bool = False) -> bool:
    """True when pattern occurs in text and that occurrence is not negated just before it.
    With ignore_modal, occurrences framed as a suggestion ("should be repainted") are ignored too."""
    for m in pattern.finditer(text):
        before = text[: m.start()].lower()[-60:]
        if _NEGATION.search(before) or (ignore_modal and _MODAL.search(before)):
            continue
        return True
    return False


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", " ".join(text.lower().split())).strip()


# Photographic (non-condition) factors vs physical-condition vocabulary.
PHOTO = re.compile(r"\b(light(ing)?|shadows?|shade|glare|sun(light|lit)?|bright(er|ness)?|dark(er)?|exposure|reflections?|"
                   r"wet(ness)?|rain|damp|moist|camera|angles?|perspective|distance|zoom(ed)?|framing|framed|focus|blur(red|ry)?|"
                   r"white balance|colou?r (cast|balance|tone)|weather|cloud(s|y)?|time of day|resolution)\b", re.I)
PHYSICAL = re.compile(r"\b(corro\w*|rust\w*|coating|paint\w*|crack\w*|dent\w*|deform\w*|pitt\w*|scal(e|ing)|blister\w*|"
                      r"flak\w*|peel\w*|damage\w*|leak\w*|missing|broken|stain\w*|oxid\w*|wear|worn|abrasion|buckl\w*|"
                      r"bent|fractur\w*|holes?|perforat\w*|debris|oil|grease|weld\w*)\b", re.I)
MAINTENANCE = re.compile(r"\b(re-?painted|repaint(ed|ing)?|painted over|re-?coated|repaired|replaced|renewed|re-?welded|"
                         r"chipped and painted|wire-?brushed|sand-?blasted|blasted|cleaned|maintenance (was|has been|had been) "
                         r"(carried out|performed|done|completed)|(was|has been|have been|had been) (fixed|treated|serviced))\b", re.I)
RECOMMEND = re.compile(r"\b(recommend\w*|advis(e|able|ed)|should be|must be|needs? to be|ought to|"
                       r"requires? (repair|attention|maintenance|painting|treatment|inspection)|schedule\w* (for )?(repair|maintenance)|"
                       r"(immediate|urgent) (repair|attention|action))\b", re.I)
CHANGE_CLAIM = re.compile(r"\b(has|have|had) (deteriorated|worsened|spread|increased|progressed|corroded|expanded)\b|"
                          r"\bmore (extensive|widespread|severe|pronounced|corrosion|rust)\b|"
                          r"\bnew (damage|corrosion|rust|crack\w*|dent\w*|blister\w*)\b|"
                          r"\b(has|have) been (restored|repaired|repainted|recoated|renewed)\b|"
                          r"\b(clearly|visibly|significantly|noticeably) (worse|improved|deteriorated|degraded)\b", re.I)
_QUAL = r"(no|not any) (visible |significant |noticeable |discernible |apparent |measurable |further )*"
NO_CHANGE_CLAIM = re.compile(rf"\b{_QUAL}(change|difference)s?\b|\bunchanged\b|\bremains? (the same|similar|unchanged)\b|"
                             r"\bcannot be (determined|established|assessed|confirmed)\b", re.I)
NO_WORSE_CLAIM = re.compile(rf"\b{_QUAL}(deterioration|degradation|new (damage|corrosion))\b", re.I)
NO_BETTER_CLAIM = re.compile(rf"\b{_QUAL}(improvement|repair|restoration)s?\b", re.I)
WORSE = re.compile(r"\b(has|have) (deteriorated|worsened|degraded)\b|\bmore (corrosion|rust|extensive (corrosion|rust))\b|"
                   r"\bnew (damage|corrosion|rust|crack\w*)\b", re.I)
BETTER = re.compile(r"\b(has|have) been (restored|repaired|repainted|recoated|renewed)\b|\b(visibly|clearly) improved\b", re.I)
VAGUE_REGIONS = {"whole image", "entire image", "whole photograph", "entire photograph", "whole photo", "entire photo",
                 "whole frame", "entire frame", "general", "overall", "everywhere", "various", "various areas",
                 "multiple areas", "all areas", "throughout", "na", "n a", "unknown", "unspecified", "not specified", "image", "photo"}
PLACEHOLDERS = {"na", "n a", "none", "nil", "not applicable", "no observations", "no observation", "unknown", "", "nothing visible",
                "not visible", "nothing"}


def evaluate(a: StrictConditionAssessment) -> list[Finding]:
    out: list[Finding] = []
    c = a.classification
    evidence = [("deterioration", e) for e in a.deterioration_evidence] + [("improvement", e) for e in a.improvement_evidence]

    # 1. Change attributed only to photographic factors.
    photo_only = [e for _, e in evidence if _asserts(e.change, PHOTO) and not PHYSICAL.search(e.change)]
    if evidence and len(photo_only) == len(evidence):
        out.append(Finding("photographic_change_only", "downgrade",
                           "the reported change is attributed only to lighting, camera or other photographic differences"))
    elif photo_only:
        out.append(Finding("photographic_change_partly", "review",
                           f"{len(photo_only)} evidence item(s) describe only photographic differences, not physical condition"))

    # 2. Maintenance activity asserted (photographs can only show results, never that work was done).
    texts = [a.explanation, *a.visual_observations, *(e.change for _, e in evidence), *(e.later_appearance for _, e in evidence)]
    if any(_asserts(t, MAINTENANCE, ignore_modal=True) for t in texts):
        if c is Classification.AMELIORATED and a.improvement_evidence:
            out.append(Finding("maintenance_claim", "review",
                               "a maintenance activity is asserted; only its visible result can be confirmed from photographs"))
        else:
            out.append(Finding("maintenance_claim_unsupported", "downgrade",
                               "a maintenance activity is asserted without supporting improvement evidence"))

    # 3. Recommendations presented as observations / evidence.
    if any(_asserts(t, RECOMMEND) for t in [*a.visual_observations, *(e.change for _, e in evidence), a.explanation]):
        out.append(Finding("recommendation", "review",
                           "the response contains maintenance recommendations, which are not observations"))

    # 4. Explanation contradicts the classification.
    exp = a.explanation
    contradiction = (
        (c is Classification.STABLE and _asserts(exp, CHANGE_CLAIM))
        or (c in (Classification.DETERIORATED, Classification.AMELIORATED) and NO_CHANGE_CLAIM.search(exp))
        or (c is Classification.DETERIORATED and NO_WORSE_CLAIM.search(exp))
        or (c is Classification.AMELIORATED and NO_BETTER_CLAIM.search(exp))
        or (c is Classification.DETERIORATED and _asserts(exp, BETTER) and not _asserts(exp, WORSE))
        or (c is Classification.AMELIORATED and _asserts(exp, WORSE) and not _asserts(exp, BETTER))
    )
    if contradiction:
        out.append(Finding("explanation_contradiction", "downgrade",
                           f"the explanation contradicts the {c.value} classification"))

    # 5. No real visual observations.
    if all(_norm(o) in PLACEHOLDERS for o in a.visual_observations):
        out.append(Finding("no_visual_observations", "downgrade", "no usable visual observations were reported"))

    # 6. Identical earlier / later descriptions offered as evidence of change.
    same = [e for _, e in evidence if _norm(e.earlier_appearance) == _norm(e.later_appearance)]
    if evidence and len(same) == len(evidence):
        out.append(Finding("identical_descriptions", "downgrade",
                           "the earlier and later descriptions are identical, so no change is evidenced"))
    elif same:
        out.append(Finding("identical_descriptions_partly", "review",
                           f"{len(same)} evidence item(s) describe the earlier and later photographs identically"))

    # 7. Vague evidence regions.
    vague = [e for _, e in evidence if _norm(e.region) in VAGUE_REGIONS or len(_norm(e.region)) < 3]
    if evidence and len(vague) == len(evidence):
        out.append(Finding("vague_regions", "downgrade", "no evidence item identifies a specific region of the photograph"))
    elif vague:
        out.append(Finding("vague_regions_partly", "review", f"{len(vague)} evidence item(s) do not identify a specific region"))

    # 8. HIGH confidence although the photographs only partly overlap.
    if a.confidence is Level.HIGH and a.same_area is SameArea.PARTIAL_OVERLAP:
        out.append(Finding("high_confidence_partial_overlap", "confidence",
                           "HIGH confidence is not supported when the photographs only partially overlap"))

    # 9. Limitations name lighting / camera differences.
    if any(_asserts(t, PHOTO) for t in a.limitations):
        out.append(Finding("photographic_limitations", "confidence" if a.confidence is Level.HIGH else "review",
                           "the stated limitations include lighting or camera differences"))
    return out
