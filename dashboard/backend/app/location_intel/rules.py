"""Layers 1 and 2: deterministic normalisation and matching against existing locations. No AI, no I/O."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

HIGH, MEDIUM = 0.90, 0.60        # confidence bands: >= HIGH high, >= MEDIUM medium, else low

# Whole-phrase spellings handled before punctuation is removed (apostrophes / ampersands carry meaning here).
_PHRASES = [
    (r"\bfo'?c'?s'?le\b|\bfo['’]?c['’]?s['’]?le\b|\bfcsle\b|\bfocsle\b|\bfo'c'sle\b", " forecastle "),
    (r"\bp\s*&\s*s\b|\bp\s*/\s*s\b", " port and starboard "),
    (r"\bx[\s-]?deck\b|\bcrossdeck\b", " cross deck "),
    (r"\bhold\s*(?:no\.?|nr\.?|#)?\s*(\d+)\b", r" hold \1 "),
]
# Single-token abbreviations and spelling variants.
_TOKENS = {
    "fwd": "forward", "fwrd": "forward", "frwd": "forward", "fore": "forward",
    "stbd": "starboard", "stb": "starboard", "sb": "starboard", "starbd": "starboard",
    "pt": "port", "ps": "port and starboard",
    "btw": "between", "btwn": "between", "betw": "between", "bet": "between",
    "forcastle": "forecastle", "forecastel": "forecastle", "fcstle": "forecastle",
    "dk": "deck", "dck": "deck", "accom": "accommodation", "accomm": "accommodation",
    "nav": "navigation", "mooring": "mooring", "winches": "winch", "holds": "hold",
    "of": "of", "&": "and", "n": "and",
}
_DROP = {"no", "nr", "num", "number", "the"}       # numbering words: "NO. 1" == "# 1" == "1"
_FILLER = {"area", "deck"}                         # ignored only when comparing against aliases / containment

# Known synonyms, applied ONLY when the canonical name already exists in the dataset (never invented).
ALIASES: dict[str, tuple[str, ...]] = {
    "FORECASTLE DECK AREA": ("forecastle", "forecastle deck", "forecastle area", "bow deck"),
    "POOP DECK AND MOORING WINCH": ("poop deck", "poop deck mooring winch", "aft mooring winch", "poop deck and winch"),
    "MONKEY ISLAND": ("monkey island deck", "compass deck"),
    "BRIDGE DECK": ("bridge", "navigation bridge deck", "nav bridge deck", "bridge wing deck"),
    "FUNNEL DECK AREA": ("funnel deck", "funnel", "funnel area"),
    "BOAT DECK AREA": ("boat deck", "lifeboat deck", "life boat deck"),
    "GANGWAY ENTRANCE": ("gangway", "gangway area", "accommodation gangway"),
    "RESCUE BOAT AND GARBAGE STORAGE DECK": ("rescue boat deck", "garbage storage deck", "rescue boat and garbage deck"),
    "TOP VIEW MAIN DECK": ("main deck top view", "top view of main deck"),
    "PORT AND STARBOARD MAIN DECK FACING FORWARD VIEW": ("port and starboard main deck forward view",
                                                         "port and starboard main deck facing forward",
                                                         "main deck forward view port and starboard"),
}


def key(text: Optional[str]) -> str:
    """Comparison key: case, spacing, punctuation, numbering words and known abbreviations removed."""
    if not text:
        return ""
    s = " " + str(text).casefold().replace("’", "'") + " "
    for pattern, repl in _PHRASES:
        s = re.sub(pattern, repl, s)
    s = s.replace("&", " and ")
    tokens = []
    for t in re.sub(r"[^0-9a-z]+", " ", s).split():
        t = _TOKENS.get(t, t)
        tokens.extend(x for x in t.split() if x not in _DROP)
    if "hold" in tokens and "cargo" not in tokens:               # "Hold 3" -> "cargo hold 3"
        tokens.insert(tokens.index("hold"), "cargo")
    return " ".join(tokens)


def _core(k: str) -> frozenset:
    return frozenset(t for t in k.split() if t not in _FILLER)


def _numbers(k: str) -> tuple:
    return tuple(t for t in k.split() if t.isdigit())


@dataclass
class RuleResult:
    method: str     # exact | normalized | managed_synonym | synonym | spelling | partial | ambiguous | unknown | missing
    normalized_location: Optional[str] = None
    confidence: float = 0.0
    candidates: list[str] = field(default_factory=list)
    explanation: str = ""

    @property
    def band(self) -> str:
        return band(self.confidence) if self.method != "missing" else "none"


def band(confidence: float) -> str:
    return "high" if confidence >= HIGH else "medium" if confidence >= MEDIUM else "low"


class KnownLocations:
    """The locations the dataset already uses (Layer 2). Names that differ only in case/punctuation are one
    location; the most frequently used spelling is the canonical one."""

    def __init__(self, names: Iterable[str], placeholder: str = "Not Labelled",
                 managed: Optional[Mapping[str, str]] = None):
        counts: dict[str, dict[str, int]] = {}
        ph = key(placeholder)
        for n in names:
            n = " ".join(str(n).split()) if n else ""
            k = key(n)
            if not k or k == ph:
                continue
            counts.setdefault(k, {}).setdefault(n, 0)
            counts[k][n] += 1
        self.by_key = {k: max(v.items(), key=lambda kv: (kv[1], kv[0]))[0] for k, v in counts.items()}
        self.names = sorted(self.by_key.values())
        self.placeholder_key = ph
        self.alias_keys: dict[str, str] = {}
        canon_by_key = {key(c): c for c in ALIASES}
        for ck, canon in canon_by_key.items():
            if ck in self.by_key:                                   # only for locations that already exist
                for alias in ALIASES[canon]:
                    self.alias_keys.setdefault(key(alias), self.by_key[ck])
        # Phase 10.3: administrator-managed synonyms {synonym text: canonical name}. Re-checked here on every build, so
        # a synonym is active only while its canonical is a trusted location, it is not itself a location, and its
        # location numbers agree with the canonical's (never "Hold 9" -> "CARGO HOLD NO. 3").
        # Phase 10.4: a synonym key mapped to two different canonicals (only possible by editing the file by hand) is
        # conflicting and is used by neither matching nor resolution.
        self.managed_keys: dict[str, str] = {}
        conflicting: set[str] = set()
        for syn, canon in sorted((managed or {}).items()):
            sk, ck = key(syn), key(canon)
            if sk and ck in self.by_key and sk not in self.by_key and _numbers(sk) == _numbers(ck)                     and sk != self.placeholder_key:
                if self.managed_keys.setdefault(sk, self.by_key[ck]) != self.by_key[ck]:
                    conflicting.add(sk)
        for sk in conflicting:
            del self.managed_keys[sk]

    def __contains__(self, name: str) -> bool:
        return key(name) in self.by_key

    def resolve(self, name: Optional[str]) -> Optional[str]:
        """The existing spelling of a location name, or None if the dataset does not use it."""
        return self.by_key.get(key(name)) if name else None

    def resolve_trusted(self, name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """Phase 10.4 -- for human decisions: (location, via). A trusted location name wins ("trusted_location"); else an
        active managed synonym gives its canonical ("managed_synonym"); else (None, None). Active synonyms already
        passed the number / trust / conflict checks above. resolve() is unchanged (AI validation keeps using it)."""
        k = key(name) if name else ""
        if k in self.by_key:
            return self.by_key[k], "trusted_location"
        if k in self.managed_keys:
            return self.managed_keys[k], "managed_synonym"
        return None, None


# Phase 13.3: adjacent-character transposition repair ("CAGRO" -> "CARGO"). Applied before matching; a repaired text
# can never score above TRANSPOSITION_CONFIDENCE and is always reported as a spelling correction.
TRANSPOSITION_MIN_LEN = 4
TRANSPOSITION_CONFIDENCE = 0.93          # same as an automatic spelling match; below "normalized" (0.98)
TRANSPOSITION_FUZZY_CAP = 0.81           # repaired AND still only a fuzzy match: medium band, human review


def _repair_vocabulary(known: KnownLocations) -> tuple[frozenset, frozenset]:
    """(repair targets, words never repaired), built once per KnownLocations. Targets come only from trusted location
    names, built-in aliases (of existing locations) and the abbreviation tables; managed-synonym words are protected
    but are not targets."""
    cached = getattr(known, "_repair_vocab", None)
    if cached is None:
        targets = {t for k in list(known.by_key) + list(known.alias_keys) for t in k.split() if t.isalpha()}
        targets |= {t for t in list(_TOKENS) + [w for v in _TOKENS.values() for w in v.split()] if t.isalpha()}
        targets |= {t for t in _DROP if t.isalpha()}
        protected = targets | {t for k in known.managed_keys for t in k.split() if t.isalpha()}
        cached = (frozenset(targets), frozenset(protected))
        known._repair_vocab = cached
    return cached


def _repair_transpositions(text: str, known: KnownLocations) -> tuple[str, list[tuple[str, str]]]:
    """Repair words that are one adjacent-letter swap away from exactly ONE vocabulary word. Words shorter than
    TRANSPOSITION_MIN_LEN, words already known, words the phrase rules recognise, and words with zero or several
    possible repairs are left unchanged. Digits are never touched (number agreement is unaffected).
    Returns (text, [(original word, corrected word), ...])."""
    targets, protected = _repair_vocabulary(known)
    fixes: list[tuple[str, str]] = []

    def fix(m: re.Match) -> str:
        word = m.group(0)
        w = word.casefold()
        if len(w) < TRANSPOSITION_MIN_LEN or w in protected or any(re.search(p, f" {w} ") for p, _ in _PHRASES):
            return word
        options = {w[:i] + w[i + 1] + w[i] + w[i + 2:] for i in range(len(w) - 1) if w[i] != w[i + 1]} & targets
        if len(options) != 1:
            return word
        repaired = options.pop()
        repaired = repaired.upper() if word.isupper() else repaired.capitalize() if word[:1].isupper() else repaired
        fixes.append((word, repaired))
        return repaired

    return re.sub(r"[A-Za-z]+", fix, text), fixes


def match(original: Optional[str], known: KnownLocations) -> RuleResult:
    text = " ".join(str(original).split()) if original else ""
    k = key(text)
    if not k or k == known.placeholder_key:
        return RuleResult("missing", explanation="No location text in the workbook.")
    repaired, fixes = _repair_transpositions(text, known)
    if not fixes:
        return _match(text, known)
    r = _match(repaired, known)
    note = "Corrected " + ", ".join(f"'{a}' to '{b}'" for a, b in fixes) + " (swapped letters). "
    if r.method in ("exact", "normalized", "managed_synonym", "synonym"):
        return RuleResult("spelling", r.normalized_location, TRANSPOSITION_CONFIDENCE,
                          explanation=note + "Then matches an existing location.")
    if r.method == "spelling":   # a further fuzzy match on top of the repair: never automatic
        return RuleResult("spelling", r.normalized_location, min(r.confidence, TRANSPOSITION_FUZZY_CAP),
                          explanation=note + r.explanation)
    if r.method == "unknown":    # the repair did not help: report the original text's own result
        return _match(text, known)
    return RuleResult(r.method, r.normalized_location, r.confidence, r.candidates, note + r.explanation)


def _match(text: str, known: KnownLocations) -> RuleResult:
    """The deterministic matching steps (unchanged since Phase 10): exact, normalized, managed synonym, alias, spelling,
    containment / ambiguity."""
    k = key(text)
    if text in known.names:
        return RuleResult("exact", text, 1.0, explanation="Exactly matches an existing location.")
    if k in known.by_key:
        return RuleResult("normalized", known.by_key[k], 0.98,
                          explanation="Matches an existing location after ignoring case, spacing, punctuation and abbreviations.")
    if k in known.managed_keys:
        return RuleResult("managed_synonym", known.managed_keys[k], 0.97,
                          explanation="An administrator-managed synonym of an existing location.")
    if k in known.alias_keys:
        return RuleResult("synonym", known.alias_keys[k], 0.95, explanation="A known synonym of an existing location.")
    # Spelling variations: close matches whose numbers agree (Cargo Hold 1 must never become Cargo Hold 7).
    scored = sorted(((difflib.SequenceMatcher(None, k, kk).ratio(), name) for kk, name in known.by_key.items()
                     if _numbers(kk) == _numbers(k)), reverse=True)
    if scored and scored[0][0] >= 0.85:
        best, name = scored[0]
        rivals = [n for r, n in scored[1:] if r >= best - 0.03]
        if rivals:
            return RuleResult("ambiguous", None, round(best * 0.6, 2), [name] + rivals[:4],
                              "Close to several existing locations.")
        conf = 0.93 if best >= 0.92 else round(0.6 + (best - 0.85) * 3, 2)
        return RuleResult("spelling", name, conf, explanation=f"Probable spelling variation ({best:.0%} similar).")
    # Containment: every word given appears in existing location names (e.g. "Cargo Hold" -> 7 holds).
    core = _core(k)
    contained = [name for kk, name in known.by_key.items() if core and core <= _core(kk) and
                 (not _numbers(k) or _numbers(k) == _numbers(kk))]
    if len(contained) == 1:
        return RuleResult("partial", contained[0], 0.7, explanation="All words appear in one existing location.")
    if contained:
        return RuleResult("ambiguous", None, 0.4, sorted(contained)[:8], f"Could be any of {len(contained)} existing locations.")
    return RuleResult("unknown", explanation="Does not match any existing location.")
