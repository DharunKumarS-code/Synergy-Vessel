"""Groq vision pilot -- ONE image pair, ONE request. Isolated experiment; not part of the dashboard.

Run from dashboard/backend:
    .venv\\Scripts\\python -m experiments.groq_evaluation.run_pilot            # dry run: builds + checks the request, no network
    .venv\\Scripts\\python -m experiments.groq_evaluation.run_pilot --execute  # one live request (needs GROQ_API_KEY)

Safety:
* The API key is read only from the GROQ_API_KEY environment variable. It is never printed, logged or saved,
  and is scrubbed from any error text. .env files are never read.
* Production code is imported read-only (prompt builder, schema, policy, inspection store); nothing in
  production is modified and nothing is written to the analysis store.
* Photographs are read, hashed before/after, and downscaled in memory only (max 1024 px, like the Gemini path).
* Results go to experiments/groq_evaluation/results/ only.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from PIL import Image

from app.ai.prompts import EARLIER_LABEL, LATER_LABEL, PROMPT_VERSION, build_prompt
from app.ai.schema import ConditionAssessment, InvalidAIResponse, apply_policy, validate_assessment
from app.config import Settings
from app.services.inspection_store import InspectionStore

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "qwen/qwen3.8-27b"          # Groq vision model (Preview) -- see README for the selection evidence
PILOT_PROMPT_VERSION = f"groq-pilot-v1 (production {PROMPT_VERSION} + JSON schema block)"
EARLIER_ID, LATER_ID = "W30-0079", "W42-0095"   # FORECASTLE DECK AREA, Week 30 vs Week 42
MAX_DIM = 1024
MAX_REQUEST_BYTES = 20 * 1024 * 1024  # Groq: 20 MB per request containing images
TIMEOUT_S = 180
RESULTS = Path(__file__).resolve().parent / "results"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare(path: Path) -> tuple[str, dict]:
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        original = im.size
        im.thumbnail((MAX_DIM, MAX_DIM))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
    data = buf.getvalue()
    return (f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}",
            {"original_size": list(original), "sent_size": list(im.size), "sent_bytes": len(data), "source_sha256": sha256(raw)})


def schema_block() -> str:
    schema = ConditionAssessment.model_json_schema()
    return ("\n\nOUTPUT FORMAT: respond with ONE JSON object only (no markdown, no prose outside JSON) that conforms "
            "exactly to this JSON Schema. Use only the enum values given. Use empty lists where nothing applies.\n"
            + json.dumps(schema, separators=(",", ":")))


def load_pair():
    s = Settings()  # read-only inspection data; analysis/import/auth stores are not touched
    store = InspectionStore.build(s)
    recs = {r.record_id: r for r in store.records}
    out = []
    for rid in (EARLIER_ID, LATER_ID):
        r = recs[rid]
        path, _media = store.images.resolve(r.image_id)
        out.append({"record_id": rid, "image_id": r.image_id, "path": str(path), "inspection_date": str(r.inspection_date),
                    "official_location": r.official_location, "week": r.week_number})
    return out


def build_request(pair) -> tuple[dict, dict]:
    e, l = pair
    d0, d1 = dt.date.fromisoformat(e["inspection_date"]), dt.date.fromisoformat(l["inspection_date"])
    prompt = build_prompt(location=e["official_location"], earlier_date=e["inspection_date"], later_date=l["inspection_date"],
                          interval_days=(d1 - d0).days) + schema_block()
    e_url, e_meta = prepare(Path(e["path"]))
    l_url, l_meta = prepare(Path(l["path"]))
    body = {
        "model": MODEL,
        "temperature": 0.1,
        "max_completion_tokens": 4096,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "text", "text": EARLIER_LABEL.format(date=e["inspection_date"])},
            {"type": "image_url", "image_url": {"url": e_url}},
            {"type": "text", "text": LATER_LABEL.format(date=l["inspection_date"])},
            {"type": "image_url", "image_url": {"url": l_url}},
        ]}],
    }
    return body, {"prompt": prompt, "earlier_image": e_meta, "later_image": l_meta,
                  "request_bytes": len(json.dumps(body).encode())}


def clean_key(raw: str | None) -> tuple[str | None, list[str]]:
    """Normalise the env value (outer whitespace, BOM, surrounding quotes) and describe problems without revealing it."""
    if raw is None or not raw.strip():
        return None, ["REFUSED: GROQ_API_KEY is not set (or empty) in this process"]
    notes = []
    key = raw.replace("﻿", "").strip()
    if key != raw:
        notes.append("removed surrounding whitespace/BOM")
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1].strip()
        notes.append("removed surrounding quotes")
    if not key.startswith("gsk_"):
        notes.append("REFUSED: value does not start with 'gsk_' (not a Groq API key, or a placeholder)")
    if any(c.isspace() for c in key) or any(ord(c) < 33 or ord(c) > 126 for c in key):
        notes.append("REFUSED: value contains spaces or non-printable/non-ASCII characters")
    if not 40 <= len(key) <= 80:
        notes.append(f"REFUSED: unexpected length {len(key)} (Groq keys are normally 56 characters)")
    return key, notes


def scrub(text: str, key: str | None) -> str:
    text = text or ""
    if key:
        text = text.replace(key, "[redacted]")
    return re.sub(r"gsk_[A-Za-z0-9]{10,}", "[redacted]", text)[:4000]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="send the single live request")
    ap.add_argument("--check-key", action="store_true", help="check GROQ_API_KEY's format locally (no network)")
    args = ap.parse_args(argv)

    pair = load_pair()
    before = {p["record_id"]: sha256(Path(p["path"]).read_bytes()) for p in pair}
    body, meta = build_request(pair)
    print(f"Pair: {pair[0]['record_id']} ({pair[0]['inspection_date']}) -> {pair[1]['record_id']} ({pair[1]['inspection_date']}), "
          f"location '{pair[0]['official_location']}'")
    print(f"Images sent as {meta['earlier_image']['sent_size']} / {meta['later_image']['sent_size']} JPEG; "
          f"request {meta['request_bytes'] / 1e6:.2f} MB (limit 20 MB); model {MODEL}")
    if meta["request_bytes"] > MAX_REQUEST_BYTES:
        print("Request too large; aborting.")
        return 2
    raw_key = os.environ.get("GROQ_API_KEY")
    key, problems = clean_key(raw_key)
    if args.check_key or args.execute:
        # Shape only -- never the key or any part of it.
        print(f"GROQ_API_KEY: {'present' if raw_key is not None else 'absent'}; "
              + ("; ".join(problems) if problems else "format looks valid (gsk_ prefix, no spaces or quotes)"))
    if args.check_key:
        return 0 if key and not [p for p in problems if p.startswith("REFUSED")] else 3
    if not args.execute:
        print("Dry run only (no network). Use --execute for the single live request.")
        return 0
    if not key or any(p.startswith("REFUSED") for p in problems):
        print("No request was sent. Fix GROQ_API_KEY (see above) and retry.")
        return 3

    eval_id = f"GRQ-{dt.datetime.now():%Y%m%d-%H%M%S}"
    record = {"evaluation_id": eval_id, "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "provider": "groq", "model": MODEL, "endpoint": ENDPOINT, "prompt_version": PILOT_PROMPT_VERSION,
              "image_pair": pair, "request": {k: v for k, v in meta.items() if k != "prompt"}, "prompt": meta["prompt"],
              "generation": {k: body[k] for k in ("temperature", "max_completion_tokens", "response_format")},
              "api_status": None, "duration_s": None, "usage": None, "errors": [], "raw_output": None,
              "reasoning_stripped": False, "parsed_json": None, "schema_valid": None, "schema_error": None,
              "policy_outcome": None, "evaluation": None}
    t0 = time.monotonic()
    try:
        r = httpx.post(ENDPOINT, json=body, timeout=TIMEOUT_S,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        record["duration_s"] = round(time.monotonic() - t0, 2)
        record["api_status"] = r.status_code
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            record["errors"].append(scrub(json.dumps(data.get("error", data)) if data else r.text, key))
        else:
            record["usage"] = data.get("usage")
            choice = (data.get("choices") or [{}])[0]
            record["finish_reason"] = choice.get("finish_reason")
            raw = (choice.get("message") or {}).get("content") or ""
            record["raw_output"] = raw
            text = raw
            if "<think>" in text:
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                record["reasoning_stripped"] = True
            try:
                record["parsed_json"] = json.loads(text)
            except ValueError as exc:
                record["errors"].append(f"output is not valid JSON: {exc}")
            if record["parsed_json"] is not None:
                try:
                    a = validate_assessment(record["parsed_json"])
                    record["schema_valid"] = True
                    record["policy_outcome"] = apply_policy(a).model_dump(mode="json")
                except InvalidAIResponse as exc:
                    record["schema_valid"] = False
                    record["schema_error"] = str(exc)
    except httpx.HTTPError as exc:
        record["duration_s"] = round(time.monotonic() - t0, 2)
        record["errors"].append(scrub(f"{type(exc).__name__}: {exc}", key))
    after = {p["record_id"]: sha256(Path(p["path"]).read_bytes()) for p in pair}
    record["source_images_unchanged"] = before == after
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{eval_id}.json"
    text = json.dumps(record, indent=2, ensure_ascii=False)
    assert key not in text
    out.write_text(text, encoding="utf-8")
    print(f"HTTP {record['api_status']} in {record['duration_s']} s; schema_valid={record['schema_valid']}; "
          f"errors={len(record['errors'])}; saved {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")
    return 0 if record["schema_valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
