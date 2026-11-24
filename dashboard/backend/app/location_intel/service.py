"""Combine the layers for an import and decide what may be applied automatically."""

from __future__ import annotations

import concurrent.futures
import logging
from collections import OrderedDict
from typing import Iterable, Optional

from .providers import (InvalidResponse, LocationQuery, ProviderUnavailable, build_provider, dedicated_key_configured,
                        validate_response)
from .rules import HIGH, MEDIUM, KnownLocations, band, key, match

log = logging.getLogger(__name__)
AUTO_METHODS = {"exact", "normalized", "managed_synonym", "synonym", "spelling"}   # deterministic methods allowed to apply on their own
# Sent to AI when not confidently resolved. Phase 9.4: "ambiguous" is NOT sent -- the AI must not be asked to force a
# choice between several existing locations; the ambiguity is preserved for the reviewer.
AI_METHODS = {"unknown", "partial", "spelling"}
# Placeholder texts that are not locations: never sent to the AI (it can only guess).
NON_LOCATION_KEYS = frozenset(key(t) for t in ("N/A", "NA", "TBC", "TBA", "TBD", "Nil", "None", "Unknown", "Not applicable",
                                                "Not known", "No location"))
# AI confidence is self-reported and was poorly calibrated in the 9.3b evaluation: it is capped below the "high" band
# and kept separate from the rule confidence, so an AI answer never looks like a deterministic match.
AI_CONFIDENCE_CAP = round(HIGH - 0.01, 2)
REVIEW_STATES = ("not_required", "auto_applied", "pending", "accepted", "rejected", "kept_original", "manual")
AGREEMENT = ("agrees", "disagrees", "abstained", "no_rule_candidate", "no_valid_answer")


class LocationIntelligence:
    def __init__(self, settings, provider=None):
        self.settings = settings
        self.provider = provider or build_provider(settings)
        self._cache: "OrderedDict[tuple, tuple]" = OrderedDict()

    # ------------------------------------------------------------ configuration status (never the key)
    def status(self) -> dict:
        p = self.provider
        return {"enabled": bool(p.available), "provider": p.name, "simulated": bool(p.simulated),
                "live": bool(p.available and not p.simulated), "model": getattr(p, "model", None) or self.settings.location_ai_model,
                "key_configured": bool(getattr(p, "key_configured", dedicated_key_configured())), "message": p.message}

    # ------------------------------------------------------------ AI layer with full fallback
    def _ask(self, queries: list[LocationQuery], known: KnownLocations, cache_key) -> tuple[dict, str, list[str]]:
        if not queries:
            return {}, "not_needed", []
        if not self.provider.available:
            return {}, "disabled", [self.provider.message]
        ck = (cache_key, tuple(sorted(key(q.original) for q in queries)), tuple(known.names))
        if ck in self._cache:
            return self._cache[ck]
        timeout = float(self.settings.location_ai_timeout_s)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            raw = pool.submit(self.provider.suggest, queries, known.names, self.settings.location_ai_model).result(timeout=timeout)
            found, problems = validate_response(raw, queries, known)
            result = (found, "ok", problems)
        except concurrent.futures.TimeoutError:
            result = ({}, "failed", [f"The location AI did not answer within {timeout:g} s."])
        except InvalidResponse as exc:
            result = ({}, "invalid", [f"The location AI response was rejected: {exc}."])
        except ProviderUnavailable as exc:
            result = ({}, "disabled", [str(exc)])
        except Exception as exc:  # noqa: BLE001 - any provider failure falls back to rules + review
            log.warning("location AI provider failed (%s)", type(exc).__name__)
            result = ({}, "failed", ["The location AI is unavailable; rules and human review are used instead."])
        finally:
            pool.shutdown(wait=False)
        if result[1] == "ok":
            self._cache[ck] = result
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
        return result

    # ------------------------------------------------------------ public entry point
    def annotate(self, rows: Iterable, known: KnownLocations, cache_key: Optional[str] = None) -> dict:
        """Attach ``location_intel`` to every non-blank row. The row's ``official_location`` (the Excel text) is
        never changed. Returns a summary for the preview."""
        rows = [r for r in rows if r.status != "skipped"]
        results = {id(r): match(r.official_location, known) for r in rows}
        queries: dict[str, LocationQuery] = {}
        for r in rows:
            rr = results[id(r)]
            if rr.method in AI_METHODS and not (rr.method in AUTO_METHODS and rr.confidence >= HIGH) \
                    and key(r.official_location) not in NON_LOCATION_KEYS:
                queries.setdefault(key(r.official_location), LocationQuery(
                    original=" ".join(r.official_location.split()), worksheet=r.worksheet_name,
                    workbook=r.original_excel_filename, image_filename=r.image_filename,
                    source_cell=getattr(r, "source_cell", None), rule_candidates=tuple(rr.candidates)))
        found, ai_status, ai_problems = self._ask(list(queries.values()), known, cache_key)
        counts = dict.fromkeys(("auto_applied", "not_required", "ai_suggestions", "review_required", "missing", "unknown",
                                "ambiguous", "ai_disagreements"), 0)
        for r in rows:
            rr = results[id(r)]
            original = " ".join(r.official_location.split()) if r.official_location else None
            k = key(original or "")
            sent = k in queries
            s = found.get(k) if sent else None
            auto = rr.method in AUTO_METHODS and rr.confidence >= HIGH
            # the rule's own (unapplied) candidate stays visible next to any AI suggestion -- never silently replaced
            rule_candidate = rr.normalized_location if not auto and rr.method in ("partial", "spelling") else None
            agreement = _agreement(sent and ai_status == "ok", s, rule_candidate)
            ai_conf = min(s.confidence, AI_CONFIDENCE_CAP) if s else None
            info = {
                "original_location": original,
                "normalized_location": rr.normalized_location,
                "rule_method": rr.method,
                "rule_confidence": rr.confidence,
                "rule_candidate": rule_candidate,
                "candidates": rr.candidates,
                "explanation": rr.explanation,
                "ai_status": ai_status if sent else "not_sent" if rr.method == "ambiguous" or k in NON_LOCATION_KEYS
                else "not_needed",
                "ai_suggestion": s.location if s else None,
                "ai_confidence": ai_conf,                                   # capped; never in the "high" band
                "ai_confidence_band": band(ai_conf) if s and s.location else None,
                "ai_reason": s.reason if s else None,
                "ai_agreement": agreement,
                # AI-generated labelling and review flags (all still require a person's decision)
                "ai_provider": self.provider.name if sent and ai_status == "ok" else None,
                "ai_simulated": bool(self.provider.simulated) if sent and ai_status == "ok" else None,
                "ai_alternatives": list(s.alternatives) if s else [],
                "ai_ambiguous": bool(s and s.alternatives),
                "ai_low_confidence": bool(s and s.location and band(ai_conf) == "low"),
            }
            method = "ai_suggestion" if not auto and s and s.location else rr.method
            info.update(normalization_method=method,
                        # rule confidence only: AI confidence is reported separately (ai_confidence / ai_confidence_band)
                        confidence=rr.confidence, confidence_band="none" if rr.method == "missing" else band(rr.confidence),
                        review_required=not auto, review_status=("not_required" if rr.method == "exact" else "auto_applied")
                        if auto else "pending",
                        review_reason=None if auto else _review_reason(rr, k, sent, ai_status, agreement)
                        + _ai_flags(info),
                        # what the record shows until someone decides: an automatic match, else the Excel text
                        applied_location=rr.normalized_location if auto else original)
            r.location_intel = info
            counts["auto_applied"] += info["review_status"] == "auto_applied"
            counts["not_required"] += info["review_status"] == "not_required"
            counts["ai_suggestions"] += bool(info["ai_suggestion"])
            counts["review_required"] += info["review_required"]
            counts["missing"] += rr.method == "missing"
            counts["unknown"] += rr.method == "unknown" and not info["ai_suggestion"]
            counts["ambiguous"] += rr.method == "ambiguous"
            counts["ai_disagreements"] += agreement == "disagrees"
        return {"ai": {**self.status(), "used": ai_status, "problems": ai_problems[:10]}, "counts": counts}


def _agreement(answered: bool, s, rule_candidate: Optional[str]) -> Optional[str]:
    """How the AI answer relates to the rule candidate (None when the AI was not asked or did not answer)."""
    if not answered:
        return None
    if s is None:
        return "no_valid_answer"                    # the item was rejected by validation (or missing from the reply)
    if s.location is None:
        return "abstained"
    if rule_candidate is None:
        return "no_rule_candidate"
    return "agrees" if s.location == rule_candidate else "disagrees"


def _ai_flags(info: dict) -> str:
    """Extra review notes for an ambiguous or low-confidence AI answer."""
    notes = ""
    if info.get("ai_ambiguous"):
        notes += f" The AI found several plausible locations ({', '.join(info['ai_alternatives'])})."
    if info.get("ai_low_confidence"):
        notes += " The AI's confidence is low."
    return notes


def _review_reason(rr, k: str, sent: bool, ai_status: str, agreement: Optional[str]) -> str:
    """One plain sentence telling the reviewer why this row needs a decision."""
    if rr.method == "missing":
        return "No location text in the workbook."
    if k in NON_LOCATION_KEYS:
        return "The workbook text is a placeholder, not a location; the AI is not asked to guess."
    if rr.method == "ambiguous":
        return f"Ambiguous: could be any of {len(rr.candidates)} existing locations; the AI is not asked to choose."
    rule = "The rules found a possible match that needs confirmation." if rr.method in ("partial", "spelling") \
        else "The rules found no matching location."
    if not sent or ai_status != "ok":
        return f"{rule} No AI suggestion is available ({ai_status if sent else 'not asked'})."
    return {
        "disagrees": "The AI suggestion differs from the rule candidate; choose one, keep the original or enter a location.",
        "agrees": "The AI suggestion agrees with the rule candidate; AI suggestion requires review.",
        "no_rule_candidate": f"{rule} AI suggestion only; requires review.",
        "abstained": f"{rule} The AI found no supported location.",
        "no_valid_answer": f"{rule} The AI answer was rejected by validation.",
    }[agreement]


def effective_location(row: dict, decision: Optional[dict]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(location the record shows, method, review status) for a committed manifest row."""
    original = row.get("official_location")
    info = row.get("location_intel")
    if decision:
        action = decision.get("action")
        if action in ("accept", "manual"):
            return decision.get("location"), "manual" if action == "manual" else "approved_suggestion", \
                "accepted" if action == "accept" else "manual"
        return original, "original", "rejected" if action == "reject" else "kept_original"
    if info:
        return info.get("applied_location", original), info.get("normalization_method"), info.get("review_status")
    return original, None, None


__all__ = ["LocationIntelligence", "KnownLocations", "effective_location", "MEDIUM", "HIGH", "REVIEW_STATES"]
