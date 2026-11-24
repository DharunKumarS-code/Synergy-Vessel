# Synergy Vessel Inspection Automation -- Operator Runbook

How to run, monitor, recover and safely operate the weekly photo-analysis pipeline
(`automation/run_pipeline.py`). Written against the code as of Phase P2-7 (2026-11-13). The
[README](README.md) holds design history and implementation detail; this document is the
operating procedure. Where the two seem to disagree, the code is authoritative -- see
[Known gaps and unresolved questions](#j-known-gaps-and-unresolved-questions).

All commands are run from PowerShell in the automation folder:

```powershell
cd "E:\Synergy Vessel\automation"
```

---

## A. System overview

### What the pipeline does

For each weekly folder (`E:\Synergy Vessel\Week NN`) it:

1. **Finds the source workbooks** -- every `*.xlsx` file directly in the week folder, except
   Excel lock files (`~$...`) and files whose name starts with `Final ` (case-insensitive;
   these are treated as generated reports). Workbooks are ordered by their trailing `(N)`
   part number, e.g. `... (1).xlsx`, `... (2).xlsx`.
2. **Extracts every embedded photo**, in document order, and works out:
   - **Official Location** -- the nearest section-heading row above the photo (a merged cell at
     least `processing.heading_min_merge_width` = 10 columns wide). A photo with no heading
     above it gets `Not Labelled`.
   - **Report date** -- trusted only if every workbook in the week agrees (banner cell and sheet
     name). Otherwise photos are named `Timestamp_Pending_NNN.jpg` and the manifest date is
     `PENDING`.
   - **Exact duplicates** (same image bytes anywhere in the week) are dropped, keeping the
     first; each drop is logged as a warning. Most weeks in this project contain 2.
3. **Classifies only the `Not Labelled` photos** with the vision AI ("AI-assessable" photos).
   Photos with an official location are never sent to the AI -- they cost no request.
   - **Identical photos in different weeks (M3):** if a byte-identical photo (same SHA-256) was
     already classified **successfully** in another week by the same provider, model and
     prompt/schema version, that result is copied into this week's own state file instead of
     sending a request. The log names the source week and the hash. Failed, incomplete or
     conflicting results, and results saved before M3 (no recorded provenance), are never
     reused. In this project 7 photos repeat across weeks 31/34/35/36 and 41/42: 66
     AI-assessable photos, 47 unique images.
4. **Writes outputs** to `automation\output\<week>\`: an Excel report, a manifest CSV and an
   `Extracted Images\` folder. It never writes into the `Week NN` folders or the source
   workbooks.
5. **Records progress** in `automation\.state\<week>.json` (one entry per photo, keyed by a
   SHA-256 hash of the image bytes) so a rerun does not re-send photos that already succeeded.

### Providers

| | Gemini (default) | Groq (experimental) |
|---|---|---|
| Setting | `provider: gemini`, `model: gemini-3.8-flash` | `provider: groq`, `model: qwen/qwen3.8-27b` |
| API key variable | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | `GROQ_API_KEY` |
| HTTP 429 stops the run | Yes; daily / per-minute / ambiguous identified | Yes (P2-8); every 429 is an unclassified stop, never retried |
| Daily-quota identification and message | Yes | **No** -- a Groq daily quota is not identified |
| Circuit breaker (5xx after retries, timeouts) | Yes | Yes (P2-8) |
| Retry / timeout settings | `rate_limit` block (5 attempts, 300 s timeout) | Fixed: 5xx retried, 5 attempts max; 60 s timeout |
| Request pacing setting | `rate_limit.requests_per_minute` (optional; `null` disables pacing) | `rate_limit.groq_requests_per_minute` (**required**, no default; P2-9) |
| Status | Default. Live-tested on 7 photos with an earlier prompt; no pipeline run has saved a successful classification yet (section J) | Experimental: 4 of 7 live single-photo calls valid, 3 invalid JSON (before P2-8/P2-9); not recommended for production runs |

### Where things are

| Path | Contents | Written by |
|---|---|---|
| `E:\Synergy Vessel\Week NN\*.xlsx` | Original source workbooks | Never written |
| `E:\Synergy Vessel\Week NN\Week_NN_Image_Register.xlsx`, `Week NN\Extracted Images - New\` | Extraction-only register and photos (no AI) | `extract_images.py` only, never `run_pipeline.py`; discovery never treats the register as a source workbook |
| `automation\config\settings.yaml` | Provider, model, limits, paths, report name | Operator |
| `automation\.env` | API keys (secret; never share or print) | Operator |
| `automation\.state\Week_NN.json` | Per-photo results and success flag (`ok`) | Live runs |
| `automation\output\<week>\` | Reports, manifests, `Extracted Images\`, STALE files | Live runs |
| `automation\logs\run_YYYYMMDD_HHMMSS.log` | Full run log | Live runs |
| `automation\logs\flagged_items_YYYYMMDD_HHMMSS.csv` | Photos needing human attention (created only if any) | Live runs |
| `automation\dry_run\...` | The same structure, for dry runs only | Dry runs |
| `automation\backups\` | Manual backups (the pipeline never writes here) | Operator |

The state file name replaces spaces with underscores (`Week 30` -> `Week_30.json`).

---

## B. Before starting

### Python environment

- Dependencies are listed in `requirements.txt`: openpyxl, Pillow, pandas, PyYAML,
  python-dotenv, tenacity, google-genai, groq, pydantic. `requirements-dev.txt` adds pytest.
- At the time of writing, the `python` on PATH (3.11.9) has all of them installed. To set up a
  fresh environment:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

- To check the installation without real data or an API key, run the offline test suite (it
  uses synthetic fixtures and refuses to open the real `.env`):

```powershell
python -m pytest -q tests
```

  The last lines should show every test passing and
  `.env guard: 0 blocked open(s) of the real automation/.env`.

### Expected folder structure

```
E:\Synergy Vessel\                 <- paths.project_root
  Week 30\  <source workbooks>.xlsx
  Week 31\  ...
  automation\                     <- paths.automation_root
    run_pipeline.py
    config\settings.yaml, config\.env.example
    .env                          <- you create it; contains the API key
    .state\  output\  logs\  backups\
```

A folder counts as a week only if it sits **directly** under `project_root` and its name
matches `Week <number>` (case-insensitive; the space is optional: `Week30`, `week 30`). Weeks
run in week-number order (Week 9 before Week 10). A week folder without any source workbook is
skipped with a warning.

### Workbook preparation

- Copy the week's original workbooks into a new `Week NN` folder. **Do not edit them.**
- Do not name a source workbook starting with `Final ` -- it would be ignored as a report.
- Keep the `(1)` / `(2)` part-number suffixes. They control photo order and serial numbers.
- Avoid leaving extra copies in the folder: a file such as `Copy of X (1).xlsx` **is** treated as
  a source. Its photos would be de-duplicated against the original, but the report's
  Original Excel Filename column could name the copy.
- Close the workbooks in Excel before a run. Lock files (`~$...`) are ignored, but they show a
  workbook is still open.

### Backups (before every live run)

The pipeline never deletes anything, but it does update `.state\` and `output\` in place. Copy
these to a dated folder first:

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
New-Item -ItemType Directory "backups\pre_run_$stamp" | Out-Null
Copy-Item -Recurse .state, output, logs "backups\pre_run_$stamp" -ErrorAction SilentlyContinue
```

Keep the original `Week NN` workbooks backed up separately, outside the project folder.

### Configuration checks

1. In `config\settings.yaml`, confirm `provider`, `model`, `paths` (all four point at this
   machine's folders) and `report.output_filename`.
2. Reprocessing is controlled **only** by the `--force` command-line option (section D). The old
   `processing.force_reprocess` setting, which nothing ever read, was removed in P2-12. If an
   older copy of `settings.yaml` still contains it, it is ignored.
3. **Groq only:** `rate_limit.groq_requests_per_minute` must be set to a whole number of 1 or more
   (see [Request pacing](#request-pacing-gemini-vs-groq)). Without it, a live Groq run stops with a
   configuration error before sending anything.
4. `.env` must contain the provider's key variable (copy `config\.env.example` to `.env`, then add
   the key). Do not open, print, paste or email `.env`.
5. Run a configuration and extraction check that sends nothing to the AI:

```powershell
python run_pipeline.py --quota-plan
```

   A settings problem prints `Configuration error: ...` and exits with code 1 (section F).

### Safety precautions

- Run **one** pipeline instance at a time. There is no lock (section G).
- Never use `--force` unless you intend to pay the quota cost again (section G).
- Keep reports and manifests closed in Excel while a run is in progress (section F).
- Treat any file whose name starts with `STALE`, `PARTIAL`, `SAMPLE` or `DRY RUN` as **not** the
  current weekly result (section E). The same applies to any file that has a
  `<file name> - OUT OF DATE - DO NOT USE.txt` warning file next to it (section F).

---

## C. Quota planning

### `--quota-plan`

```powershell
python run_pipeline.py --quota-plan
python run_pipeline.py --quota-plan --weeks "Week 41" "Week 42"
python run_pipeline.py --quota-plan --daily-request-limit 20
```

What it does:

- It reads each selected week's workbooks into memory and reads (never writes) `.state\`.
- It makes **no API request**, builds no AI client, does not load `.env`, and writes **nothing**:
  no state, report, image or even a log file.

Output, per week:

```
  Week 30: 6 AI-assessable, 0 done, 6 remaining
```

| Term | Meaning |
|---|---|
| AI-assessable | Photos with no official location (`Not Labelled`), counted after de-duplication. Only these ever send a request. |
| done | AI-assessable photos saved in `.state\` as **successful** (`ok`). This includes answers that need visual review: they were completed requests. |
| remaining | AI-assessable photos that have never been tried, or whose last attempt failed, counted **once per unique image** (M3). Each needs at least one more request. |
| identical to a photo in another week (shown only when non-zero) | Outstanding photos that need no request of their own: another week already holds a reusable result, or an identical photo earlier in the plan is already counted in `remaining`. |

- A week that cannot be read is shown as `could not be read (...) -- not counted`.
- Labelled photos are never counted.
- `--quota-plan` always counts whole weeks; it cannot be combined with `--sample`, `--force` or
  `--dry-run` (exit code 2).
- With `--strict-exit`, it exits with code 3 if any week could not be read, and 0 otherwise.

### `--daily-request-limit N`

This option is accepted only together with `--quota-plan` (otherwise exit code 2), and `N` must
be at least 1. You supply `N` from your own project's limit page
(https://aistudio.google.com/rate-limit). The pipeline never assumes a limit. With `N`, it adds:

> `Conditional estimate: at N request(s)/day ... need at least D quota day(s)`

where `D = ceil(remaining / N)`. This is a **lower bound**, valid only if every photo succeeds on
its first request, the limit does not change, and nothing else uses the same Google project.
Without `N`, no day estimate is printed.

### Observed values vs official limits

| Kind | What is known |
|---|---|
| **Provider-documented** (Google's rate-limit docs, checked 2026-11-12) | Daily quotas reset at **midnight Pacific time** and apply **per Google project**, not per key. Google does not publish a free-tier daily number for `gemini-3.8-flash` there; your project's limits are shown in AI Studio. |
| **Observed in this project** (not an official figure) | A 429 on 2026-11-12 reported `quotaValue 20` (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`). Treat "20 per day" as a past observation; re-check it in AI Studio before planning. |
| **Unknown** | Whether failed requests (503, 429, timeouts) count against the daily quota. Whether the limit is still 20. |
| **Groq** (docs only, never confirmed on a real account) | Free tier published as 30 RPM / 1,000 RPD / 8,000 TPM / 200,000 TPD. The pipeline does not use these numbers. `--quota-plan` and its messages refer to Gemini. |

### Estimating quota days and run duration

- **Requests:** at least one per remaining photo. A Gemini 503 is retried up to 4 times (5
  attempts per photo in total), and a per-minute 429 may add one retry. Plan with a margin.
- **Quota days:** `ceil(remaining / daily limit)` as a minimum. Example: 66 remaining at an
  observed 20/day means at least 4 quota days.
- **Duration per photo** (Gemini):
  - Request starts are at least `60 / rate_limit.requests_per_minute` seconds apart (15/min
    gives 4 s).
  - Measured model latency in this project ranged from about 5 s to 214 s per photo (about 77 s
    on average with the earlier, longer prompt). The current prompt's latency has not been
    measured live.
  - A request is cut off at `request_timeout_seconds` (300 s).
  - 503 backoff waits add roughly 4-8 s, 8-12 s, 16-20 s and 32-36 s.
  - So 20 photos may take anywhere from a few minutes to over an hour. Treat any figure as a
    rough estimate.
- **Duration per photo** (Groq): at least `60 / rate_limit.groq_requests_per_minute` seconds
  between request starts, plus model latency (not measured for this project) and up to 60 s per
  attempt before a timeout.
  - Minimum time for N photos is about `(N - 1) x 60 / groq_requests_per_minute` seconds, if
    every photo succeeds on its first request.

### Request pacing: Gemini vs Groq

| | Gemini | Groq |
|---|---|---|
| Setting | `rate_limit.requests_per_minute` | `rate_limit.groq_requests_per_minute` |
| If missing / `null` | No pacing (current file sets 15) | **Configuration error** for a live run -- no default |
| Valid values | Whole number of 1 or more, or `null` | Whole number of 1 or more |
| Gap between request starts | `60 / value` seconds | `60 / value` seconds |
| Affects the other provider? | No | No |

- **Independent settings:** the two settings are never shared. Changing one never changes the
  other provider's timing.
- **A safety cap, not a quota:** both values are your own caps, not provider quotas. Take the
  Groq value from **your** account's per-minute request and token limits for the model (Groq
  console). Each photo request uses several thousand tokens, so the token limit is often the
  tighter one.
- **Not a guarantee:** pacing does **not** guarantee that no 429 happens. A Groq 429 still
  stops the run after one request (section F), and pacing never adds retries or waits after a
  429.
- **When waits happen:**
  - Retries of a 5xx are paced too.
  - The first request never waits, and there is no wait after the last request.
  - No wait is ever longer than one interval (at most 60 s).
- **Dry runs** make no requests, so they are never paced and do not need the Groq setting. An
  **invalid** value (0, negative, decimal, text) is still rejected in every mode.

---

## D. Running the pipeline

Every command runs on the weeks selected (all discovered weeks by default) in week-number order.

### Command reference

| Purpose | Command | AI requests? |
|---|---|---|
| Normal processing, all weeks | `python run_pipeline.py` | Yes, for remaining photos only |
| Specific weeks | `python run_pipeline.py --weeks "Week 41" "Week 42"` | Yes, for those weeks |
| Sample (first N photos per week) | `python run_pipeline.py --weeks "Week 41" --sample 5` | Yes, for remaining photos among the first N |
| Dry run (no AI) | `python run_pipeline.py --dry-run` | **No** |
| Forced reprocessing | `python run_pipeline.py --weeks "Week 41" --force` | Yes, for **every** AI-assessable photo, including finished ones |
| Strict exit codes | add `--strict-exit` to any of the above | Unchanged |
| Plan only | `python run_pipeline.py --quota-plan` | **No** |
| Register-based input | add `--from-registers` to any of the above (see below) | Unchanged; one request per unique photo |

### `--weeks WEEK [WEEK ...]`

- Give one or more names, quoting any that contain spaces: `--weeks "Week 41" "Week 42"`.
- Matching ignores case and extra spaces.
- Weeks always run in week-number order, whatever order you type them in. A repeated name is
  processed once.
- If **any** name matches no folder, the command stops with exit code 2, lists the unknown
  names and the available week folders, and processes nothing. The same happens if `--weeks`
  has no name after it.

### `--sample N`

- Processes only the first `N` photos (in serial order) of **each** selected week. `N` must be a
  whole number of 1 or more.
- Only AI-assessable photos among those `N` send requests.
- Results are saved to the **real** `.state\`. A later full run therefore skips sample photos
  that already succeeded; it does not repeat them.
- A sample run writes `SAMPLE - ...` files and never touches the week's normal report (section E).

### `--dry-run`

- Builds no AI client, makes **no request**, and does not load `.env`. It still checks
  `settings.yaml`, `--weeks` and the `rate_limit` values.
- Extracts photos and builds the report, but every AI-assessable row is a **placeholder**
  (status `DRY_RUN`). It is **not** a classification.
- Everything is written under `automation\dry_run\` (its own `.state\`, `output\`, `logs\`). The
  report is named `DRY RUN - <report name>`. The real `.state\`, `output\` and `logs\` are never
  touched.
- Dry-run state is never treated as done by a live run.
- Can be combined with `--weeks`, `--sample`, `--force` and `--strict-exit`. `--force` with
  `--dry-run` sends nothing; it only rebuilds the dry-run placeholders.
- `automation\dry_run\` can be deleted at any time.

### `--force`

- Ignores saved state and re-sends **every** AI-assessable photo in the selected weeks (or among
  the first `N` with `--sample`). It also turns off cross-week reuse (M3): identical photos in
  other weeks are sent again too.
- Each re-sent photo costs at least one more request, even if it already succeeded, and the new
  answer replaces the saved one.
- Labelled photos are still never sent.
- **Not** needed to retry failures (section G).

### `--strict-exit`

Without it, a run that stops early or has API failures still exits with code **0**. With it:

| Exit code | Meaning |
|---|---|
| 0 | Everything succeeded. `VISUAL_REVIEW` answers, a finished `--sample` and `DRY_RUN` placeholders count as success. |
| 3 | Ran, but any of: an API failure; a daily-quota, per-minute 429, ambiguous 429, circuit-breaker or provider-wide-error (401/403/404) stop; a failed week; unprocessed photos; weeks not attempted; a report file left OUT OF DATE under its normal name (P2-10); no week processed at all (no week folder with source workbooks found). The last log line lists the reasons. |

These codes apply with or without `--strict-exit`, and take precedence over it:

| Exit code | Meaning |
|---|---|
| 1 | Configuration error (`settings.yaml`, `rate_limit` values, unsupported provider, missing API key). Nothing processed, no requests. |
| 2 | Invalid command line (unknown option or week, bad `--sample`, a forbidden combination). Nothing processed. |
| 130 | Interrupted with Ctrl+C. |

An unexpected crash outside the handled cases shows a Python traceback and exits non-zero.

### Combination rules (enforced; violations give exit code 2)

- `--daily-request-limit` requires `--quota-plan`.
- `--quota-plan` cannot be combined with `--dry-run`, `--force` or `--sample`.
- Any other combination is allowed.

### `--from-registers` (register-based input)

Uses each week's completed extraction-only register (`Week NN\Week_NN_Image_Register.xlsx`) and its
photos (`Week NN\Extracted Images - New\WNN-NNNN.<ext>`) as input instead of re-extracting the
source workbooks. Without the flag nothing changes.

- **Validation first:** a week is processed only if its register passes the same completeness
  check the extraction stage uses (every row, photo and file present and byte-identical, row
  count = images in the source workbooks), and every photo is re-hashed (SHA-256) when read.
  Any problem: that week fails with `RegisterInputError` (no requests for it); other weeks go on.
- **Every register row is kept** (98 per historical week), identified by its Image ID
  (`W30-0001`). Official Location is copied verbatim; only rows with a **blank** Official
  Location are AI-assessed.
- **One request per unique photo:** a repeat of a photo earlier in the same week copies its
  result (also its failure) and is never sent, even with `--force`; an identical photo in
  another week reuses its saved result as before. State (`.state\Week_NN.json`, keyed by
  SHA-256) is shared with the normal mode.
- **Output:** `output\<week>\AI Classification - Week_NN.xlsx` (the usual 11 columns plus
  **Result Source**: `AI request (this run)`, `Saved result (earlier run)`, `Same photo as
  W30-0001 (no AI request)`, `Reused from Week 31 ...`, `Official Location (no AI request)`,
  `Not reached (run stopped early)`) and `AI Classification - Week_NN - manifest.csv` (adds
  `image_id`, `classification_status`, `result_source`, `sha256`). PARTIAL / SAMPLE / STALE /
  OUT OF DATE handling applies to this family only: the normal `Final Official vs AI Location
  Report.xlsx`, `manifest.csv` and `Extracted Images\` are never written, renamed or retired.
  No photo files are written or copied. Dry runs: `dry_run\output\<week>\DRY RUN - AI
  Classification - Week_NN.xlsx`.
- **Registers, their photos and the source workbooks are only read, never modified.**
- `--quota-plan --from-registers` counts unique photos still needing a request; repeat
  appearances within a week are shown separately.
- Image outcome counts in the log are per register row, so a repeat of a failed photo counts
  as a second API failure (only one request is needed to retry both).

### Monitoring a run

- The console and `logs\run_*.log` show one line per week and per failure.
- At the end the run logs a summary line: `Run complete. N week(s) succeeded, ...` (or
  `Run complete WITH API FAILURES`, or a `Run stopped early: ...` explanation).
- It then logs
  `Image outcomes: N classified, N need visual review, N API failure(s), N unprocessed (early stop), N already labelled (no AI needed).`
- `logs\flagged_items_*.csv` lists every photo needing attention, with columns `week`,
  `image_filename`, `official_location`, `ai_suggested_location`, `location_confidence`,
  `activity_identified`, `reason`, `failure_type`. `failure_type` is `API_FAILURE`,
  `VISUAL_REVIEW` or `DRY_RUN`.

---

## E. Understanding outputs

### Report files in `output\<week>\`

`<report>` is `report.output_filename`, i.e. `Final Official vs AI Location Report.xlsx`.

| File | When it exists | Current? |
|---|---|---|
| `<report>` + `manifest.csv` | The latest full (non-sample) run **finished** this week | **Yes** -- the COMPLETE weekly result, **unless** an `OUT OF DATE` warning file names it (row below) |
| `<file name> - OUT OF DATE - DO NOT USE.txt` | A file that should have become STALE was open in Excel, so it kept its normal name (P2-10, section F) | No -- and the file it names is **not** current either |
| `PARTIAL - <report>` + `PARTIAL - manifest.csv` | A full run stopped early in this week (daily quota, 429, circuit breaker) | No -- incomplete |
| `SAMPLE - <report>` + `SAMPLE - manifest.csv` | A `--sample` run finished | No -- covers the first N photos only |
| `PARTIAL - SAMPLE - <report>` + `PARTIAL - SAMPLE - manifest.csv` | A `--sample` run stopped early | No |
| `STALE YYYYMMDD_HHMMSS[ (n)] - <name>` | An older file made out of date (renamed, never deleted) | No -- history only |
| `Extracted Images\YYYY-MM-DD_NNN.jpg` or `Timestamp_Pending_NNN.jpg` | Copies of the photos processed by the latest run | Pipeline copies; originals stay in the workbooks |

Dry runs use the same names under `dry_run\output\<week>\`, with the report named
`DRY RUN - <report>` (for example `SAMPLE - DRY RUN - <report>`). The dry-run manifest names
(`manifest.csv` etc.) carry no `DRY RUN` prefix; the `dry_run\` folder is what identifies them.

### When files become STALE

| Event | Files renamed to STALE |
|---|---|
| A full run stops early | That week's complete and partial files (then a new PARTIAL is written) |
| A full run fails or is interrupted in a week (Ctrl+C, a bad workbook, a failed write) | That week's complete and partial files. **No new report is written.** |
| A full run finishes a week | Any leftover PARTIAL **and** SAMPLE files of that week |
| A sample run stops early, fails or is interrupted | Only that week's SAMPLE files |

Weeks a run never reached are left untouched. Any report in their folders is from an earlier
run.

If a file that should become STALE is open in Excel, it cannot be renamed. It then keeps its
normal name and gets a warning file next to it, `<file name> - OUT OF DATE - DO NOT USE.txt`
(see "Windows file-locking errors" in section F). **Never use a report that has such a warning
file.**

### The report workbook

**Sheet 1, Image Register** -- one row per photo, with 11 columns: Serial Number, Image
Filename, Report Date, Original Excel Filename, Worksheet Name, Official Location, AI Suggested
Location, Extracted Photograph (embedded image), Activity Identified, Location Confidence,
Classification Status.

**Sheet 2, Processing Status**:

| Row | Content |
|---|---|
| Report status | One of: `COMPLETE` (every photo has a result); `COMPLETE WITH N API FAILURE(S) – not all photos classified` (the week finished, but N photos have no classification and are retried on the next run); `COMPLETE FOR A SAMPLE ONLY - first N of M photos (--sample)...`; `COMPLETE FOR A SAMPLE RUN - --sample N covered all M photos...` (either sample status ends with `; WITH N API FAILURE(S) – not all photos classified` when that applies); `PARTIAL - processing stopped early; this is NOT a complete report` |
| Week, Generated | Week name, generation time |
| Photos in this report | `N of M extracted` |
| Stop reason | PARTIAL only, e.g. `Gemini daily quota exhausted` |
| Rows by Classification Status | Counts per status |
| What to do | PARTIAL only: rerun later, no `--force` needed |

**Classification Status values**:

| Status | Meaning |
|---|---|
| `CLASSIFIED` | Either a labelled photo (official location from the heading, no AI used), or an AI answer with no low or Needs-Review confidence |
| `VISUAL_REVIEW` | The AI answered, but its location or activity confidence is Low or Needs Review. A human must check it. Not retried automatically. |
| `API_FAILURE` | The request failed. **No classification.** Retried automatically on the next run. |
| `UNPROCESSED` | Not reached because the run stopped early. Processed on the next run. |
| `DRY_RUN` | Dry-run placeholder. No request was made. |

### Manifest files

Manifest files are UTF-8 CSV with 9 columns: `serial_number`, `image_filename`, `report_date`
(`YYYY-MM-DD` or `PENDING`), `original_excel_filename`, `worksheet_name`, `official_location`,
`ai_suggested_location`, `activity_identified`, `location_confidence`. They have **no
Classification Status column**. To tell `API_FAILURE`, `UNPROCESSED` and `DRY_RUN` rows apart,
use the report (or the flagged CSV). In a manifest, such rows show placeholder text such as
`Needs Review` or `Not Processed (Run Stopped Early)`. Manifests and reports are both written
atomically: a failed write never leaves a half-written file.

### Activity Identified values

| Value | Meaning |
|---|---|
| A specific description (e.g. "Painting the hatch coaming") | The AI saw a person doing an observable task |
| `No activity observed` | The AI reported no person visible |
| `A person is visible; specific task not confirmed` | A person is visible, but the AI gave no clear task (Needs Review) |
| `Needs Review` | The AI was uncertain whether a person is visible, **or** the request failed (check Classification Status), **or** a dry-run placeholder |
| `Not Assessed (Official Location)` | A labelled photo; never sent to the AI, so no activity assessment was made |
| `Not Processed (Run Stopped Early)` | `UNPROCESSED` -- not reached in this run |

**Location Confidence** is `High`, `Medium`, `Low` or `Needs Review`. If the AI's suggested
location is not in the project's known vocabulary (the real heading names, `Main Deck`,
`Hatch Coaming`, or `Cross deck Fwd of # N` / `Cross deck Between # N&M`), the text is kept but
the confidence is forced to `Needs Review`. Labelled photos have no AI location or confidence.

---

## F. Failure handling and recovery

In this section, "Automatic" is what the pipeline does by itself; "Operator" is what you do.
Every failed photo is saved in state as failed (`ok: false`), so **the next normal run retries it
automatically. No `--force` is needed for any recovery below.**

### Daily quota exhausted (Gemini 429, per-day quota)

- **Automatic:**
  - The photo that hit the limit is recorded as `API_FAILURE` and is not retried in this run.
  - No further request is made, and the rest of the week is built offline into a
    `PARTIAL - <report>` (unreached photos are `UNPROCESSED`; photos already done keep their saved
    result). Old files for that week become STALE, and later weeks are not attempted.
  - The log explains what happened: Google's quota ID and suggested retry delay (with the clock
    time it implies), the reset documented as midnight Pacific, and how many AI-assessable photos
    remain. Only if Google reported a daily limit, it adds a conditional day estimate.
  - Exit code 0 (or 3 with `--strict-exit`).
- **Operator:** wait for the quota to reset; confirm the limit in AI Studio; rerun **the same
  command** without `--force`. The pipeline does not wait or reschedule itself.

### Per-minute rate limit (Gemini 429, per-minute quota)

- **Automatic:**
  - The pipeline waits once (Google's `retryDelay`, or `max_backoff_seconds` if Google gives
    none) and retries the photo once.
  - If Google asks for longer than `max_backoff_seconds` (60 s), or the retry is also limited,
    the run stops exactly like a quota stop: the photo is `API_FAILURE`, a PARTIAL report is
    written, and later weeks are not attempted.
  - Client-side pacing (`requests_per_minute`) is meant to prevent this in the first place.
- **Operator:** wait a few minutes and rerun. If it keeps happening, lower
  `rate_limit.requests_per_minute`.

### Ambiguous 429 (neither daily nor per-minute identified)

- **Automatic:**
  - Never retried, and the run stops as above.
  - The redacted error text is kept in the log and in the flagged CSV `reason` column.
- **Operator:** read the error text and check AI Studio for the project's quotas before
  rerunning. Do not rerun in a tight loop.

### Provider-wide errors: bad key, no permission, unknown model

These errors would fail every photo the same way, so the run stops after **one** request
(Gemini and Groq; tested offline only):

| Provider answer | Meaning |
|---|---|
| HTTP 401 | Authentication failed (key missing, invalid or revoked) |
| HTTP 403 | Permission denied (the key or project may not use this model or API) |
| HTTP 404 | The model or resource was not found (check `model` in `settings.yaml`) |
| Gemini HTTP 400 with reason `API_KEY_INVALID` | Authentication failed |
| Gemini HTTP 400 with status `FAILED_PRECONDITION` | A persistent account or region problem |

- **Automatic:**
  - The photo is saved as `API_FAILURE` (retried on the next run), with the key redacted.
  - No further photos or weeks are sent. A `PARTIAL` report is written for the week, like any
    other early stop.
  - The log ends with `Run stopped early: <provider> reported <cause> (HTTP <code>) ...`.
    With `--strict-exit` the run exits with code 3; without it, the exit code is 0 as for the
    other early stops.
- **Operator:** fix the key in `.env` or the provider/model in `settings.yaml`, then rerun the
  same command (no `--force`). Try a new key first with `--sample 1` on one week.

### Other API failures

Examples: an HTTP 400 about one photo (any 400 not listed above, including Groq's
`json_validate_failed`), a network outage, or an invalid response.

- **Automatic:**
  - The photo is recorded as `API_FAILURE` with the error text (keys redacted).
  - Then the run **continues with the next photo**. These failures neither stop the run nor
    count toward the circuit breaker.
  - The end summary says `Run complete WITH API FAILURES`, and the week's report status says
    `COMPLETE WITH N API FAILURE(S) – not all photos classified`.
- **Operator:**
  - Read the error in the log or flagged CSV, and fix the cause (network, input). Then rerun.
  - A new key or configuration should still be tried first with `--sample 1` or `--sample 5`
    on one week.

### Gemini 5xx errors, timeouts and the circuit breaker

- **Automatic (Gemini):**
  - A 5xx response is retried with backoff, up to 5 attempts per photo.
  - A request exceeding `request_timeout_seconds` (300 s) is not retried within the run.
  - Either way, a photo that still fails is `API_FAILURE`.
  - After `circuit_breaker_threshold` (2) **consecutive** such photos, counted across weeks, the
    run stops like a quota stop: a PARTIAL report is written and later weeks are not attempted.
    Any successful request resets the count.
- **Operator:** Gemini is overloaded or unreachable. Rerun later; nothing else is needed.

### Groq differences (important; Phase P2-8)

- **Any Groq HTTP 429 stops the run.**
  - Exactly one request is made: it is never retried, by the pipeline or by the Groq SDK.
  - The stop works like the ambiguous-429 stop above: `API_FAILURE` for the photo, a PARTIAL
    report, later weeks not attempted, and the message `Run stopped early: Groq returned HTTP
    429 (rate limit not identifiable as per-minute or daily)`.
  - The pipeline does **not** try to tell a Groq per-minute limit from a daily quota. Treat
    every Groq 429 as possibly the daily quota.
  - A `Retry-After` header, if Groq sends one, is shown as the provider-suggested wait. It is
    never waited on automatically.
- **Groq 5xx:** retried with backoff of 2-30 s, at most 5 attempts per photo. Before P2-8 the
  SDK's own hidden retries could make this up to 15.
- **Groq timeouts:** a request taking longer than 60 s is not retried.
- **Circuit breaker:** 5xx failures and timeouts count toward it exactly as for Gemini, and the
  stop message names Groq.
- **Provider-wide errors stop the run after one request:** HTTP 401, 403 and 404 (see
  "Provider-wide errors" above). No Groq HTTP 400 stops the run: its error codes have not been
  verified by this project.
- **Still ordinary per-photo failures (the run continues):**
  - HTTP 400 (including `json_validate_failed`, invalid JSON from the model);
  - connection errors.
- **Pacing (P2-9):** Groq uses its own required `rate_limit.groq_requests_per_minute` (section
  C). A cap above what your account really allows makes 429 stops likely on longer runs; lower
  it after a 429 stop.
- **Operator:**
  - Use Groq only for small `--sample` runs.
  - After a Groq 429 stop, wait, then rerun the same command.
  - Project history recorded unreliable structured output from this Groq model (about 3 of 7
    single-photo calls failed to return valid JSON).

### Interrupted processing (Ctrl+C, window closed, power loss)

- **Automatic:**
  - Each photo's result is saved atomically right after its request, so completed work
    survives.
  - On Ctrl+C the run logs `Interrupted by user...` and exits 130. That week's existing report
    files are renamed STALE (a file open in Excel is flagged OUT OF DATE instead; section F),
    and **no new report or PARTIAL report is written** for it.
  - A hard kill or power loss skips even that renaming. The previous report then keeps its
    normal name but may be older than the state.
- **There is no automatic resume.** Nothing restarts by itself.
- **Operator:** rerun the same command (no `--force`). Photos already saved as successful are
  not re-sent, and the week's report is rebuilt. A photo whose request was in flight at the
  moment of interruption may be sent again, which costs one request.

### Failed report or manifest write

Causes include a full disk or a locked file.

- **Automatic:**
  - Reports, manifests and state are written to a temporary file, then swapped in. A failure
    leaves any previous file byte-for-byte intact and removes the temporary file.
  - On a full run the week is recorded as `Failed week: ...`, its previous files are renamed
    STALE, the run continues with the next week, and the classifications already saved in state
    are kept.
  - If the failure happens while writing a PARTIAL report, the log says
    `could not write the partial report ... No current report exists for this week`. If the
    PARTIAL report itself was written, that message is about the PARTIAL manifest.
- **Operator:** fix the cause and rerun the same command. Saved photos are not re-sent, so the
  report is rebuilt without new requests for them.

### Missing or invalid configuration

- **Automatic:**
  - Problems in `settings.yaml` print `Configuration error: ... No images were processed and no
    API requests were made.` and exit 1. All the problems are listed together. This covers a
    missing, unreadable or empty file, invalid YAML, or a missing or mistyped required
    setting. A `paths.project_root` that does not exist or is not a folder is reported the
    same way (exit 1), in every mode including `--dry-run` and `--quota-plan`.
  - A `project_root` that exists but contains no week folder with source workbooks is not an
    error: the run logs `No week was processed ...` as a warning (exit 0, or 3 with
    `--strict-exit`).
  - Invalid `rate_limit` values, an unsupported provider, or a missing API key are reported the
    same way in the log, also with exit 1.
- **Operator:** correct `settings.yaml`, or the key in `.env`, and rerun. Nothing needs cleaning
  up.

### Windows file-locking errors (a report, manifest or photo open in Excel or a viewer)

Windows does not let a program replace or rename a file that Excel has open. Since Phase P2-10
the pipeline handles this explicitly.

- **Automatic:**
  - **The new report:** it is written to a temporary file first. If the final swap is refused
    (`PermissionError`), the temporary file is removed. The open report is **never** truncated,
    overwritten or deleted: its bytes stay exactly as they were.
  - **The week:** it is recorded as failed (`Failed week: ...`). Saved state is kept, so photos
    already done are not sent again.
  - **The STALE renames:** the pipeline then tries to rename the week's old files to STALE. Each
    file is tried separately: one locked file no longer stops the others from being renamed.
  - **If a file cannot be renamed**, it is left untouched under its normal name, and the pipeline
    flags it in three ways:
    - **A warning file next to it:** `<file name> - OUT OF DATE - DO NOT USE.txt`, for example
      `Final Official vs AI Location Report.xlsx - OUT OF DATE - DO NOT USE.txt`. It sorts
      directly after the report in Explorer, and says why the report is out of date and what to
      do.
    - **An ERROR log line** for the file: `... is OUT OF DATE but still has its normal name --
      do NOT use it ...`.
    - **An end-of-run summary:** `N report file(s) are OUT OF DATE but still have their normal
      name ... do NOT use them: ...`. With `--strict-exit`, the run exits with code 3 (`N report
      file(s) OUT OF DATE under their normal name`). Without the flag the exit code is unchanged.
  - **During an early stop** (daily quota, 429, circuit breaker), a locked old report no longer
    disturbs the stop. The run still stops (no further requests or weeks), the PARTIAL report is
    still written, and the locked file is flagged as above.
  - **Clearing the warning:** the warning file is removed automatically once a later run either
    replaces that file with a current one or renames it to STALE. Until then, every run that
    touches the week lists it again in the end-of-run summary.
- **How to recognise an outdated report:** a `... - OUT OF DATE - DO NOT USE.txt` file sitting
  next to it, or its Processing Status sheet's `Generated` time being older than your latest run.
- **Operator:**
  1. Close the report and manifest everywhere: Excel, other Excel windows, and Explorer's preview
     pane.
  2. Rerun **the same command**, without `--force`. Finished photos are not sent again, so the
     retry costs no requests for them.
  3. Check that the warning file is gone and the Processing Status sheet says `COMPLETE` with a
     current `Generated` time.
  4. Do not delete the warning file by hand to "fix" it. It disappears by itself once the report
     really is current. (If the log says the warning file could not be removed, the report it
     names **is** current and the warning file may then be deleted.)

---

## G. Safe operating procedures

- **Protect the originals.**
  - The pipeline only reads `Week NN\*.xlsx` files; keep backups of them anyway.
  - Never save reports or other files into the `Week NN` folders under a source-looking name.
  - Never hand-edit `.state\*.json`.
- **Preserve previous outputs.**
  - Never delete or rename STALE, PARTIAL or SAMPLE files by hand during a recovery. Archive them
    (section H) only after the week has a COMPLETE report you have reviewed.
  - Back up `.state\` and `output\` before each live run (section B).
- **Avoid duplicate API requests.**
  - Recover with a plain rerun, never `--force`.
  - Run `--quota-plan` first to see exactly how many requests are outstanding.
  - Do not delete `.state\`. That makes every photo "new" again.
  - Do not run the same week twice at the same time.
- **When to use `--force`:**
  - Only to deliberately re-classify photos that already have a saved answer, for example
    after a model or prompt change has been approved.
  - Always pair it with `--weeks` (and possibly `--sample`), check the cost with `--quota-plan`
    first, and remember that saved answers are overwritten.
  - **Never** use it to retry failures, finish a PARTIAL week, or after a quota stop.
- **One instance only.**
  - There is no lock against two runs at once.
  - Two concurrent runs would share the state file and the API quota, could send the same
    photos twice, and whichever finishes last overwrites the other's state, report and
    manifest.
  - Before starting, check that no other `run_pipeline.py` is running.
- **Secrets.**
  - Never open, print, paste or share `.env`.
  - Error messages redact the API key, but still avoid posting raw logs publicly.
- **Why backups matter.**
  - State is the only record of which photos were paid for and succeeded. Losing it means
    paying again.
  - Backups also let you restore a known-good report if a reviewed file is overwritten by a
    later finished run.

---

## H. Recommended weekly workflow

1. **Receive the workbooks.**
   - Create `E:\Synergy Vessel\Week NN\` and copy the source workbooks in unchanged, keeping the
     `(N)` suffixes.
   - Keep a separate backup of the originals.
   - Make sure no workbook name starts with `Final ` and there are no stray copies.
2. **Check the environment.** Make sure no other pipeline run is active and close all Excel
   files in `output\`.
3. **Dry run the new week** (no cost):

   ```powershell
   python run_pipeline.py --dry-run --weeks "Week NN"
   ```

   - In `dry_run\output\Week NN\DRY RUN - <report>`, check the photo count, the official
     locations and the report date.
   - Look for `Timestamp_Pending` names, which mean the date was not confirmed.
   - In `dry_run\logs\`, check the duplicate-drop warnings.
4. **Plan the quota:**

   ```powershell
   python run_pipeline.py --quota-plan --weeks "Week NN" --daily-request-limit <your limit>
   ```

5. **Back up** `.state\`, `output\` and `logs\` (section B).
6. **Run a small live sample first** whenever the key, provider or model has changed:

   ```powershell
   python run_pipeline.py --weeks "Week NN" --sample 5 --strict-exit
   ```

7. **Run the week:**

   ```powershell
   python run_pipeline.py --weeks "Week NN" --strict-exit
   ```

   - Exit code 0 means the week is complete.
   - Exit code 3 means read the last log line. Then:
     - After a quota stop, wait for the reset and rerun the same command.
     - After API failures, fix the cause and rerun the same command.
     - Repeat until the exit code is 0, or until only `VISUAL_REVIEW` items remain.
8. **Verify the outputs.**
   - `output\Week NN\` has `<report>` and `manifest.csv` with no prefix.
   - The Processing Status sheet says `COMPLETE`.
   - Rows by Classification Status shows no `API_FAILURE` or `UNPROCESSED`.
9. **Review.**
   - Every `VISUAL_REVIEW` row (also listed in `logs\flagged_items_*.csv`).
   - Any `Needs Review` location confidence.
   - A spot-check of `CLASSIFIED` AI rows against the embedded photos.
10. **Archive.**
    - Copy the reviewed COMPLETE report, its manifest and the run log to your archive location.
    - Then move older STALE, PARTIAL and SAMPLE files for that week out of `output\Week NN\`
      into a dated archive folder, rather than deleting them.
    - Delete `dry_run\` if you no longer need it.

---

## I. Troubleshooting reference

"More requests?" means whether the recommended action may send further AI requests.

| Symptom | Likely cause | What to do | More requests? |
|---|---|---|---|
| `Configuration error: ...` and exit 1 | settings.yaml missing, invalid or incomplete; `paths.project_root` missing or not a folder; bad `rate_limit` value; unsupported provider; missing API key | Fix the named setting or the key; rerun | No (until rerun) |
| `No week was processed: no week folder with source workbooks was found` (exit 3 with `--strict-exit`) | `paths.project_root` points at the wrong folder, or no selected week folder has source workbooks | Fix `project_root` or the week folders; rerun | No (until rerun) |
| `Failed week: ... StateFileError: state file ... is not valid JSON` (or `must contain a JSON object`) | That week's `.state\Week_NN.json` is damaged; it is never modified by the pipeline | Restore it from `backups\`; rerun. Moving it aside instead makes every photo of that week be sent again | Yes, when rerun live |
| `error: unknown week name(s) in --weeks` and exit 2 | Typo, or the folder is not `Week <number>` directly under project_root | Use a name from the "Available week folders" list | No |
| `--quota-plan cannot be combined...` or `--daily-request-limit is only used with --quota-plan` and exit 2 | Forbidden option combination | Remove the conflicting option | No |
| `<week>: no source workbooks found, skipping` | Empty folder, or every file excluded (`Final ...`, `~$...`) | Add or rename the workbooks; rerun | Yes, when rerun live |
| `could not be read (...)` in `--quota-plan`, or `Failed week: ... WorkbookExtractionError` | Corrupt or non-Excel workbook | Replace it with a good copy from the original source | Yes, when rerun live |
| Photos named `Timestamp_Pending_NNN.jpg`, manifest date `PENDING` | The workbooks disagree on the report date | Expected safeguard; confirm the date manually | No |
| `Run stopped early: Gemini's daily request quota was exhausted` | Daily quota used up | Wait for the reset (midnight Pacific per Google's docs); rerun the same command, no `--force` | Yes, after the reset |
| `Run stopped early: Gemini returned HTTP 429 (per-minute ...)` | Per-minute limit still exceeded after one wait | Wait a few minutes; rerun; consider lowering `requests_per_minute` | Yes |
| `Run stopped early: Gemini returned HTTP 429 (rate limit not identifiable ...)` | Ambiguous 429 | Read the error text, check AI Studio quotas, then rerun | Yes |
| `Run stopped early: Gemini was unavailable for N consecutive image(s)` | Circuit breaker: repeated 5xx or timeouts | Rerun later | Yes (each photo up to 5 attempts) |
| `Run stopped early: <provider> reported authentication failed / permission denied / ... (HTTP 401/403/404/400)` | A provider-wide error: wrong key, no access, wrong model name | Fix `.env` or `settings.yaml`; rerun with `--sample 1` first (no `--force`) | Yes |
| `Run complete WITH API FAILURES` | Individual request errors (a photo-specific 400, network, invalid response) | Read the errors in the log or flagged CSV; fix; rerun (no `--force`) | Yes, one or more per failed photo |
| Every AI photo is `API_FAILURE` with the same error | Wrong or expired key, or a persistent provider error | Stop; fix the key or configuration; test with `--sample 1` | Yes |
| `Run stopped early: Groq returned HTTP 429 ...` | A Groq rate limit or quota (not distinguished) | Wait (at least the suggested wait, if shown); consider lowering `groq_requests_per_minute`; rerun the same command; prefer small `--sample` runs | Yes, after waiting |
| `Configuration error: rate_limit.groq_requests_per_minute is required when provider is groq ...` | Groq selected without its pacing cap (no default exists) | Set it to a whole number >= 1 chosen from your Groq account's limits | No |
| `Configuration error: rate_limit.groq_requests_per_minute must be an integer >= 1 ...` | 0, negative, decimal or text value | Fix the value (also rejected in dry runs) | No |
| `Run stopped early: Groq was unavailable for N consecutive image(s)` | Groq 5xx or timeouts (circuit breaker) | Rerun later | Yes (each photo up to 5 attempts) |
| On Groq: many consecutive `API_FAILURE` rows, no stop | Per-photo errors (400 invalid JSON, network) | Read the error; fix; test with `--sample 1` | Yes, one per photo |
| `Interrupted by user ...` and exit 130 | Ctrl+C | Rerun the same command (no `--force`) | Yes, remaining photos only |
| `PermissionError` / `Failed week: ...` while saving | A report, manifest or photo open in Excel or a viewer; or a full disk | Close the files or free space; rerun | Only for photos not yet saved |
| `... is OUT OF DATE but still has its normal name -- do NOT use it`, or a `<file> - OUT OF DATE - DO NOT USE.txt` warning file in `output\<week>\` | The file was open in Excel, so it could not be renamed to STALE (P2-10) | Close it everywhere; rerun the same command (no `--force`); the warning file disappears once the file is current or renamed | Only for photos not yet saved |
| `N report file(s) are OUT OF DATE but still have their normal name` (end of run); with `--strict-exit`, exit 3 | Summary of the line above | As above | Only for photos not yet saved |
| `could not write the partial report ...` | Write failure while stopping early | Fix the cause (lock, disk); rerun later | Yes, remaining photos |
| Report or manifest has a `PARTIAL - ` or `SAMPLE - ` prefix | Run stopped early, or a sample run | Finish with a normal full run | Yes, remaining photos |
| `--strict-exit: exiting with code 3 because ...` | Any non-success (the line lists the reasons) | Handle each listed reason as above | Depends on the reason |
| `FlaggedCsvSchemaError` / `does not match the current schema` | A flagged CSV with an old header already exists at the same timestamped name (rare) | Move the old file out of `logs\`; rerun | Yes, when rerun |
| Dry-run report has `DRY_RUN` rows only | Expected: dry runs never classify | Use a live run for real results | -- |

---

## J. Known gaps and unresolved questions

- **No run lock.** Concurrent runs are not detected (section G).
- **No automatic resume or scheduling.** A stopped run must be restarted by hand.
- **Groq (P2-8):**
  - Every 429 is a safe stop, and 5xx failures and timeouts reach the circuit breaker.
  - The pipeline cannot tell a Groq daily quota from a per-minute limit: the response format has
    never been observed live, so it is not parsed. A Groq stop message never includes a day
    estimate. `--quota-plan` still counts remaining photos correctly for Groq, but its wording
    refers to Google/AI Studio; `--daily-request-limit N` works with any N you supply.
  - Groq pacing (P2-9) has no default and cannot know your account's real limits. A cap set too
    high still produces 429 stops. Pacing is per process; a second concurrent run is not
    coordinated (another reason for one instance only).
  - The new handling has been tested offline only.
  - Groq's only live record is 7 single-photo calls on 2026-11-12, before P2-8/P2-9: 4 valid,
    3 invalid JSON. Treat it as experimental.
- **Whether failed requests count against Gemini's daily quota is unknown.** Google does not
  document it.
- **Live latency of the current prompt has not been measured**, so duration estimates are rough.
- **Not verified live:**
  - the per-minute 429 handling;
  - the circuit breaker;
  - the request timeout;
  - the `requests_per_minute` pacing.

  These are all tested offline only.
- **Hard kill or power loss:** a previous report may keep its canonical name although newer
  results exist in state (section F). Rerun to rebuild it.
- **No successful live classification has been saved yet.** Gemini's earlier 7-photo live test
  used an older prompt; every live pipeline run since then failed on 503s or the daily quota.
- **Excel-lock handling (P2-10)** is tested offline, including a real Windows open-file lock,
  but not with Excel itself.
- **Manifests have no Classification Status column**, so failures and placeholders can be
  identified only from the report or the flagged CSV.
