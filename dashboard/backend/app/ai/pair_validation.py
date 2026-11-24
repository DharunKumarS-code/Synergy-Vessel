"""Image-pair eligibility checks run BEFORE any AI request (Phase 5A).

Keeps five ideas separate:
  A. same physical file        -> identical SHA-256 (blocking)
  B. visually identical photo  -> perceptual hashes (near) identical although the bytes differ
  C. similar photo             -> reported as distances only; similarity is NOT evidence of anything
  D. same inspection area      -> only metadata (Official / AI location) can suggest it; never proven here
  E. different inspection view -> cannot be determined automatically; the user selects the pair

Perceptual hashes (pHash, dHash; 64-bit) are computed with Pillow only, from image *content*
(never file names or image ids), for just the two selected photographs. Results are cached in
memory by SHA-256 (bounded; no files are written). Original files are opened read-only and never
re-encoded or modified.
"""

from __future__ import annotations

import hashlib
import io
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal, Optional

from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError
from pydantic import BaseModel, Field

from ..models import InspectionRecord
from ..services.image_service import ImageNotFound, InvalidImageId
from ..services.inspection_store import InspectionStore, normalise

# --------------------------------------------------------------------------- thresholds
@dataclass(frozen=True)
class PairThresholds:
    """Hamming distances out of 64 bits, calibrated on the 609 unique real photographs:
    - copies of one photograph (re-saved JPEG q50, PNG, resized, +/-15% tone) measured <= 2 / <= 1;
    - the same photograph cropped by 3% measured 8 / 7;
    - the closest pair of separately taken photographs measured 4 / 5 (W38-0019 / W41-0019); every
      other distinct pair had a dHash distance >= 9. Unrelated areas can still be as close as 6-12,
      so similarity is never used as evidence that two photographs show the same area."""

    phash_identical: int = 3
    dhash_identical: int = 3
    phash_near: int = 8
    dhash_near: int = 8
    min_short_side_px: int = 240
    dark_mean: float = 30.0
    bright_mean: float = 228.0
    flat_stddev: float = 14.0
    blur_edge_mean: float = 2.5


Severity = Literal["error", "warning", "info"]
Similarity = Literal["IDENTICAL_FILE", "VISUALLY_IDENTICAL", "NEAR_DUPLICATE", "NOT_DUPLICATE", "UNKNOWN"]
LocationMatch = Literal["OFFICIAL", "AI_ONLY", "MIXED", "CONFLICT", "INSUFFICIENT"]
QualityLevel = Literal["GOOD", "ACCEPTABLE", "POOR", "UNREADABLE"]


class Issue(BaseModel):
    code: str
    severity: Severity
    message: str


class ImageCheck(BaseModel):
    record_id: str
    image_id: Optional[str] = None
    readable: bool = False
    sha256_recorded: Optional[str] = None
    sha256_actual: Optional[str] = None
    sha256_matches_record: Optional[bool] = None
    format: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    brightness: Optional[float] = None
    contrast: Optional[float] = None
    sharpness: Optional[float] = None
    quality: QualityLevel = "UNREADABLE"
    quality_notes: list[str] = Field(default_factory=list)


class PerceptualCheck(BaseModel):
    phash_distance: Optional[int] = None
    dhash_distance: Optional[int] = None
    similarity: Optional[float] = Field(None, description="1 - mean(pHash, dHash distance)/64; NOT a same-area score")
    category: Similarity = "UNKNOWN"


class LocationCheck(BaseModel):
    match: LocationMatch
    earlier_official: Optional[str] = None
    later_official: Optional[str] = None
    earlier_ai: Optional[str] = None
    later_ai: Optional[str] = None
    explanation: str


class PairValidation(BaseModel):
    valid: bool = Field(description="False when any error-level issue exists: AI analysis is blocked")
    requires_acknowledgement: bool = Field(description="Warning-level issues the user must acknowledge first")
    requires_human_review: bool = Field(description="Any resulting AI analysis must be reviewed by a person")
    sha256_duplicate: bool = False
    date_order_valid: Optional[bool] = None
    vessel_match: Optional[bool] = None
    perceptual: PerceptualCheck = Field(default_factory=PerceptualCheck)
    location: Optional[LocationCheck] = None
    earlier: Optional[ImageCheck] = None
    later: Optional[ImageCheck] = None
    errors: list[Issue] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)
    info: list[Issue] = Field(default_factory=list)
    notice: str = ("Perceptual hashing detects copies of the same photograph; it cannot prove that two "
                   "photographs show the same physical area or the same view.")

    def first_error(self) -> Optional[Issue]:
        return self.errors[0] if self.errors else None

    def summary(self) -> dict:
        """Compact form stored with a saved analysis."""
        return {
            "valid": self.valid,
            "requires_human_review": self.requires_human_review,
            "sha256_duplicate": self.sha256_duplicate,
            "perceptual": self.perceptual.model_dump(),
            "location_match": self.location.match if self.location else None,
            "warnings": [w.code for w in self.warnings],
            "image_quality": [self.earlier.quality if self.earlier else None, self.later.quality if self.later else None],
        }


# --------------------------------------------------------------------------- hashing (Pillow only)
_DCT_N = 32
_COS = [[math.cos((2 * x + 1) * u * math.pi / (2 * _DCT_N)) for x in range(_DCT_N)] for u in range(8)]


def _bits_to_int(bits) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def dhash(gray: Image.Image) -> int:
    """Difference hash: 9x8 grayscale, compare horizontal neighbours."""
    px = list(gray.resize((9, 8), Image.Resampling.LANCZOS).getdata())
    return _bits_to_int(px[r * 9 + c] < px[r * 9 + c + 1] for r in range(8) for c in range(8))


def phash(gray: Image.Image) -> int:
    """DCT hash: 32x32 grayscale -> 8x8 lowest frequencies (DC excluded from the median)."""
    px = list(gray.resize((_DCT_N, _DCT_N), Image.Resampling.LANCZOS).getdata())
    rows = [px[i * _DCT_N:(i + 1) * _DCT_N] for i in range(_DCT_N)]
    # separable 2-D DCT-II restricted to the 8x8 low-frequency block
    tmp = [[sum(_COS[u][x] * row[x] for x in range(_DCT_N)) for u in range(8)] for row in rows]          # 32 x 8
    coeffs = [sum(_COS[v][y] * tmp[y][u] for y in range(_DCT_N)) for v in range(8) for u in range(8)]  # 8 x 8
    ac = coeffs[1:]
    median = sorted(ac)[len(ac) // 2]
    return _bits_to_int(c > median for c in coeffs)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass(frozen=True)
class _Analysed:
    phash: int
    dhash: int
    format: str
    width: int
    height: int
    brightness: float
    contrast: float
    sharpness: float


class _Cache:
    def __init__(self, size: int = 2048):
        self.size, self._d, self._lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
        return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)

    def __len__(self):
        return len(self._d)


CACHE = _Cache()


def analyse_image(data: bytes, sha256: str) -> _Analysed:
    """Decode (in memory) and measure one image. Raises ValueError if it cannot be decoded."""
    cached = CACHE.get(sha256)
    if cached is not None:
        return cached
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format or "UNKNOWN"
            im = ImageOps.exif_transpose(im)
            im.load()
            width, height = im.size
            gray = im.convert("L")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("image could not be decoded") from exc
    small = gray.copy()
    small.thumbnail((512, 512), Image.Resampling.LANCZOS)  # measurement copy only
    stat = ImageStat.Stat(small)
    edges = ImageStat.Stat(small.filter(ImageFilter.FIND_EDGES))
    result = _Analysed(phash=phash(gray), dhash=dhash(gray), format=fmt, width=width, height=height,
                       brightness=round(stat.mean[0], 1), contrast=round(stat.stddev[0], 1),
                       sharpness=round(edges.mean[0], 2))
    CACHE.put(sha256, result)
    return result


# --------------------------------------------------------------------------- validator
def _loc(text: Optional[str]) -> Optional[str]:
    return " ".join(text.split()) if text and text.strip() else None


class PairValidator:
    def __init__(self, store: InspectionStore, thresholds: PairThresholds | None = None):
        self.store = store
        self.t = thresholds or PairThresholds()

    # -- one side ----------------------------------------------------------
    def _check_image(self, rec: InspectionRecord, side: str, v: PairValidation) -> tuple[ImageCheck, Optional[_Analysed], Optional[bytes]]:
        p = rec.extracted_photograph
        chk = ImageCheck(record_id=rec.record_id, image_id=rec.image_id, sha256_recorded=p.sha256 if p else None)
        if rec.image_status != "resolved" or not rec.image_id:
            v.errors.append(Issue(code="photograph_unavailable", severity="error",
                                  message=f"The {side} photograph is not available for this inspection record."))
            return chk, None, None
        try:
            path, _ = self.store.images.resolve(rec.image_id)
            with open(path, "rb") as f:  # read-only
                data = f.read()
        except (InvalidImageId, ImageNotFound, OSError):
            v.errors.append(Issue(code="photograph_unavailable", severity="error",
                                  message=f"The {side} photograph file could not be found or read."))
            return chk, None, None
        actual = hashlib.sha256(data).hexdigest()
        chk.sha256_actual = actual
        if chk.sha256_recorded:
            chk.sha256_matches_record = actual == chk.sha256_recorded
            if not chk.sha256_matches_record:
                v.errors.append(Issue(code="file_integrity", severity="error",
                                      message=f"The {side} photograph file no longer matches its recorded SHA-256 fingerprint."))
        else:
            v.info.append(Issue(code="sha256_not_recorded", severity="info",
                                message=f"The {side} record has no recorded SHA-256; the file's own hash was used."))
        try:
            a = analyse_image(data, actual)
        except ValueError:
            v.errors.append(Issue(code="photograph_unreadable", severity="error",
                                  message=f"The {side} photograph file is damaged or not a readable image."))
            return chk, None, data
        chk.readable = True
        chk.format, chk.width, chk.height = a.format, a.width, a.height
        chk.brightness, chk.contrast, chk.sharpness = a.brightness, a.contrast, a.sharpness
        notes = []
        if min(a.width, a.height) < self.t.min_short_side_px:
            notes.append(f"low resolution ({a.width}x{a.height} px)")
        if a.brightness < self.t.dark_mean:
            notes.append("very dark")
        elif a.brightness > self.t.bright_mean:
            notes.append("very bright / overexposed")
        if a.contrast < self.t.flat_stddev:
            notes.append("very low contrast (possibly obstructed or featureless)")
        if a.sharpness < self.t.blur_edge_mean:
            notes.append("very little edge detail (possibly blurred)")
        chk.quality_notes = notes
        chk.quality = "POOR" if notes else ("GOOD" if min(a.width, a.height) >= 480 else "ACCEPTABLE")
        if notes:
            v.warnings.append(Issue(code="image_quality", severity="warning",
                                    message=f"The {side} photograph may be unsuitable for visual comparison: {', '.join(notes)}."))
        return chk, a, data

    # -- location ----------------------------------------------------------
    @staticmethod
    def _location(e: InspectionRecord, l: InspectionRecord) -> LocationCheck:
        eo = _loc(e.official_location) if e.official_location_labelled else None
        lo = _loc(l.official_location) if l.official_location_labelled else None
        ea, la = _loc(e.ai_suggested_location), _loc(l.ai_suggested_location)
        same = lambda a, b: bool(a and b and normalise(a) == normalise(b))  # noqa: E731
        common = dict(earlier_official=eo, later_official=lo, earlier_ai=ea, later_ai=la)
        if eo and lo:
            if same(eo, lo):
                return LocationCheck(match="OFFICIAL", explanation="Both records have the same Official Location.", **common)
            return LocationCheck(match="CONFLICT", explanation=f"The Official Locations differ (“{eo}” vs “{lo}”).", **common)
        if eo or lo:
            official, other_ai = (eo, la) if eo else (lo, ea)
            if same(official, other_ai):
                return LocationCheck(match="MIXED", explanation="One record has an Official Location; the other matches it only through its AI-suggested location.", **common)
            if other_ai:
                return LocationCheck(match="CONFLICT", explanation="The Official Location of one record does not match the AI-suggested location of the other.", **common)
            return LocationCheck(match="INSUFFICIENT", explanation="Only one record has any location information.", **common)
        if ea and la:
            if same(ea, la):
                return LocationCheck(match="AI_ONLY", explanation="Neither record has an Official Location; they match only through unverified AI-suggested locations.", **common)
            return LocationCheck(match="CONFLICT", explanation=f"The AI-suggested locations differ (“{ea}” vs “{la}”) and neither has an Official Location.", **common)
        return LocationCheck(match="INSUFFICIENT", explanation="At least one record has no Official or AI-suggested location.", **common)

    # -- pair --------------------------------------------------------------
    def validate(self, earlier_record_id: str, later_record_id: str) -> PairValidation:
        v = PairValidation(valid=False, requires_acknowledgement=False, requires_human_review=False)
        if earlier_record_id == later_record_id:
            v.errors.append(Issue(code="same_record", severity="error", message="The same inspection record was selected twice."))
            return self._finish(v)
        e = self.store.by_id.get(earlier_record_id)
        l = self.store.by_id.get(later_record_id)
        for side, rec, rid in (("earlier", e, earlier_record_id), ("later", l, later_record_id)):
            if rec is None:
                v.errors.append(Issue(code="unknown_record", severity="error", message=f"The {side} inspection record was not found."))
        if e is None or l is None:
            return self._finish(v)

        v.vessel_match = e.vessel_id == l.vessel_id
        if not v.vessel_match:
            v.errors.append(Issue(code="different_vessels", severity="error", message="Both photographs must belong to the same vessel."))
        if not e.inspection_date or not l.inspection_date:
            v.errors.append(Issue(code="undated_record", severity="error", message="Both inspection records must have an inspection date."))
        else:
            v.date_order_valid = e.inspection_date < l.inspection_date
            if e.inspection_date == l.inspection_date:
                v.errors.append(Issue(code="same_inspection_date", severity="error",
                                      message="Both photographs are from the same inspection date; choose an earlier and a later inspection."))
            elif not v.date_order_valid:
                v.errors.append(Issue(code="invalid_date_order", severity="error",
                                      message="The earlier photograph must come from an earlier inspection date."))
        for rec, side in ((e, "earlier"), (l, "later")):
            if rec.date_source == "worksheet_name":
                v.info.append(Issue(code="derived_date", severity="info",
                                    message=f"The {side} inspection date was derived from the worksheet name (original report date: {rec.report_date})."))

        v.location = self._location(e, l)
        lm = v.location.match
        if lm == "AI_ONLY":
            v.warnings.append(Issue(code="ai_only_location", severity="warning",
                                    message="Location match relies only on unverified AI-suggested locations. Check the photographs show the same area."))
        elif lm == "MIXED":
            v.warnings.append(Issue(code="mixed_location_evidence", severity="warning", message=v.location.explanation))
        elif lm == "CONFLICT":
            v.warnings.append(Issue(code="location_conflict", severity="warning",
                                    message=v.location.explanation + " The photographs may show different areas."))
        elif lm == "INSUFFICIENT":
            v.warnings.append(Issue(code="location_insufficient", severity="warning",
                                    message="There is not enough location information to tell whether both photographs show the same area."))

        v.earlier, ea, _ = self._check_image(e, "earlier", v)
        v.later, la, _ = self._check_image(l, "later", v)

        e_sha = v.earlier.sha256_actual or v.earlier.sha256_recorded
        l_sha = v.later.sha256_actual or v.later.sha256_recorded
        if (e.image_id and e.image_id == l.image_id) or (e_sha and e_sha == l_sha):
            v.sha256_duplicate = True
            v.perceptual.category = "IDENTICAL_FILE"
            v.errors.append(Issue(code="same_image", severity="error",
                                  message="Both selections are the same photograph file (byte-for-byte identical, same SHA-256). "
                                          "The photograph was reused in another inspection; there is nothing to compare."))
        elif ea and la:
            pd, dd = hamming(ea.phash, la.phash), hamming(ea.dhash, la.dhash)
            v.perceptual = PerceptualCheck(phash_distance=pd, dhash_distance=dd,
                                           similarity=round(1 - (pd + dd) / 128, 3), category="NOT_DUPLICATE")
            if pd <= self.t.phash_identical and dd <= self.t.dhash_identical:
                v.perceptual.category = "VISUALLY_IDENTICAL"
                v.warnings.append(Issue(code="visually_identical", severity="warning",
                                        message="The two files differ, but their image content is visually identical (likely a re-saved "
                                                "copy of the same photograph). A comparison would not show a real change over time."))
            elif pd <= self.t.phash_near and dd <= self.t.dhash_near:
                v.perceptual.category = "NEAR_DUPLICATE"
                v.warnings.append(Issue(code="near_duplicate", severity="warning",
                                        message="The photographs are nearly identical in content (possibly the same photograph edited, "
                                                "cropped or re-compressed). Check they are genuinely separate inspection photographs."))
            if (ea.width, ea.height) != (la.width, la.height):
                v.info.append(Issue(code="different_dimensions", severity="info",
                                    message=f"The photographs have different dimensions ({ea.width}x{ea.height} vs {la.width}x{la.height})."))
        return self._finish(v)

    @staticmethod
    def _finish(v: PairValidation) -> PairValidation:
        v.valid = not v.errors
        v.requires_acknowledgement = v.valid and bool(v.warnings)
        v.requires_human_review = bool(v.warnings)
        return v
