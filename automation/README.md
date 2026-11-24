# Synergy Vessel Automated Image Analysis Workflow

Standalone Python pipeline that extracts embedded ship-inspection photographs from the weekly
source Excel files, classifies each one's activity/location with a vision AI, and writes a
per-week Excel report -- reproducing (and automating) the process originally done by hand in
this project. Does not depend on Claude Code / Fable at runtime.

**Output location:** `automation/output/<week>/` -- kept fully separate from the existing,
hand-built `E:\Synergy Vessel\Week NN\...` folders. This pipeline never writes into or modifies
those folders, the original source workbooks, or anything already in the project.

## Setup

```
cd "E:\Synergy Vessel\automation"
python -m venv .venv                   # SV-07: the automation has its own environment (already set up on this PC)
.venv\Scripts\python -m pip install "pip==26.3" "setuptools==84.2.1"
.venv\Scripts\python -m pip install --require-hashes -r requirements.txt   # or requirements-dev.txt to also get pytest
.venv\Scripts\activate
copy config\.env.example .env
# edit .env and set GEMINI_API_KEY=... (and/or GROQ_API_KEY=... -- see "Choosing a provider" below)
```

No API key is required to try the pipeline -- see Dry run below.

**Operators:** day-to-day procedures (weekly workflow, quota planning, output meanings, failure
recovery, troubleshooting) are in [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md).

## Choosing a provider: Gemini or Groq

`config/settings.yaml`'s `provider` and `model` fields select the vision backend. Both providers
implement the same `VisionClient` interface (`src/vision_client.py`), so nothing in
`classifier.py`, `report_builder.py`, or `run_pipeline.py` changes based on which one is active.

```yaml
# Gemini (default; live-tested on 7 photos with an earlier prompt -- see "Live-testing status" below):
provider: gemini
model: gemini-3.8-flash          # needs GEMINI_API_KEY in .env

# Groq (experimental; 7 live single-photo calls, 3 returned invalid JSON -- see below):
provider: groq
model: qwen/qwen3.8-27b          # needs GROQ_API_KEY in .env
```

**Live-testing status (as of 2026-11-13):**
- **Gemini:** a 7-photo live comparison (Phase 3/3b, 2026-11-11) returned classifications with
  an earlier, longer prompt. The current prompt, structured-output schema and 768px resize have
  **not** been tested live. Every approved live pipeline run since then has failed with 503
  "high demand" or a quota 429, so `.state/` holds **no successful classification** yet.
- **Groq:** 7 live single-photo calls on 2026-11-12, all before the P2-8 and P2-9 changes:
  4 valid classifications and 3 invalid-JSON failures (see "Groq provider" below). No live call
  has exercised the P2-8 error handling or the P2-9 pacing.
- Everything else marked "offline-tested" below is proven only by mocked tests.

**Why Groq is being evaluated:** this project's Gemini free tier was **observed** at 20
requests/day for `gemini-3.8-flash` (Google's 429 of 2026-11-12; not a published figure, see
"Gemini daily quota" below), which is impractical for a 1,058-photo dataset without enabling billing. Groq's
published free tier for `qwen/qwen3.8-27b` is far more generous on request *count* -- but read
the caveat below before assuming it's a drop-in fix:

- Per Groq's own docs as read on 2026-11-12 (not verified against any real account used by this
  project, and Groq can change them): **30 RPM /
  1,000 RPD / 8,000 TPM / 200,000 TPD** on the free tier.
- **TPM, not RPM or RPD, is the real constraint.** This pipeline's prompt + 1 image is already
  roughly 2,750 input tokens per call, so the 8,000 TPM cap allows only ~2-3 calls/minute
  sustainably -- well under the 30 RPM headline number. (Superseded: Groq now has its own
  required pacing cap, `rate_limit.groq_requests_per_minute` -- see Phase P2-9 below.)
- Unlike Gemini, Groq charges a **flat 2,048 tokens per image regardless of resolution**, so
  `image_prep.py`'s 768px downscaling (wired into `GeminiVisionClient` only) would not reduce
  Groq token cost even if applied there too.
- None of the above has been confirmed with a live call. Treat every number in this section as
  "what the documentation says," not "what this account gets."

## Running

```
python run_pipeline.py --dry-run                       # extract + report, no AI calls; writes only under dry_run/
python run_pipeline.py --weeks "Week 30" --sample 5     # real API, first 5 images of one week only
python run_pipeline.py --weeks "Week 30"                # real API, that whole week
python run_pipeline.py                                   # full run, every discovered week, every photo
python run_pipeline.py --force                           # re-classify every image, ignoring saved state
python run_pipeline.py --quota-plan                      # read-only: photos still needing a Gemini request
python run_pipeline.py --quota-plan --daily-request-limit 20   # + conditional day estimate at YOUR limit
python run_pipeline.py --from-registers --dry-run          # input = completed weekly registers (see runbook)
```

**Argument validation (Phase P2-1 / P2-2).** `--sample N` must be a whole number of 1 or more
(`0`, negatives or a missing value are rejected). Every `--weeks` name must match a discovered
week folder (`Week N` directly under the project root; matching ignores case and extra spaces,
so `"week 31"` and `"Week  31"` both select `week 31`). If any name is unknown -- including a
mix of valid and unknown names -- or `--weeks` is given with no names, the command stops with
exit code 2 and a message listing the unknown names and the available week folders; nothing is
processed or written. Selected weeks always run in discovery (folder-name) order, whatever order
they were typed in. This applies equally with `--dry-run`, `--force`, `--sample`,
`--strict-exit` and `--quota-plan`.

`--quota-plan` (Phase P1-3) makes no API call and writes nothing (no state, report, image or log
file): it extracts each week in memory, reads `.state/`, and prints per-week AI-assessable / done /
remaining counts. A day estimate is printed only if you pass `--daily-request-limit` (take the
number from your project's AI Studio rate-limit page); it cannot be combined with `--dry-run`,
`--force` or `--sample`. See "Gemini daily quota" under Known limitations.

Reruns are idempotent: already-classified images (by content hash, in `.state/<week>.json`) are
skipped unless `--force` is given.

**Dry runs are fully isolated (Phase P0-1).** `--dry-run` never makes an AI request and never
touches the real `.state/`, `output/` or `logs/`: everything it writes goes under
`automation/dry_run/` (`dry_run/.state/`, `dry_run/output/<week>/DRY RUN - <report>.xlsx`,
`dry_run/logs/`). Every AI-assessed row is marked `DRY_RUN` in the report's Classification
Status column and the flagged CSV's `failure_type`, and its state entry is saved `ok: false`, so a
dry-run placeholder is never mistaken for, or cached as, a real classification -- a later live
run still classifies those images. Delete `automation/dry_run/` whenever you like.

## Testing

```
.venv\Scripts\python -m pip install --require-hashes -r requirements-dev.txt
.venv\Scripts\python -m pytest tests/ -v
```

`requirements.in` / `requirements-dev.in` list the top-level dependencies; `requirements.txt` /
`requirements-dev.txt` are generated locks (exact versions + hashes, pip-tools `pip-compile --generate-hashes
--strip-extras`). Edit the `.in` file and regenerate; never edit a lock by hand.

Tests run against tiny synthetic fixture workbooks in `tests/fixtures/` and a stubbed vision
client -- no real API calls, no dependency on the real ship files.

**Recommended rollout order:** `--dry-run` first (validates extraction + report generation end
to end with zero cost) -> `--weeks "Week 30" --sample 5` with a real key (validates the AI call
path cheaply) -> `--weeks "Week 30"` (one full week) -> full run once that's reviewed.

## How the source workbooks are structured (read this before changing extraction.py)

- One worksheet per source file; sheet name is a report-date string, inconsistently spaced
  across weeks (confirmed: `"AUG 02 2026 "`, `"SEPT 06  2026"`, `"OCT 11 2026"`).
- Cell A1 (merged) holds a banner: `" DECK PHOTOS\nM.V. NORD KUDU\nDate: DD MON YYYY"` -- the
  only per-file date; there is no per-photo timestamp anywhere in these files.
- **Official location = the nearest preceding section-heading row.** Headings are plain-text
  merged cells spanning most of the sheet's width (>=10 columns, see
  `settings.yaml: processing.heading_min_merge_width`); photo-block merges are narrower (~7
  columns) by construction, which is what tells the two apart. A heading applies to every image
  row strictly *after* it (confirmed against all 1,058 known-correct labels: using `<=` instead
  of `<` misattributes the one image in this project whose anchor row exactly coincides with
  the next heading's row).
- Each week's photos are split across 2 "Part" source files, numbered inconsistently
  (`(1)/(2)` in most weeks, `(2)/(3)` in weeks 38-42). `discovery.py` sorts by the `(N)` number
  itself, not the raw filename -- a plain alphabetical sort silently misorders at least one real
  week (35) because of a stray double space in its filename.
- **Exact-duplicate images are real, not a parsing bug.** Confirmed in every week of this
  project: the same 2 photos are embedded twice -- once early (before any heading) and again
  later under an unrelated heading. `extraction.extract_week()` dedupes by content hash, keeping
  the first occurrence (matching what the existing hand-built reports already did), and logs
  every drop. This is exactly why every week here extracts 98 raw images but ends up with 96
  (Week 39 is the only week where the duplicates were never introduced, hence 98 stays 98).
- **Report date is only trusted if every source file in the week independently confirms the
  same date.** Week 34 is the real precedent: its banner says "23 AUG 2026" while its sheet name
  says "AUG 30 2026" -- they disagree, so the date is treated as unconfirmed and photos get
  `Timestamp_Pending_NNN.jpg` filenames instead of a guessed date, mirroring the existing
  (manual) convention already present in this project for that week.

All of the above was empirically re-validated against this project's existing, known-correct
`Final Official vs AI Location Report.xlsx` files: 0 mismatches across all 1,058 photos in all
11 weeks.

## Architecture

```
automation/
  config/settings.yaml      provider/model, rate limits, paths, heading-detection threshold
  config/.env.example        template for the real (gitignored) .env
  src/discovery.py           find week folders + order their source "Part" files correctly
  src/extraction.py          heading detection, date resolution, dedup (see above)
  src/vision_client.py       Gemini + Groq clients + NullVisionClient (dry-run), one interface
  src/classifier.py          guardrails: closed confidence vocabulary, "never invent" fallback
  src/image_prep.py          downscales a photo (768px long side) before a Gemini request only
  src/report_builder.py      builds the 11-column report from scratch, embeds photos
  src/register_input.py      --from-registers: validated, re-hashed register rows (read-only)
  src/state.py                content-hash ledger for idempotent reruns
  src/logging_utils.py        run log + flagged-items CSV for human review
  run_pipeline.py             CLI entry point (AI classification pipeline)

  Extraction-only workflow (no AI, no provider code, no .env, network blocked):
  extract_images.py           CLI: one register per week folder + "Extracted Images - New\"
  src/image_extraction.py     reads every embedded image of every worksheet (no dedupe)
  src/weekly_image_report.py  writes/validates Week_NN_Image_Register.xlsx; skips completed weeks
  tests/                      unit tests against synthetic fixtures (no API, no real files)
```

## Known limitations / things to revisit

- `classifier.py`'s activity detection only runs on photos with no official (heading-derived)
  location (i.e. the ones that previously needed AI review). Photos that already have an
  official location get `"Not Assessed (Official Location)"` as Activity Identified (Phase P0-2;
  previously the misleading `"No activity observed"`) and no AI call -- extend
  `run_pipeline.process_week()` if activity classification should run on every photo regardless
  of location status.
- **Gemini daily quota (Phase P1-3) -- what is documented vs. observed vs. assumed:**
  - *Provider-documented* (Google's rate-limit page, https://aistudio.google.com/docs/rate-limits
    -- the page Google's own quota 429 links to; checked 2026-11-12): requests-per-day quotas
    **reset at midnight Pacific time**, and limits apply **per project**, not per API key. That
    page does **not** publish a per-model free-tier daily number for `gemini-3.8-flash`; your
    project's actual limits are shown at https://aistudio.google.com/rate-limit. Re-check both
    before planning -- Google can change them.
  - *Observed in this project* (not a published figure): the 429 of 2026-11-12 11:35:57 IST
    reported `quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue 20`, and
    `retryDelay 6843s` -- which landed exactly on 00:00 Pacific (13:30 IST), consistent with the
    documented reset. So this project's free-tier limit was **20 requests/day** at that time.
  - *Not known / not assumed*: whether failed requests (503s, 429s, timeouts) count against the
    daily quota (Google doesn't say); whether the limit is still 20. The pipeline **does not
    compute a reset time** and does not hard-code any daily limit. It only reports what Google's
    429 said: the suggested `retryDelay` (shown with the clock time it implies, labelled as
    derived from that value) and `quotaValue`/`quotaId` when present.
  - **Daily-quota stop message:** says what was detected, that no further requests were made,
    that the stopping photo is saved `ok: false` and retried automatically, that no later photo
    or week was processed, which `PARTIAL` / `STALE` files were written, how to resume (rerun the
    same command after the reset -- **not** `--force`, which would re-send finished photos), and
    how many AI-assessable photos this run knows are still outstanding. Only when Google reported
    `quotaValue` does it add a **conditional lower-bound** day estimate (ceil(remaining / limit),
    valid only if every photo succeeds on its first request and nothing else uses the project's
    quota -- 503 retries can send up to 5 requests per photo). Never a completion date.
  - **Planning the 66 historical AI-assessable photos:** only photos with no official location
    send requests; labelled photos never do. Run `--quota-plan` for the current remaining count
    (as of 2026-11-13, `.state/` holds no successful classification, so all of them remain). At
    the observed 20/day that is *at least* 4 quota days if every request succeeds first time --
    a conditional estimate, not a schedule. A larger dataset, or the remaining photos plus 503
    retries, may need a paid tier on the AI Studio project.
- `GeminiVisionClient._call` now only retries `google.genai.errors.ServerError` (5xx/transient).
  A 429 `RESOURCE_EXHAUSTED` (quota) or other 4xx client error fails immediately instead of
  retrying 5x with backoff -- confirmed necessary: blind retrying on quota exhaustion burns
  through what little daily budget remains for no benefit.
- **Phase AE3 -- 5xx resilience (Gemini only, offline-tested, not yet exercised live).** After
  repeated live `503 UNAVAILABLE` ("model is currently experiencing high demand") responses:
  - Retries are configured from `settings.yaml`'s `rate_limit` block: `max_retries: 4` (retries
    after the first attempt, so **5 attempts per image**), exponential backoff starting at
    `backoff_seconds: 4`, plus random `jitter_seconds: 4`, capped at `max_backoff_seconds: 60`
    (waits of roughly 4-8s, 8-12s, 16-20s, 32-36s). The settings apply per client instance.
  - An image whose retries all end in a 5xx is recorded as `GeminiServiceUnavailableError`
    (`ClassificationResult.service_unavailable`), checkpointed as `ok: false` and flagged as
    `API_FAILURE`, exactly like any other API failure.
  - **Circuit breaker:** after `circuit_breaker_threshold: 2` *consecutive* such images (counted
    across weeks; any successful call resets the count; cached, skipped, already-labelled, and
    4xx/parse-failure images neither count nor reset it) the whole run stops -- no further
    images or weeks are requested. Since Phase P1-2 the stopped week gets a `PARTIAL` report
    (same as a quota stop). Rerun later with no `--force`: failed and unprocessed images are retried automatically.
  - The Phase V daily-quota stop is unchanged and always takes priority (checked first). Ctrl+C
    still exits with code 130 and keeps every completed checkpoint, including during a backoff wait.
  - (Superseded by P1-1: `rate_limit.requests_per_minute` now paces Gemini requests; Groq has its
    own `groq_requests_per_minute` since P2-9.)
  - Unknown: whether a 503 counts against the free tier's 20-requests/day quota (Google's docs
    don't say); the breaker exists partly to limit that potential cost.
- **Phase AE5 -- request timeout and configuration checks (offline-tested, not yet exercised
  live):**
  - `rate_limit.request_timeout_seconds: 300` (integer, >= 30) is passed to the SDK as
    `HttpOptions(timeout=300000)`. Before this, the SDK had no client-side timeout at all, so a
    stalled connection could hang a run indefinitely. The SDK also sends it to Google as
    `X-Server-Timeout`; keep it generous (successful calls have taken up to 214s) or the server
    starts answering 504. SDK-level retries stay disabled.
  - A timeout becomes `GeminiRequestTimeoutError` after a single attempt -- never retried within
    the same image, since the server may already have processed (and possibly counted) it. The
    image is checkpointed `ok: false`, flagged `API_FAILURE`, and retried on the next run.
  - Timeouts count toward `circuit_breaker_threshold` alongside server errors, but are tracked
    separately (`stopped_unavailable` gains `server_errors` and `timeouts`; stop messages state
    both). Ordinary connection failures (no network) are not timeouts and remain neutral.
  - Retry/timeout settings and the breaker threshold are now validated in every mode, including
    `--dry-run`. Any configuration problem (including a missing API key) is reported as a single
    `Configuration error: ...` log line with exit code 1 -- no traceback, no images processed, no
    requests made. Ctrl+C still exits with 130.
  - P2 remediation: `settings.yaml` itself is checked first. A missing, unreadable, empty or
    invalid-YAML file, a non-mapping top level, or a missing/mistyped required setting (`provider`,
    `model`, the four `paths`, the four `report` keys, `processing.heading_min_merge_width`) prints
    one `Configuration error: ...` line to stderr (every problem listed together) and exits 1 --
    before discovery, logging, any client, or any output, with or without `--strict-exit`. A
    non-mapping `rate_limit` and NaN/infinite backoff/jitter values are rejected as configuration
    errors too. Week folders now run in week-number order (`Week 9` before `Week 10`), and a
    folder named `*.xlsx` is never treated as a workbook.
  - `.env` safety: `automation/.env` (API keys) is loaded **only** by a live run -- no
    `--dry-run`, no `--quota-plan` -- after argument, settings, `--weeks` and rate-limit/provider
    validation pass, immediately before the provider client is built
    (`config.load_provider_environment()`, called once in `run_pipeline.main()`). Loading settings,
    `--quota-plan`, `--dry-run`, every argument/configuration error, and importing any module
    never read it. The offline tests refuse any open/load of the real `.env` (`tests/conftest.py`).
- **Phase AE9 -- operational summary (offline-tested):** after the unchanged week-level lines,
  every run logs `Image outcomes: N classified, N need visual review, N API failure(s), N
  unprocessed (early stop), N already labelled (no AI needed).` A run with any API failure logs
  `Run complete WITH API FAILURES ...` plus a warning that those images were NOT classified
  (and, if nothing succeeded, says so). Weeks skipped by a quota/breaker stop are listed by name
  (their image counts are unknown -- never extracted). Week "succeeded" still means "processed
  without an operational error", not "every image classified". **Exit codes are unchanged:** 0
  for completed runs *and* quota/breaker stops, 1 for configuration errors, 130 for Ctrl+C -- so
  a script/scheduler must read the log warnings, not the exit code, to detect API failures --
  or use `--strict-exit` (below).
- **Phase P1-5 -- optional `--strict-exit` (offline-tested):** with this flag, a run exits with
  code **3** (instead of 0) if it had any API failure, a daily-quota / per-minute / ambiguous-429
  / circuit-breaker / provider-wide-error stop, any failed week, unprocessed photos or weeks not attempted; the last
  log line `--strict-exit: exiting with code 3 because ...` lists why. A fully successful run
  (including `VISUAL_REVIEW` answers, a finished `--sample` run, and a `--dry-run` -- `DRY_RUN`
  placeholders are not failures) exits 0. Codes 1 (configuration error), 2 (invalid arguments)
  and 130 (Ctrl+C) are unchanged and take precedence. `--quota-plan --strict-exit` exits 0 when
  every week was read, 3 if any week could not be read. Without the flag nothing changes.
- **Phase P1-2 -- stale and partial reports (offline-tested):** in `automation/output/<week>/`:
  - `Final Official vs AI Location Report.xlsx` (and `manifest.csv`) exists only when the latest
    full (non-`--sample`) run that processed this week **finished** it. Its second sheet,
    **Processing Status**, says `COMPLETE`.
  - `SAMPLE - <report>.xlsx` (and `SAMPLE - manifest.csv`) is what any `--sample` run writes
    (`PARTIAL - SAMPLE - ...` if it stops early). Its status sheet says `COMPLETE FOR A SAMPLE
    ONLY - first N of M photos`. A sample run never writes, replaces or renames the normal
    complete report; the next finished full run renames leftover SAMPLE files to STALE.
  - `PARTIAL - <report>.xlsx` (and `PARTIAL - manifest.csv`) is written when a run stops early
    (daily quota, rate limit, circuit breaker). Processing Status says `PARTIAL` and gives the
    stop reason. Photos not reached show `UNPROCESSED` / "Not Processed (Run Stopped Early)",
    unless an earlier run already classified them, in which case that saved result is shown.
  - `STALE <timestamp> - <name>` is an older report that an unfinished run made out of date:
    after an early stop, Ctrl+C or any other week failure, the previous complete/partial files
    are renamed (never deleted or overwritten). A finished rerun also renames a leftover
    PARTIAL report to STALE. Weeks a run never reached are left untouched.
  - The Image Register sheet's 11 columns are unchanged; reports are still saved atomically.
  - Manifests (`manifest.csv`, `PARTIAL - `/`SAMPLE - `/dry-run variants) are also written
    atomically (P2-6): written to a `<name>.<random>.tmp` file in the same folder, flushed and
    fsynced, then swapped in with `os.replace`. A failed or interrupted write leaves the previous
    manifest byte-for-byte intact and removes the temp file; CSV columns/encoding are unchanged.
    On Windows a manifest open in another program (e.g. Excel) makes the swap fail -- the week is
    then recorded as failed and the intact previous files are renamed to STALE as usual.
  - **Excel-locked reports (P2-10).** Windows refuses to replace or rename a file that is open
    in Excel. The locked file is never truncated, overwritten or deleted: its bytes stay as they
    were. If it cannot be renamed to STALE, it keeps its normal name and three things happen:
    - a warning file `<name> - OUT OF DATE - DO NOT USE.txt` is written next to it;
    - an ERROR line is logged for it, plus an end-of-run summary (`N report file(s) are OUT OF
      DATE ...`);
    - with `--strict-exit`, the run exits with code 3.

    The week's other files are still renamed. An early stop (quota, 429, breaker) stays a stop
    even when the old report is locked. Never use a report that has such a warning file next to
    it. Close it in Excel and rerun the same command (no `--force`): saved photos are not sent
    again, and the warning file is removed automatically once the report is replaced or renamed.
    See OPERATOR_RUNBOOK.md section F.
- **Final fixes M1/M2 (offline-tested, not yet seen live):**
  - **M1 -- report status:** a finished week says `COMPLETE` only when every photo has a result.
    With failed requests it says `COMPLETE WITH N API FAILURE(S) – not all photos
    classified`; an early stop still says `PARTIAL`. The Image Register is unchanged.
  - **M2 -- provider-wide errors stop the run after one request** (Gemini and Groq): HTTP 401,
    403 and 404, plus a Gemini HTTP 400 whose response names `API_KEY_INVALID` or status
    `FAILED_PRECONDITION`. The photo is saved as `API_FAILURE`, a PARTIAL report is written and
    no later photo or week is sent. Every other HTTP 400 stays a per-photo failure. See the
    runbook's "Provider-wide errors" section.
- **M3 -- cross-week reuse of identical photos (offline-tested):** a successful result for a
  byte-identical photo (SHA-256) saved in another week is copied into the receiving week's own
  state instead of sending a request. It is reused only when its saved provenance -- provider,
  model and a fingerprint of the prompt, schema and request defaults -- matches exactly, so
  results saved before M3 are never reused. Failed, incomplete or conflicting results are never
  reused; `--force` and dry runs never reuse. `--quota-plan` now counts unique outstanding
  images. In this project: 66 AI-assessable photos, 47 unique, so 19 fewer first-attempt
  requests. Each reuse is logged with the source week and hash; the Image Register is unchanged.
- **Phase P1-1 -- HTTP 429 handling (Gemini, offline-tested, not yet seen live):** every 429 is
  classified from Google's response, never from the status code alone:
  - **daily** (QuotaFailure quotaId/description or message naming a per-day window): unchanged
    -- 1 request, never retried, run stops.
  - **per-minute** (quotaId containing `PerMinute`, or "per minute" in description/message): one
    wait (Google's `RetryInfo.retryDelay`; `max_backoff_seconds` if absent) and one retry per
    image. If Google asks for longer than `max_backoff_seconds`, or the retry 429s again, the run
    stops (`GeminiRateLimitedError`, kind `per_minute`).
  - **ambiguous** (any other 429): never retried, run stops (kind `ambiguous`); the original
    (redacted) error text is kept in the log and flagged CSV.
  - The 5-attempts-per-image cap covers 429 and 5xx attempts together. Rate-limit stops are
    neutral for the circuit breaker, leave the image `ok: false` (retried next run, no
    `--force`) and exit 0 like the other stops.
  - `requests_per_minute` now paces Gemini: request starts, retries included, are at least
    60/requests_per_minute seconds apart. `settings.yaml` sets it to 15; there is no code
    default (missing or `null` disables pacing). It is this project's own cap, not Google's
    figure. Groq ignores it.
- The model's default "thinking" budget adds large, highly variable latency (measured 5s-214s
  per image with the full classification prompt, ~77s/image average) -- impractical at 1,058
  images. `thinking_level="LOW"` is supported by `gemini-3.8-flash` (`MINIMAL` is rejected with
  400) and was wired into `GeminiVisionClient(thinking_level=...)`, but could not be validated
  for quality/latency trade-off before the free-tier quota above was exhausted -- re-test this
  once quota is available, before committing to a thinking-level override in `settings.yaml`.

**Phase 3c (performance optimization, offline only -- see automation/README.md git history /
memory for the full report):**
- Classification now uses structured output (`response_mime_type="application/json"` +
  `response_schema=ClassificationSchema` in `vision_client.py`) instead of prose-instructed JSON.
  Combined prompt+schema text is ~2,500 chars vs. the old prompt-only ~3,460 chars, and JSON
  parse failures are no longer possible (the API enforces the schema). Every rule from the old
  prompt is preserved, mostly as per-field `Field(description=...)` text on `ClassificationSchema`.
- Images are now downscaled to 768px on the long side (`image_prep.py`) before being sent for
  classification only -- extracted-image files and report-embedded photos are untouched. This
  targets Gemini's ~768x768 tiling boundary (this project's photos are consistently 850x637,
  just over it) and was visually confirmed (not just measured) to preserve legibility of small
  markings like "1S" at both 768px and even 512px.
- **Still pending a live test** (blocked by the same exhausted quota): whether the structured
  output + resized images actually reduce measured latency/token usage, and whether
  `thinking_level="LOW"` combined with the shorter prompt holds classification quality. Do not
  assume the offline-measured character-count reduction translates 1:1 to token/latency savings
  until confirmed live.

**Groq provider (`GroqVisionClient`, experimental):**
- **Live history (2026-11-12, before P2-8/P2-9):** 7 single-photo live calls. 4 returned valid
  classifications; 3 failed with HTTP 400 `json_validate_failed` in three different ways
  (free-text note, empty `{}`, a repeated-token loop), even with `strict: true`. That is why
  Groq stays experimental and Gemini stays the default.
- Reuses `ClassificationSchema` directly via Groq's JSON Schema structured-output mode
  (`response_format={"type": "json_schema", ...}`) and the same `PROMPT` text as Gemini.
  `reasoning_effort` ("none"/"low"/"medium"/"high"/"default") is Groq's analog to Gemini's
  `thinking_level`, passed through only when explicitly set.
- Retry policy mirrors Gemini's: only `groq.InternalServerError` (every 5xx, confirmed from the
  SDK's own status-code-to-exception mapping) is retried; `groq.RateLimitError` (429) and every
  other 4xx fail immediately, never auto-retried.
- **Phase P2-8 -- Groq failure handling (offline-tested):**
  - **Finding:** the statement above was only true of this project's own retry layer. The groq
    SDK (0.37.1 and 1.7.0, read from its installed source) defaults to `max_retries=2` and
    silently re-sent 429s, 408/409s, 5xx, timeouts and connection errors underneath it. That
    meant up to 3 requests per 429 and up to 15 per 5xx photo.
  - **Finding:** every Groq error was then flattened to a plain `RuntimeError`. A Groq 429 never
    stopped the run, and Groq 5xx or timeouts never reached the circuit breaker, so an exhausted
    quota kept being hit once per remaining photo.
  - **Fix -- SDK retries off:** `groq.Groq(max_retries=0, timeout=60 s / 5 s connect)`, the SDK's
    former default timeout made explicit. The tenacity policy (5xx only, at most 5 attempts) is
    now the only retry layer.
  - **Fix -- 429:** any Groq HTTP 429 is `GroqRateLimitedError`, kind `ambiguous`, sent once and
    never retried. It stops the whole run with a PARTIAL report, exactly like Gemini's
    ambiguous 429. No daily/per-minute distinction is attempted: Groq's 429 format has never
    been observed by this project. `Retry-After` / `retry-after-ms` are parsed for the message
    only.
  - **Fix -- 5xx and timeouts:** a 5xx after retries is `GroqServiceUnavailableError`; a timeout
    is `GroqRequestTimeoutError`. Both count toward the existing circuit breaker.
  - **Unchanged:** other errors (400 `json_validate_failed`, connection errors) remain ordinary
    per-photo `API_FAILURE`s. (401/403/404 now stop the run -- see "Final fixes M1/M2" below.)
  - **How it is wired:** `classifier.py` and `run_pipeline.py` check new provider-neutral bases
    (`ProviderRateLimitedError`, `ProviderServiceUnavailableError`,
    `ProviderRequestTimeoutError`), which the Gemini classes now subclass. Gemini types, behaviour
    and messages are unchanged. Stop messages name the configured provider ("Gemini" unless
    `provider: groq`).
  - **Still not implemented for Groq:** daily-quota identification (pacing was added in P2-9).
- **Phase P2-9 -- independent Groq request pacing (offline-tested):**
  - **Two separate settings, never shared:**
    - `rate_limit.requests_per_minute` paces **Gemini** only.
    - `rate_limit.groq_requests_per_minute` paces **Groq** only.
    - Each client keeps its own clock state, so neither setting nor client affects the other.
  - **Timing:** Groq request starts are at least `60 / groq_requests_per_minute` seconds apart,
    measured with a monotonic clock.
    - The pacer runs before every Groq HTTP attempt, 5xx retries included.
    - The first request never waits, and nothing waits after the last request.
    - A wait never exceeds one interval (at most 60 s).
    - Ctrl+C during a wait sends nothing.
  - **No default (fail-safe):** a live `provider: groq` run without the setting (or with `null`)
    stops with `Configuration error: rate_limit.groq_requests_per_minute is required when
    provider is groq ...`. This happens before `.env` is loaded, before any client is built and
    before any request is made.
    - Groq limits differ by account, model and tier, so the pipeline never assumes one.
    - A dry run and a Gemini run do not need the setting.
  - **Validation:** any value present must be an integer of 1 or more. 0, negatives, decimals,
    text and true/false are rejected in every mode, for either provider, like the other
    `rate_limit` settings.
  - **Choosing a value:** the setting is an operator-chosen safety cap, not Groq's quota. Choose
    it from your own account's per-minute request **and** token limits for the model. Each photo
    request uses several thousand tokens, so the token limit is usually the tighter one.
  - **Limits of pacing:** it cannot guarantee that no 429 occurs. Every Groq 429 still stops the
    run after one request (P2-8), and pacing adds no retries.
  - **Unchanged:** Gemini pacing, retries and messages; the Groq retry, timeout and circuit-breaker
    behaviour.
- `UsageInfo` for Groq only ever populates `output_tokens`/`total_tokens` (from the API's
  `completion_tokens`/`total_tokens`; `prompt_tokens` is not recorded) -- there is no modality breakdown
  (text vs. image tokens) and no separate reasoning/thoughts token count in Groq's response, so
  those fields stay `None`, same degradation path Gemini already supports.
- See "Choosing a provider" above for the free-tier numbers and the TPM pacing caveat.
- **Next step before trusting any of this:** a small approved live `--sample` pilot through the
  pipeline with `groq_requests_per_minute` set. It must show whether the invalid-JSON rate seen
  on 2026-11-12 persists, and exercise the P2-8/P2-9 behaviour live for the first time.
