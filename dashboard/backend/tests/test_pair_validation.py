"""Phase 5A -- image-pair validation before AI analysis. Offline only (mock provider, synthetic photos)."""

from __future__ import annotations

import hashlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai import pair_validation as pv
from app.ai.providers import MockConditionProvider
from app.config import Settings
from app.main import create_app
from conftest import sign_in, _img, _rec, _register, _report, jpeg_bytes

IMG = "Extracted Images - New"
X = "Nord Kudu  Deck Photos -02 AUG 2026 Part 1 (1).xlsx"


def _reencode(data: bytes, fmt="JPEG", scale=1.0, crop=0.0, quality=70) -> bytes:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = im.size
    if crop:
        im = im.crop((int(w * crop), int(h * crop), w, h)).resize((w, h))
    if scale != 1.0:
        im = im.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    im.save(buf, fmt, **({"quality": quality} if fmt == "JPEG" else {}))
    return buf.getvalue()


def _dark() -> bytes:
    im = Image.new("RGB", (320, 240), (8, 8, 8))
    for i in range(0, 320, 40):
        im.paste((14, 14, 14), (i, 0, i + 20, 240))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("pairs")
    base, bridge = jpeg_bytes(10), jpeg_bytes(15)
    weeks = {
        ("Week 30", 30, "8/2/2026", "AUG 02 2026"): [
            ("W30-0001", "CARGO HOLD NO. 1", None, base),
            ("W30-0002", "CARGO HOLD NO. 2", None, jpeg_bytes(11)),
            ("W30-0003", "Not Labelled", "Main Deck", jpeg_bytes(12)),
            ("W30-0004", "Not Labelled", "Hatch Coaming", jpeg_bytes(13)),
            ("W30-0005", "Not Labelled", None, jpeg_bytes(14)),
            ("W30-0006", "BRIDGE DECK", None, bridge),
            ("W30-0007", "MONKEY ISLAND", None, jpeg_bytes(16)),
            ("W30-0008", "FUNNEL DECK AREA", None, _dark()),
        ],
        ("week 31", 31, "8/9/2026", "AUG 09 2026"): [
            ("W31-0001", "CARGO HOLD NO. 1", None, jpeg_bytes(20)),             # new shot / different angle
            ("W31-0002", "CARGO HOLD NO. 1", None, base),                       # byte-for-byte the same file
            ("W31-0003", "Not Labelled", "Main Deck", jpeg_bytes(21)),
            ("W31-0004", "Not Labelled", "Main Deck", jpeg_bytes(22)),
            ("W31-0005", "CARGO HOLD NO. 1", None, _reencode(base, "PNG")),     # different format, same picture
            ("W31-0006", "CARGO HOLD NO. 1", None, _reencode(base, scale=2.0)),  # different resolution, same picture
            ("W31-0007", "BRIDGE DECK", None, _reencode(bridge, crop=0.03)),     # lightly cropped copy
            ("W31-0008", "CARGO HOLD NO. 1", None, b"\xff\xd8\xff\xe0 damaged"),  # corrupted
            ("W31-0009", "CARGO HOLD NO. 1", None, None),                       # file missing
            ("W31-0010", "CARGO HOLD NO. 1", None, jpeg_bytes(23)),             # replaced after loading
            ("W31-0011", "Main Deck", None, jpeg_bytes(24)),
            ("W31-0012", "FUNNEL DECK AREA", None, jpeg_bytes(25)),
            ("W31-0013", "BRIDGE DECK", None, jpeg_bytes(26)),                  # unrelated content, same area
        ],
    }
    for (folder, num, date, ws), rows in weeks.items():
        d = root / folder
        recs, reg = [], []
        for i, (image_id, official, ai, payload) in enumerate(rows, 1):
            sha = _img(d / IMG, f"{image_id}.jpg", payload) if payload is not None else "e" * 64
            recs.append(_rec(i, f"{image_id}.jpg", date, X, ws, official, ai, sha))
            reg.append((i, f"{image_id}.jpg", date, X, ws, None if official == "Not Labelled" else official))
        _report(d, folder, recs)
        _register(d, num, reg)
    return root


@pytest.fixture
def make(root, tmp_path):
    clients = []

    def _make(provider=None):
        provider = provider or MockConditionProvider("stable")
        c = TestClient(create_app(Settings(data_root=root, analysis_dir=tmp_path / "analyses"), condition_provider=provider))
        c.__enter__()
        clients.append(c)
        sign_in(c, "reviewer")
        return c, provider

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def rid(c, image_id):
    recs = [r for r in c.app.state.store.records if r.source == "json_report"]
    found = next((r for r in recs if r.image_id == image_id), None)
    if found is None:  # an unresolved photo has no image id: locate it by week and serial number
        week, serial = int(image_id[1:3]), int(image_id[4:])
        found = next(r for r in recs if r.week_number == week and r.image_status != "resolved"
                     and getattr(r, "serial_number", None) == serial)
    return found.record_id


def validate(c, e, l):
    r = c.get("/api/ai-analysis/validate", params={"earlier_record_id": rid(c, e), "later_record_id": rid(c, l)})
    assert r.status_code == 200, r.text
    return r.json()


def codes(v, level):
    return {i["code"] for i in v[level]}


def compare(c, e, l, **extra):
    return c.post("/api/ai-analysis/compare", json={"earlier_record_id": rid(c, e), "later_record_id": rid(c, l), **extra})


# --------------------------------------------------------------------------- hashing unit tests
def test_hashes_are_content_based_and_cached():
    a = jpeg_bytes(77)
    first = pv.analyse_image(a, hashlib.sha256(a).hexdigest())
    size = len(pv.CACHE)
    again = pv.analyse_image(a, hashlib.sha256(a).hexdigest())
    assert again is first and len(pv.CACHE) == size  # cache hit keyed by SHA-256, nothing recomputed
    for variant in (_reencode(a, "PNG"), _reencode(a, scale=0.5), _reencode(a, quality=40)):
        b = pv.analyse_image(variant, hashlib.sha256(variant).hexdigest())
        assert pv.hamming(first.phash, b.phash) <= 3 and pv.hamming(first.dhash, b.dhash) <= 3
    other = jpeg_bytes(78)
    o = pv.analyse_image(other, hashlib.sha256(other).hexdigest())
    assert pv.hamming(first.phash, o.phash) > 8


def test_undecodable_bytes_raise():
    with pytest.raises(ValueError):
        pv.analyse_image(b"not an image", "x" * 64)


# --------------------------------------------------------------------------- 1-6 duplicates & similarity
def test_exact_sha256_duplicate_is_blocked(make):
    c, provider = make()
    v = validate(c, "W30-0001", "W31-0002")
    assert v["valid"] is False and v["sha256_duplicate"] is True
    assert v["perceptual"]["category"] == "IDENTICAL_FILE"
    assert "byte-for-byte" in v["errors"][0]["message"]
    r = compare(c, "W30-0001", "W31-0002", acknowledge_warnings=True)
    assert r.status_code == 400 and r.json()["code"] == "same_image"
    assert provider.calls == []


@pytest.mark.parametrize("later,label", [("W31-0005", "different format (PNG)"), ("W31-0006", "different resolution")])
def test_different_file_same_picture_is_visually_identical(make, later, label):
    c, _ = make()
    v = validate(c, "W30-0001", later)
    assert v["valid"] is True and v["sha256_duplicate"] is False, label
    assert v["perceptual"]["category"] == "VISUALLY_IDENTICAL"
    assert "visually_identical" in codes(v, "warnings")
    assert v["requires_acknowledgement"] and v["requires_human_review"]


def test_formats_and_dimensions_are_reported(make):
    c, _ = make()
    v = validate(c, "W30-0001", "W31-0005")
    assert v["earlier"]["format"] == "JPEG" and v["later"]["format"] == "PNG"
    v = validate(c, "W30-0001", "W31-0006")
    assert (v["later"]["width"], v["later"]["height"]) == (640, 480)
    assert "different_dimensions" in codes(v, "info")


def test_lightly_cropped_copy_is_a_near_duplicate(make):
    c, _ = make()
    v = validate(c, "W30-0006", "W31-0007")
    assert v["valid"] is True
    assert v["perceptual"]["category"] == "NEAR_DUPLICATE"
    assert "near_duplicate" in codes(v, "warnings")


def test_different_camera_angle_is_not_a_duplicate(make):
    c, _ = make()
    v = validate(c, "W30-0001", "W31-0001")
    assert v["perceptual"]["category"] == "NOT_DUPLICATE" and v["perceptual"]["phash_distance"] > 8
    assert v["valid"] is True and v["warnings"] == [] and v["requires_human_review"] is False
    assert v["location"]["match"] == "OFFICIAL"
    assert "cannot prove" in v["notice"]  # hashing never claims the same physical area


# --------------------------------------------------------------------------- 7-9 files & records
def test_missing_file(make):
    c, provider = make()
    v = validate(c, "W30-0001", "W31-0009")
    assert v["valid"] is False and "photograph_unavailable" in codes(v, "errors")
    r = compare(c, "W30-0001", "W31-0009", acknowledge_warnings=True)
    assert r.status_code == 422 and r.json()["code"] == "photograph_unavailable" and provider.calls == []


def test_corrupted_image(make):
    c, provider = make()
    v = validate(c, "W30-0001", "W31-0008")
    assert v["valid"] is False and "photograph_unreadable" in codes(v, "errors")
    assert v["later"]["readable"] is False and v["later"]["quality"] == "UNREADABLE"
    r = compare(c, "W30-0001", "W31-0008", acknowledge_warnings=True)
    assert r.status_code == 422 and r.json()["code"] == "photograph_unreadable" and provider.calls == []


def test_file_changed_since_recorded_fails_integrity(make, root):
    c, provider = make()
    path = root / "week 31" / IMG / "W31-0010.jpg"
    original = path.read_bytes()
    try:
        path.write_bytes(jpeg_bytes(99))  # synthetic tmp data only
        v = validate(c, "W30-0001", "W31-0010")
        assert v["valid"] is False and "file_integrity" in codes(v, "errors")
        assert v["later"]["sha256_matches_record"] is False
        assert compare(c, "W30-0001", "W31-0010", acknowledge_warnings=True).json()["code"] == "file_integrity"
        assert provider.calls == []
    finally:
        path.write_bytes(original)


def test_invalid_and_unknown_records(make):
    c, provider = make()
    r = c.get("/api/ai-analysis/validate", params={"earlier_record_id": "no-such-record", "later_record_id": rid(c, "W31-0001")})
    assert r.status_code == 200 and "unknown_record" in codes(r.json(), "errors")
    r = c.get("/api/ai-analysis/validate", params={"earlier_record_id": "../etc/passwd", "later_record_id": "x"})
    assert r.status_code == 400
    r = c.post("/api/ai-analysis/compare", json={"earlier_record_id": "no-such-record", "later_record_id": rid(c, "W31-0001")})
    assert r.status_code == 404 and provider.calls == []


# --------------------------------------------------------------------------- 10-12 locations
def test_matching_official_locations(make):
    c, _ = make()
    v = validate(c, "W30-0006", "W31-0013")
    assert v["location"]["match"] == "OFFICIAL" and v["warnings"] == []


def test_ai_only_location_match_is_flagged(make):
    c, _ = make()
    v = validate(c, "W30-0003", "W31-0003")
    assert v["location"]["match"] == "AI_ONLY" and "ai_only_location" in codes(v, "warnings")
    assert v["requires_human_review"] is True


def test_mixed_location_evidence_is_flagged(make):
    c, _ = make()
    v = validate(c, "W30-0003", "W31-0011")
    assert v["location"]["match"] == "MIXED" and "mixed_location_evidence" in codes(v, "warnings")


@pytest.mark.parametrize("e,l", [("W30-0007", "W31-0001"), ("W30-0004", "W31-0004"), ("W30-0004", "W31-0011")])
def test_conflicting_locations_require_review(make, e, l):
    c, _ = make()
    v = validate(c, e, l)
    assert v["location"]["match"] == "CONFLICT" and "location_conflict" in codes(v, "warnings")
    assert v["valid"] is True and v["requires_acknowledgement"] is True


def test_missing_location_is_not_invented(make):
    c, _ = make()
    v = validate(c, "W30-0005", "W31-0003")
    assert v["location"]["match"] == "INSUFFICIENT"
    assert v["location"]["earlier_official"] is None and v["location"]["earlier_ai"] is None


# --------------------------------------------------------------------------- 13-15 dates & same selection
def test_incorrect_date_order(make):
    c, provider = make()
    v = validate(c, "W31-0001", "W30-0001")
    assert v["date_order_valid"] is False and "invalid_date_order" in codes(v, "errors")
    assert compare(c, "W31-0001", "W30-0001").json()["code"] == "invalid_date_order" and provider.calls == []


def test_same_inspection_date(make):
    c, provider = make()
    v = validate(c, "W30-0001", "W30-0002")
    assert "same_inspection_date" in codes(v, "errors")
    assert compare(c, "W30-0001", "W30-0002").json()["code"] == "same_inspection_date" and provider.calls == []


def test_same_image_selected_twice(make):
    c, provider = make()
    v = validate(c, "W30-0001", "W30-0001")
    assert v["valid"] is False and codes(v, "errors") == {"same_record"}
    assert compare(c, "W30-0001", "W30-0001").status_code == 422 and provider.calls == []


def test_poor_image_quality_warns(make):
    c, _ = make()
    v = validate(c, "W30-0008", "W31-0012")
    assert v["earlier"]["quality"] == "POOR" and "image_quality" in codes(v, "warnings")
    assert v["later"]["quality"] in ("GOOD", "ACCEPTABLE")


# --------------------------------------------------------------------------- 16-18 workflow
def test_valid_pair_is_analysed_once_and_records_validation(make):
    c, provider = make()
    first = compare(c, "W30-0001", "W31-0001")
    assert first.status_code == 200, first.text
    a = first.json()["analysis"]
    assert a["pair_validation"]["valid"] is True and a["pair_validation"]["location_match"] == "OFFICIAL"
    assert a["pair_validation"]["perceptual"]["category"] == "NOT_DUPLICATE"
    second = compare(c, "W30-0001", "W31-0001").json()
    assert second["reused"] is True and second["analysis"]["analysis_id"] == a["analysis_id"]
    assert len(provider.calls) == 1  # existing duplicate protection unchanged


def test_warnings_need_acknowledgement_before_any_request(make, tmp_path):
    c, provider = make()
    r = compare(c, "W30-0006", "W31-0007")
    assert r.status_code == 409 and r.json()["code"] == "validation_warnings"
    assert provider.calls == [] and not list((tmp_path / "analyses").glob("CA-*.json"))
    r = compare(c, "W30-0006", "W31-0007", acknowledge_warnings=True)
    assert r.status_code == 200
    a = r.json()["analysis"]
    # a near-duplicate pair can never yield a trusted STABLE: downgraded, never upgraded
    assert a["model_classification"] == "STABLE" and a["classification"] == "NEEDS_REVIEW"
    assert a["human_review_required"] is True
    assert any("near_duplicate" in adj for adj in a["policy_adjustments"])
    assert any(reason.startswith("Pair validation:") for reason in a["review_reasons"])
    assert a["simulated"] is True


def test_ai_only_pair_keeps_classification_but_requires_review(make):
    c, _ = make()
    a = compare(c, "W30-0003", "W31-0003", acknowledge_warnings=True).json()["analysis"]
    assert a["classification"] == "STABLE" and a["human_review_required"] is True
    assert a["pair_validation"]["location_match"] == "AI_ONLY"


def test_validation_endpoint_never_calls_provider_or_saves(make, tmp_path):
    c, provider = make()
    for e, l in [("W30-0001", "W31-0001"), ("W30-0001", "W31-0002"), ("W30-0001", "W31-0008"), ("W30-0006", "W31-0007")]:
        validate(c, e, l)
    assert provider.calls == []
    assert not (tmp_path / "analyses").exists() or not list((tmp_path / "analyses").glob("CA-*.json"))


@pytest.mark.parametrize("e,l", [("W30-0001", "W31-0002"), ("W30-0001", "W31-0008"), ("W30-0001", "W31-0009"),
                                 ("W31-0001", "W30-0001"), ("W30-0001", "W30-0002")])
def test_failed_validation_never_reaches_the_provider(make, e, l):
    c, provider = make()
    r = compare(c, e, l, acknowledge_warnings=True)
    assert r.status_code >= 400 and provider.calls == []
