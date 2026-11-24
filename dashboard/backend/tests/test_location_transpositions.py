"""Phase 13.3 -- adjacent-character transposition repair in the location rules. Synthetic data only."""

import pytest

from app.location_intel.providers import LocationQuery, validate_response
from app.location_intel.rules import (HIGH, KnownLocations, TRANSPOSITION_CONFIDENCE, _numbers, _repair_transpositions,
                                      key, match)
from app.location_intel.service import AUTO_METHODS

NAMES = ["BOAT DECK AREA", "BRIDGE DECK", *[f"CARGO HOLD NO. {i}" for i in range(1, 8)], "Cross deck Between # 1&2",
         "Cross deck Between # 2&3", "Cross deck Between # 3&4", "Cross deck Between # 4&5", "Cross deck Fwd of # 1",
         "Cross deck between # 5&6", "Cross deck between # 6&7", "FORECASTLE DECK AREA", "FUNNEL DECK AREA",
         "GANGWAY ENTRANCE", "MONKEY ISLAND", "POOP DECK AND MOORING WINCH",
         "PORT AND STARBOARD MAIN DECK FACING FORWARD VIEW", "RESCUE BOAT AND GARBAGE STORAGE DECK", "TOP VIEW MAIN DECK"]


@pytest.fixture(scope="module")
def known():
    return KnownLocations(NAMES, "Not Labelled")


def _auto(r):
    return r.method in AUTO_METHODS and r.confidence >= HIGH


# ------------------------------------------------------------------ positive cases
@pytest.mark.parametrize("text, expected, corrected", [
    ("CAGRO HOLD NO. 1", "CARGO HOLD NO. 1", ["'CAGRO' to 'CARGO'"]),
    ("CRAGO HLOD NO. 4", "CARGO HOLD NO. 4", ["'CRAGO' to 'CARGO'", "'HLOD' to 'HOLD'"]),
    ("TOP VEIW MAIN DECK", "TOP VIEW MAIN DECK", ["'VEIW' to 'VIEW'"]),
    ("Cross deck Bewteen # 1&2", "Cross deck Between # 1&2", ["'Bewteen' to 'Between'"]),
    ("BAOT DECK", "BOAT DECK AREA", ["'BAOT' to 'BOAT'"]),
    ("POOP DEKC", "POOP DECK AND MOORING WINCH", ["'DEKC' to 'DECK'"]),
    ("GANGWYA", "GANGWAY ENTRANCE", ["'GANGWYA' to 'GANGWAY'"]),
    ("BRDIGE", "BRIDGE DECK", ["'BRDIGE' to 'BRIDGE'"]),
    ("cargo hlod 7", "CARGO HOLD NO. 7", ["'hlod' to 'hold'"]),
])
def test_transposition_is_repaired_as_a_spelling_match(known, text, expected, corrected):
    r = match(text, known)
    assert (r.method, r.normalized_location, r.confidence) == ("spelling", expected, TRANSPOSITION_CONFIDENCE)
    assert _auto(r) and r.band == "high"
    for c in corrected:
        assert c in r.explanation


def test_repair_never_gets_normalized_confidence(known):
    r = match("CAGRO HOLD NO. 1", known)
    assert r.confidence == 0.93 < match("cargo hold #1", known).confidence == 0.98


# ------------------------------------------------------------------ negative / safety cases
@pytest.mark.parametrize("text", [
    "CAGRO HOLD NO. 9",            # number not in the dataset
    "CAGRO HOLD NO. 12",           # 12 is neither hold 1 nor hold 2
    "Cross deck Bewteen # 1&3",    # number pair that does not exist
    "DEKC",                        # a single generic word
    "HATCH COAMING",               # not a location
    "ENGINE ROOM",                 # unknown vocabulary
])
def test_unresolvable_inputs_stay_unresolved(known, text):
    r = match(text, known)
    assert r.method == "unknown" and r.normalized_location is None and not _auto(r)


@pytest.mark.parametrize("text, n_candidates", [
    ("CAGRO HOLD", 7),             # no number: any of the seven holds
    ("Cross deck Bewteen", 6),     # no number pair
    ("MAIN DEKC", 2),              # two main-deck locations
])
def test_repaired_but_ambiguous_goes_to_review(known, text, n_candidates):
    r = match(text, known)
    assert r.method == "ambiguous" and r.normalized_location is None and len(r.candidates) == n_candidates
    assert not _auto(r) and "Corrected" in r.explanation


def test_words_shorter_than_four_letters_are_not_repaired(known):
    assert _repair_transpositions("TPO VIEW DKE ON. 3", known) == ("TPO VIEW DKE ON. 3", [])


def test_two_possible_repairs_leave_the_word_unchanged():
    k = KnownLocations(["SLAT DECK", "LAST DECK"])
    assert _repair_transpositions("LSAT DECK", k) == ("LSAT DECK", [])        # LSAT -> SLAT or LAST
    r = match("LSAT DECK", k)
    assert not _auto(r) and "Corrected" not in r.explanation


def test_digits_are_never_swapped(known):
    assert _repair_transpositions("CARGO HOLD NO. 21", known)[1] == []
    assert match("CARGO HOLD NO. 21", known).method == "unknown"


@pytest.mark.parametrize("text, method, location, conf", [
    ("BRIDGE DECK", "exact", "BRIDGE DECK", 1.0),
    ("cargo hold #3", "normalized", "CARGO HOLD NO. 3", 0.98),
    ("Hold #3", "normalized", "CARGO HOLD NO. 3", 0.98),
    ("FCSLE", "synonym", "FORECASTLE DECK AREA", 0.95),
    ("Forcastle deck area", "normalized", "FORECASTLE DECK AREA", 0.98),
    ("Funel deck area", "spelling", "FUNNEL DECK AREA", 0.93),
    ("Not Labelled", "missing", None, 0.0),
])
def test_existing_behaviour_is_unchanged(known, text, method, location, conf):
    r = match(text, known)
    assert (r.method, r.normalized_location, r.confidence) == (method, location, conf)
    assert "Corrected" not in r.explanation


def test_a_further_fuzzy_match_after_a_repair_needs_review(known):
    r = match("Funel dek arae", known)                 # 'arae' -> 'area' repaired, 'Funel dek' still only similar
    assert r.method == "spelling" and r.normalized_location == "FUNNEL DECK AREA" and r.confidence < HIGH
    assert "'arae' to 'area'" in r.explanation and not _auto(r)


def test_managed_synonym_words_are_protected():
    k = KnownLocations(NAMES, managed={"Wheelhouse Top": "MONKEY ISLAND", "Lifebaot station": "BOAT DECK AREA"})
    assert match("Wheelhouse Top", k).method == "managed_synonym"
    assert _repair_transpositions("Lifebaot station", k) == ("Lifebaot station", [])   # a managed word is never "fixed"
    assert match("Lifebaot station", k).method == "managed_synonym"


def test_ai_validation_is_unchanged(known):
    q = [LocationQuery("CAGRO HOLD NO. 1")]
    found, problems = validate_response({"suggestions": [{"original": "CAGRO HOLD NO. 1", "location": "CARGO HOLD NO. 3",
                                                          "confidence": 0.8, "reason": "x"}]}, q, known)
    assert found == {} and "number" in problems[0]
    found, _ = validate_response({"suggestions": [{"original": "CAGRO HOLD NO. 1", "location": "CARGO HOLD NO. 1",
                                                   "confidence": 0.8, "reason": "x"}]}, q, known)
    assert found[key("CAGRO HOLD NO. 1")].location == "CARGO HOLD NO. 1"


# ------------------------------------------------------------------ property test (Phase 13.2 synthetic set)
def _swaps(word):
    return [word[:i] + word[i + 1] + word[i] + word[i + 2:] for i in range(len(word) - 1)
            if word[i] != word[i + 1] and word[i].isalpha() and word[i + 1].isalpha()]


def _generated_cases():
    cases = []
    for n in NAMES:
        ws = n.split()
        for wi, w in enumerate(ws):                                   # every single adjacent transposition
            cases += [(" ".join(ws[:wi] + [s] + ws[wi + 1:]), n) for s in _swaps(w)]
        idx = [i for i, w in enumerate(ws) if w.isalpha() and _swaps(w)]
        for a in idx:                                                 # two transpositions in two different words
            for b in idx:
                if a < b:
                    v = ws[:]
                    v[a], v[b] = _swaps(ws[a])[0], _swaps(ws[b])[-1]
                    cases.append((" ".join(v), n))
    return cases


def test_property_no_wrong_automatic_match_on_generated_transpositions(known):
    cases = _generated_cases()
    assert len(cases) == 383                                          # 281 single + 102 double (Phase 13.2)
    auto = 0
    for text, label in cases:
        r = match(text, known)
        if _auto(r):
            auto += 1
            assert r.normalized_location == label, text                # never a wrong automatic match
            assert r.normalized_location in known.names                # only trusted locations
            assert _numbers(key(r.normalized_location)) == _numbers(key(text)), text   # number agreement
        if r.method == "ambiguous":
            assert not _auto(r) and r.normalized_location is None
    # Measured 350/383 (rules before Phase 13.3: 286). Lower than the 13.2 simulation (371) by design: a repaired text
    # that still needs a fuzzy match (e.g. 'ADN', 'fo' are too short to repair) is capped at 0.81 and goes to review.
    assert auto >= 350


def test_property_wrong_numbers_never_resolve(known):
    for i in range(1, 8):
        for j in (8, 9, 10, 11):
            r = match(f"CAGRO HOLD NO. {j}", known)
            assert r.normalized_location is None and not _auto(r)
        r = match(f"CAGRO HOLD NO. {i}", known)
        assert r.normalized_location == f"CARGO HOLD NO. {i}" and _auto(r)
