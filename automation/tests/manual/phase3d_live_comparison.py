"""Phase 3d test plan -- NOT run automatically (not collected by pytest; lives under tests/manual/).

Runs the same 7 photographs used in Phase 3 / 3b through the Phase 3c-optimized
GeminiVisionClient in two configurations (default thinking, thinking_level="LOW"), capturing
token usage and latency via classify_with_usage(), and prints a comparison against the Phase 3
baseline numbers and the manually verified ground truth. Requires a live, non-exhausted Gemini
API key -- do not run until that is confirmed. 7 photos x 2 configurations = 14 API calls.

Usage (only after approval + confirmed available quota):
    python tests/manual/phase3d_live_comparison.py --confirm-live-api-calls

Without that flag the script exits immediately (code 2) -- before it reads settings or
loads .env, and before any client exists. It bypasses the pipeline's pacing, quota stop
and circuit breaker, so it must never run by accident.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src import config, vision_client

# (week, filename, ground_truth_official, ground_truth_ai_location, ground_truth_activity, ground_truth_confidence)
SAMPLES = [
    ("Week 30", "2026-08-02_001.jpg", "Main Deck", "Medium", "No activity observed"),
    ("Week 30", "2026-08-02_004.jpg", "Hatch Coaming", "Medium", "No activity observed"),
    ("Week 30", "2026-08-02_082.jpg", "Cross deck Fwd of # 1", "High", "No activity observed"),
    ("week 37", "2026-09-20_001.jpg", "Main Deck", "Medium", "Needs Review (human unsure if person)"),
    ("week 38", "2026-09-27_020.jpg", "Hatch Coaming", "High", "Needs Review (structural crack)"),
    ("week 41", "2026-10-18_020.jpg", "Hatch Coaming", "High", "No activity observed"),
    ("week 42", "2026-10-25_001.jpg", "Cross deck Fwd of # 1", "High", "No activity observed"),
]

ROOT = Path(r"E:\Synergy Vessel")

# Phase 3 baseline (old prompt, no schema, full-size image, default thinking) for reference:
#   avg latency 15.72s/image; one token sample: text=216 image=1064 output=29 thoughts=160 total=1469
PHASE3_BASELINE_AVG_LATENCY = 15.72
PHASE3_BASELINE_TOKEN_SAMPLE = {"text": 216, "image": 1064, "output": 29, "thoughts": 160, "total": 1469}


def run_config(label: str, client: vision_client.VisionClient):
    print(f"\n=== {label} ===")
    results = []
    for wk, fname, *_ in SAMPLES:
        img_bytes = (ROOT / wk / "Extracted Images" / fname).read_bytes()
        result, usage = client.classify_with_usage(img_bytes)
        results.append((wk, fname, result, usage))
        print(f"{wk}/{fname}: {result}")
        print(f"  latency={usage.latency_seconds:.2f}s tokens(text/image/output/thoughts/total)="
              f"{usage.input_text_tokens}/{usage.input_image_tokens}/{usage.output_tokens}/"
              f"{usage.thoughts_tokens}/{usage.total_tokens}")
    avg_latency = sum(r[3].latency_seconds for r in results) / len(results)
    print(f"\n{label} avg latency: {avg_latency:.2f}s (Phase 3 baseline: {PHASE3_BASELINE_AVG_LATENCY}s)")
    return results


LIVE_OPT_IN_FLAG = "--confirm-live-api-calls"


def require_live_opt_in(argv: list[str]) -> None:
    """L2: refuse to run unless the operator passed LIVE_OPT_IN_FLAG explicitly."""
    if LIVE_OPT_IN_FLAG not in argv:
        print(f"Refusing to run: this script makes 14 LIVE Gemini API calls outside the "
              f"pipeline's safeguards. Rerun with {LIVE_OPT_IN_FLAG} only after explicit "
              f"approval. Nothing was loaded and no request was made.", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    require_live_opt_in(sys.argv[1:])  # L2: must stay first -- before settings / .env
    settings = config.load_settings()
    config.load_provider_environment()  # manual LIVE script: explicit .env load (no longer implicit)

    # Configuration A: optimized prompt+schema+resize, default thinking (isolates the
    # prompt-shortening + image-resize effect against the Phase 3 baseline, which also used
    # default thinking).
    client_default = vision_client.GeminiVisionClient(model=settings["model"])
    run_config("Optimized (default thinking)", client_default)

    # Configuration B: same, plus thinking_level=LOW (validates the still-unverified
    # Phase 3b/3c thinking-budget optimization).
    client_low = vision_client.GeminiVisionClient(model=settings["model"], thinking_level="low")
    run_config("Optimized (thinking_level=LOW)", client_low)
