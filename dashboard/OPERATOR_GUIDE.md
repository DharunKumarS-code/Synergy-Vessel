# Synergy Vessel Dashboard — Operator Guide

This guide covers the inspection dashboard: a web interface (frontend) and a local service (backend)
that run on one Windows computer. It explains how to install, start, use, stop, back up and
troubleshoot the dashboard.

The extraction pipeline in `automation\` is a separate tool with its own documentation
(`automation\README.md`, `automation\OPERATOR_RUNBOOK.md`). Nothing in this guide runs it.

> **Scope.** The dashboard is built for **local use on a single computer**. It has not been prepared
> for network or internet access (no HTTPS, and viewing inspection data needs no sign-in, see §8).
> Do not expose it beyond `localhost` (this computer).

---

## Contents
1. [System requirements](#1-system-requirements)
2. [Installation and environment setup](#2-installation-and-environment-setup)
3. [Starting and stopping the backend](#3-starting-and-stopping-the-backend)
4. [Starting and stopping the frontend](#4-starting-and-stopping-the-frontend)
5. [Ports and local addresses](#5-ports-and-local-addresses)
6. [Creating the first administrator](#6-creating-the-first-administrator)
7. [Signing in, sessions and passwords](#7-signing-in-sessions-and-passwords)
8. [Roles and permissions](#8-roles-and-permissions)
9. [Dashboard overview and navigation](#9-dashboard-overview-and-navigation)
10. [Inspection search and photographs](#10-inspection-search-and-photographs)
11. [Image comparison](#11-image-comparison)
12. [Reports and exports](#12-reports-and-exports)
13. [AI Condition Analysis](#13-ai-condition-analysis)
14. [Excel import](#14-excel-import)
15. [Location review](#15-location-review)
16. [Audit log](#16-audit-log)
17. [Backup and restore](#17-backup-and-restore)
18. [Troubleshooting](#18-troubleshooting)
19. [Known limitations and cautions](#19-known-limitations-and-cautions)
20. [Reference: environment variables](#20-reference-environment-variables)

---

## 1. System requirements

| Item | Requirement |
|---|---|
| Operating system | Windows (all commands in this guide use PowerShell) |
| Python | 3.11 (the installed environment uses Python 3.11.9) |
| Node.js | Current LTS or newer (this computer has Node.js 24.19.0; the project does not pin a version) |
| Disk | Space for the project and its data, plus the same again for each backup (backups are full copies) |
| Browser | A current Chrome, Edge or Firefox |

The project folder is `E:\Synergy Vessel`. The dashboard lives in `E:\Synergy Vessel\dashboard`:

```
dashboard\
  backend\     FastAPI service (Python)
  frontend\    web interface (React / Vite)
  data\        created automatically on first use: accounts, audit log, imports, AI analyses
```

**Historical inspection data is read-only.** The week folders (`Week 30` … `week 42`), their
workbooks and photographs are only ever read by the dashboard. Imports, decisions, accounts and
analyses are stored separately under `dashboard\data\`.

---

## 2. Installation and environment setup

On this computer, both environments are already installed (`backend\.venv` and
`frontend\node_modules` exist). Follow this section only on a new computer or after deleting those folders.

### Backend (Python)
```powershell
cd "E:\Synergy Vessel\dashboard\backend"
python -m venv .venv
.venv\Scripts\python -m pip install "pip==26.3" "setuptools==84.2.1"
.venv\Scripts\python -m pip install --require-hashes -r requirements.txt
```
`requirements-dev.txt` adds the test tools (pytest); it is only needed for running tests.
Both files are **locks** (SV-07): exact versions with SHA-256 hashes, generated from `requirements.in` /
`requirements-dev.in` by pip-tools. To change a dependency, edit the `.in` file and run
`.venv\Scripts\pip-compile --generate-hashes --strip-extras requirements.in -o requirements.txt` (and the same for
`requirements-dev`); never edit a lock by hand.

### Frontend (Node.js)
```powershell
cd "E:\Synergy Vessel\dashboard\frontend"
npm install
```

### Configuration
No configuration is required for normal use. The defaults are:

- Data is read from the project folder.
- Dashboard data goes to `dashboard\data\`.
- Backups go to `E:\Synergy Vessel Backups`.
- All AI features are off.

The settings that can be changed are environment variables, listed in §20. The backend never
reads `.env` files itself. The exceptions are live Groq condition analysis (§13) and live Gemini Location AI
(§15), both off by default: when one is authorised, the automation's own loader reads its key (`GROQ_API_KEY` or
`GEMINI_API_KEY`) from `automation\.env`.

---

## 3. Starting and stopping the backend

Open a PowerShell window and run:

```powershell
cd "E:\Synergy Vessel\dashboard\backend"
$env:SYNERGY_TRUSTED_PROXIES = "127.0.0.1"      # SV-07: nginx (on this computer) is the trusted proxy
.venv\Scripts\python -m uvicorn app.main:app --port 8000 --no-proxy-headers
```

(Set the AI variables from "AI provider" in the same window first if AI analysis should be available.)

- The service listens on `http://127.0.0.1:8000`. That is uvicorn's default address, so only this
  computer can reach it. Do **not** add `--host 0.0.0.0`; that would make the data viewable from the network.
- Keep `--no-proxy-headers`. Without it, uvicorn believes an `X-Forwarded-For` header from any program on this
  computer and treats the request as coming from whatever address the header names, which would let it dodge the
  sign-in and request limits. When the optional nginx proxy is used, trust it explicitly with
  `SYNERGY_TRUSTED_PROXIES` instead (see `INFRASTRUCTURE.md`).
- To check it is running, open `http://127.0.0.1:8000/api/health` in a browser. A healthy
  backend returns `"status": "ok"` and `weeks_loaded` equal to the number of week folders (11
  at present).
- Keep this window open while you use the dashboard.

**To stop it:** press **Ctrl+C** in that window.

When the backend stops, every user is signed out: sessions and sign-in lockouts are kept in memory
only. Data on disk is not affected.

---

## 4. Starting and stopping the frontend

**Production (since SV-07, 2026-11-24): nginx serves the built frontend.** Start the backend first. Then:

```powershell
cd "E:\Synergy Vessel\dashboard\deploy\nginx"
& "C:\tools\nginx-1.30.8\nginx.exe" -p "E:/Synergy Vessel/dashboard/deploy/nginx/" -c synergy-vessel.conf
```

Open **http://127.0.0.1:8088**. nginx serves the files in `frontend\dist\` and forwards every `/api` request to
the backend at `http://127.0.0.1:8000`. It runs in the background (no window).

**To stop it:**
`& "C:\tools\nginx-1.30.8\nginx.exe" -p "E:/Synergy Vessel/dashboard/deploy/nginx/" -c synergy-vessel.conf -s quit`

**After changing frontend source code**, publish it with `cd "E:\Synergy Vessel\dashboard\frontend"; npm test; npm run build`.
Changes are not visible on 8088 until the build has run.

**Development only:** `npm run dev` in `frontend\` starts the Vite development server on
**http://localhost:5173** (hot reload, same `/api` forwarding). Never use it to serve production: it is a
development tool with its own security advisories. Stop it with **Ctrl+C**.

---

## 5. Ports and local addresses

| What | Address | Notes |
|---|---|---|
| Dashboard (use this) | http://127.0.0.1:8088 | nginx serving `frontend\dist` (SV-07); this computer only |
| Development server | http://localhost:5173 | `npm run dev`, development only; fails to start if 5173 is in use |
| Backend API | http://127.0.0.1:8000 | Listens on this computer only (uvicorn default) |
| Backend health | http://127.0.0.1:8000/api/health | Quick check that the backend is running |
| Preview build (optional) | http://localhost:4173 | Only after `npm run build` and `npm run preview` |

If the backend runs on a different address, set `VITE_API_PROXY_TARGET` in
`dashboard\frontend\.env.local` (for example `VITE_API_PROXY_TARGET=http://127.0.0.1:8001`).

---

## 6. Creating the first administrator

The dashboard has **no default account and no default password**. The first administrator is
created from the command line, in an interactive PowerShell window (it does not work from a script):

```powershell
cd "E:\Synergy Vessel\dashboard\backend"
.venv\Scripts\python -m app.cli create-admin
```

You are asked for:
1. a **username**;
2. an optional **display name**;
3. the **password**, twice (it is not shown on screen).

The account is saved in `dashboard\data\auth\`, and the step is recorded in the audit log. Press
**Ctrl+C** at any point to cancel; no account is created.

After that, sign in to the dashboard as this administrator and create further accounts on the
**Administration** page (§8).

---

## 7. Signing in, sessions and passwords

- **Sign in** with your username and password. Sign out with the sign-out control in the interface.
- **Idle timeout:** a session ends after **8 hours** without activity. You will be asked to sign in again.
- **Closing the browser tab** signs you out. The session is kept in the tab's session storage.
- **Restarting the backend** signs everyone out (§3).
- **Lockout:** **5 failed sign-ins within 15 minutes** for one username lock that username for
  **15 minutes**. The message says how long to wait. The lockout also ends if the backend restarts.

**Password rules:**
- 12 to 256 characters;
- must not start or end with a space;
- must not contain the username.

Passwords are stored only as salted PBKDF2-SHA256 hashes (600,000 iterations). They are never
displayed or logged.

---

## 8. Roles and permissions

There are two roles: **Administrator** and **Reviewer**.

| Action | Not signed in | Reviewer | Administrator |
|---|:-:|:-:|:-:|
| View dashboard, inspection search, photographs, image comparison | ✓ | ✓ | ✓ |
| View reports and download PDF / CSV | ✓ | ✓ | ✓ |
| View saved AI analyses and AI status | ✓ | ✓ | ✓ |
| View import history and import details | ✓ | ✓ | ✓ |
| Run an AI condition analysis | — | ✓ | ✓ |
| Review an AI analysis (approve / reject / needs reassessment) | — | ✓ | ✓ |
| View the Location Review queue | — | ✓ | ✓ |
| Preview and commit Excel imports | — | — | ✓ |
| Verify, withdraw or reinstate an import | — | — | ✓ |
| Make location decisions (single row and whole group) | — | — | ✓ |
| Manage location synonyms | — | — | ✓ |
| Manage users; view and verify the audit log | — | — | ✓ |

> **Viewing is public on purpose.** Inspection data is read-only, so viewing does not require
> sign-in, but every change does. This is safe only while the dashboard is reachable from this
> computer alone (§3, §5).

**Administration page** (administrators only):
- Create users (username, password, role, optional display name).
- Change a user's role (make administrator / make reviewer).
- Disable or enable an account.
- Reset a password.
- View recent audit entries and verify the audit log (§16).

The last active administrator cannot be disabled or demoted. Create or enable another
administrator first.

---

## 9. Dashboard overview and navigation

The menu on the left contains:

| Menu item | Purpose | Status |
|---|---|---|
| Dashboard | Inspection statistics (weeks, locations, images, appearances), inspection activity and condition trends | Implemented |
| Inspection Search | Search a location, browse its inspection dates, view photographs date by date | Implemented |
| Image Comparison | Place photographs of the same location from two inspection dates side by side | Implemented |
| AI Condition Analysis | Saved AI assessments of compared photographs and their human review status | Implemented (AI off by default, §13) |
| Reports | Report preview, PDF and CSV downloads | Implemented |
| Excel Import | Preview and import Image Register / Deck Photos workbooks; import history | Implemented |
| Location Review | Pending location reviews across all imports, grouped by original text | Implemented |
| Location Synonyms | Administrator-managed synonyms used when matching imported locations | Implemented |
| Administration | Users, roles and the audit log | Implemented (administrators only) |
| **Settings** | — | **Placeholder only.** It shows an information page; there are no settings to change |

---

## 10. Inspection search and photographs

1. Open **Inspection Search** and type a location (for example a deck area).
2. The page shows how many photographs, appearances, dates and weeks match.
3. Use the date navigation (oldest / previous / next / newest) to move between inspection dates.
4. Click a photograph to view it larger.

Photographs come only from the historical week folders and from committed (not withdrawn) imports.
They are served read-only.

---

## 11. Image comparison

Since SV-08 the comparison follows one **physical viewpoint** (one camera view) through the weeks,
instead of picking "the first photo of each date" for a location.

1. Open **Image Comparison** and choose a location. Locations whose name contains "deck" are also
   listed under **Main Deck Areas** at the top of the list. This is only a shortcut: each entry is
   still its own Official Location and nothing is merged.
2. Choose a **viewpoint** (for example "Top view main deck - view 2"). The list shows how much of
   each viewpoint an administrator has approved.
3. Choose the **earlier** and **later inspection week**. Both photographs always come from the
   chosen viewpoint. If a week has several photographs of that viewpoint, step through them or use
   "Choose from all photos of this viewpoint".
4. The page then shows one of these states:
   - **PHOTOGRAPH NOT AVAILABLE FOR THIS INSPECTION WEEK**: the viewpoint has no photograph for that
     week. Choose another week.
   - **NOT RETAKEN – SAME PHOTOGRAPH**: the later week reused the very same photograph file. There
     is nothing to compare. Use **Show the next week with a new photograph** to jump ahead.
   - **VIEWPOINT MAPPING REQUIRES ADMIN REVIEW**: an administrator has not yet confirmed that both
     photographs show this viewpoint. The pair is not analysed. Administrators get a link to the
     review (§11a).
   - Otherwise the photographs are an approved pair and the inspection result / AI analysis
     appears as described in §13.

Links that contain only the `a` and `b` photograph IDs (from older bookmarks, Search or Reviews)
still work: the page opens the viewpoint and weeks that hold those photographs.

If no viewpoint mapping exists yet, or the location has no mapped viewpoints, the page shows a
notice and falls back to the older location view. In that view the photographs may show different
viewpoints and the AI never analyses them.

### 11a. Viewpoint mapping (administrators)

The mapping is stored in `dashboard\data\locations\viewpoints.json` (or the folder in
`SYNERGY_VIEWPOINT_DIR`). It is additional metadata: historical workbooks, reports, registers and
photographs are never changed, and every photograph keeps its record ID and original Official
Location (Not Labelled photographs stay Not Labelled).

**Generating the proposal (once).** Stop nothing; from `dashboard\backend`, with the same
`SYNERGY_*` folder variables as the backend:

```powershell
.venv\Scripts\python -m app.cli generate-viewpoints --dry-run      # summary only, writes nothing
.venv\Scripts\python -m app.cli generate-viewpoints                # writes viewpoints.json + viewpoints_report.md
```

The generator only reads the historical data. It:

- detects the workbook layout eras (serial numbers were renumbered between Week 37 and Week 38);
- uses a serial number only inside one layout era;
- marks byte-identical files as repeated photographs, never as new inspections;
- flags, but never merges, an identical file under different labels, a serial whose label changes,
  and a change in the number of photographs of a location;
- proposes links across a layout change by position only. These links are always **needs review**;
- never matches by visual similarity or by similar names, and **approves nothing**.

Read `viewpoints_report.md` (inventory, eras, flags and findings) before reviewing. The command
refuses to overwrite a mapping that already contains administrator decisions.
`--replace-proposals` replaces a mapping that holds proposals only. Generation is recorded in the
audit log (`viewpoints_generated`). The running backend picks up the new file on the next request;
no restart is needed.

**Reviewing.** Open **Admin › Viewpoints**. Filter by location, week and status (start with
**Needs review**). Each card shows the earlier and later photograph side by side with week, serial,
Official Location, AI hint, the matching evidence and any flags. You can:

- **Approve same viewpoint**: both photographs become *approved*. Only approved pairs can be
  analysed by the AI.
- **Reject earlier / later photograph**: the photograph leaves the viewpoint. It stays in the file
  and the history, and can be reinstated.
- **Correct…**: move the later photograph to another viewpoint, join another viewpoint into this
  one, split the viewpoint from the later week, or rename it.
- **Approve <viewpoint>** (bulk): approves the remaining unflagged proposals of a viewpoint. It is
  refused while that viewpoint still has flagged photographs.

Changing an approved photograph, moving and joining require a note. Every decision is checked
against the mapping revision you were looking at. If someone else changed the mapping meanwhile,
the decision is refused and the page reloads. Each decision is added to the mapping history
(Decision history) and to the audit log (`viewpoint_decided`, failures as
`viewpoint_decision_failed`).

At least two inspection weeks with photographs must exist for a viewpoint before it can be compared.
Running an AI assessment on a pair is described in §13.

---

## 12. Reports and exports

Open **Reports**. The available filters are:

- inspection week;
- date from / date to;
- location;
- AI classification.

The preview shows what the report will contain. Downloads:

| Download | Contents |
|---|---|
| PDF report | Inspection summary for the chosen filters |
| Inspections CSV | Inspection records matching the filters |
| Analyses CSV | Saved AI condition analyses matching the filters |

If nothing matches the filters, the dashboard says so instead of producing an empty file.

---

## 13. AI Condition Analysis

**All AI is off by default.** With the default settings, no photograph is ever sent to an AI service.

The backend supports three modes, set with environment variables **before starting the backend**:

| Mode | How to enable | What happens |
|---|---|---|
| Disabled (default) | nothing | The AI page shows that AI is unavailable; no analyses can be run |
| Mock (simulated) | `$env:SYNERGY_AI_PROVIDER = "mock"` | Returns built-in sample results. Every result is stored with **simulated = true** and is not a real assessment. `SYNERGY_AI_MOCK_SCENARIO` selects `stable` (default), `deteriorated`, `ameliorated` or `needs_review` |
| Live Groq | `$env:SYNERGY_AI_PROVIDER = "groq"` **and** `$env:SYNERGY_AI_ALLOW_LIVE = "1"` | Sends the two photographs to Groq (vision model `qwen/qwen3.8-27b`) using the automation's existing `GROQ_API_KEY` (in `automation\.env`, or already set in the window). **Use only with explicit authorisation.** `SYNERGY_AI_MODEL` can select another Groq vision model (only documented image-capable models are accepted). If the key is missing, the status shows "not configured" and nothing is sent |

Set these in the same PowerShell window, before the `uvicorn` command in §3. They last until that
window is closed.

**Using it** (signed in, reviewer or administrator):
1. In **Image Comparison**, choose a valid pair and run the analysis. Since SV-08 a pair is eligible only when an
   administrator has approved both photographs as the same viewpoint (§11a) and they are different files. The
   server enforces this for every request (error `viewpoint_not_approved`), before anything is sent to the AI.
   Automatic analysis stays **off** by default.
2. The result has one of these classifications: **Deteriorated**, **Ameliorated**, **Stable** or **Needs review**.
3. Every result starts as **Pending review**. A person sets it to **Approved**, **Rejected** or
   **Needs reassessment** on the **AI Condition Analysis** page. Treat AI output as a suggestion only.

**Failures:** if the AI service is busy or unavailable (for example HTTP 503), the dashboard shows
an error, may ask you to retry later, and saves nothing. Groq server errors are retried at most twice;
a rate limit (HTTP 429) is never retried. Gemini is no longer used for condition analysis (replaced by Groq on
2026-11-20); `SYNERGY_AI_PROVIDER = "gemini"` now means disabled.

> **Caution:** mock results are saved in the same store as real ones (marked simulated). Do not use
> mock mode on the real data store unless you intend to keep those simulated records.

---

## 14. Excel import

Administrators only. Imported data is stored under `dashboard\data\imports\`. The original week
folders are never changed.

### Supported workbooks
Only `.xlsx` files, up to **60 MB** and **5,000 rows/photographs**. Two formats are recognised automatically:

- **Image Register:** the weekly `Week_NN_Image_Register.xlsx` files. A worksheet named
  `Image Register` (or a sheet with the register columns), a header row within the first 10 rows,
  and one photograph per row in the `Extracted Photograph` column. Common column-name spellings are accepted.
- **Deck Photos:** the original vessel workbooks. Worksheets are named by date (for example
  `AUG 02 2026`), with the banner `DECK PHOTOS / M.V. <vessel> / Date: …` in cell A1, section
  headings in wide merged cells, and photographs placed in the grid. If the banner date and the
  sheet-name date disagree, the sheet-name date is used and a warning is shown.

Missing information is reported, never invented.

### Workflow
1. **Preview.** Choose the workbook. The dashboard checks it and shows:
   - the detected format;
   - every row's status (ready, duplicate, skipped or invalid);
   - location matching results.

   Nothing is saved at this step.
2. **Commit.** Confirm the import. Only the exact file that was previewed can be committed; it is
   identified by its SHA-256 fingerprint. If the file changed, preview it again.
3. The new records appear in the dashboard immediately, and the import is listed in **Import history**.

Only one import or import change can run at a time. If another is in progress you will see a
"busy" message; wait and try again.

### Duplicate handling
- **A whole workbook identical to one already in the dataset is rejected.** This covers a
  production register and an earlier import. The message names the existing copy. If that import
  was withdrawn, reinstate it instead of importing it again.
- **Rows already in the dataset** are marked *duplicate* and skipped. This covers the same
  workbook, worksheet, date, serial and photograph, or (for Deck Photos) the same worksheet and photograph.
- **Rows repeated within the same workbook** are marked *duplicate*.
- If every row is a duplicate, blank or invalid, nothing is imported.

### Withdraw and reinstate
- **Withdraw** removes an import's records from the dashboard; its files are kept.
- **Reinstate** brings them back.
- Both actions need a **reason (5–500 characters)** and typing the **import ID** to confirm.
- A reinstate is refused if it would conflict with data already present.

### Integrity check
**Verify** re-hashes the stored workbook and every imported photograph against the import's
manifest and reports any problem. It changes nothing.

---

## 15. Location review

Imported rows are matched against **trusted locations**. These are the historical locations plus
locations an administrator has accepted or entered for imports. Matching uses rules: exact,
normalised spelling, managed synonym, built-in synonym, spelling correction, partial match.
Confident matches are applied automatically; everything else waits for review.

### Review queue
**Location Review** lists every pending row across all active imports, grouped by the original
Excel text.

For each group the page shows:
- how often the text occurs and in which imports;
- the rule-based suggestion at import time and the current match;
- links to the individual rows.

Reviewers can view the queue; **only administrators can decide**.

### Single-row decisions (administrator)
From a row of an import:

| Decision | Effect |
|---|---|
| **Accept** | Use the suggested location (it must be one of the row's trusted suggestions) |
| **Reject** | Reject the suggestion; the record shows its original Excel text, marked as rejected |
| **Keep original** | Keep the original Excel text as the location |
| **Manual** | Enter a location (2–120 characters). If it is a synonym of a known location it is resolved to that location; otherwise it is recorded as a new location |

### Whole-group decisions (administrator)
In the queue, use the group's decision buttons (**Accept suggestion…**, **Enter location…**,
**Keep original…**, **Reject suggestion…**):

1. A **preview** shows how many rows will be decided and which are excluded, and why. Rows are
   excluded if their import is withdrawn, they are already decided, they have no or conflicting
   suggestions, and so on.
2. **Confirm for N record(s)** applies the decision to exactly those rows.
3. The change is **all-or-nothing**. If the group changed since the preview, nothing is applied
   and you are asked to review it again.

### Managed synonyms (administrator)
**Location Synonyms** maps alternative spellings to a trusted location (for example a local name
to the official location). The dashboard refuses a synonym that:
- is empty or too long;
- is already a location name;
- points to an unknown location;
- would create a loop or duplicate an existing synonym;
- would conflict with an existing synonym;
- has numbers that do not match the location's (for example `Hold 2` → `CARGO HOLD NO. 3`).

From a queue group, **Create synonym from this group** opens the page with the text pre-filled.

### Location AI (Gemini) — off by default
Location AI suggests an existing Official Location for Excel location text that the rules could not resolve.
It is **off by default** and is independent of AI Condition Analysis (which uses Groq, §13): changing one never
changes the other. An earlier Groq-based evaluation found little value, so treat suggestions with caution.

**Enable (only with explicit authorisation)** — in the PowerShell window, before starting the backend (§3):

```powershell
$env:SYNERGY_LOCATION_AI_PROVIDER = "gemini"
$env:SYNERGY_LOCATION_AI_ALLOW_LIVE = "1"
# optional: $env:SYNERGY_LOCATION_AI_MODEL = "gemini-3.8-flash"   (default; only Gemini model names are accepted)
```

- **Key:** `GEMINI_API_KEY` in `automation\.env` (the same name the automation uses). It is read by the automation's
  loader only when live use is authorised, and is never shown, logged or returned. If it is missing, empty or not a
  Google API key, Location AI reports "not configured" and nothing is sent.
- **What is sent:** only unresolved location text, the workbook and worksheet names, the source cell, the rules'
  candidates and the list of existing Official Locations. Never photographs, filenames, dates or other inspection
  data. Locations the rules resolve, placeholders (N/A, TBC…) and texts the rules flag as ambiguous are not sent.
- **Human review:** every AI suggestion is labelled AI-generated and stays **Pending review** until an administrator
  decides it in Excel Import or Location Review. Nothing is applied automatically. A suggestion must name an existing
  Official Location with the same number as the text, or it is discarded. Ambiguous answers (several plausible
  locations) and low-confidence answers are flagged in the review reason.
- **Fallback:** if Gemini is unavailable, slow (20 s timeout), rate-limited or returns invalid output, the import
  continues with the rules and human review only.
- **Disable:** close the backend window and start it again without the two variables (or set
  `SYNERGY_LOCATION_AI_PROVIDER` to `disabled`). The Settings page shows the current Location AI state.

---

## 16. Audit log

Important actions are written to a tamper-evident audit log in `dashboard\data\auth\`. Each
entry is chained to the previous one by a hash. Passwords, tokens and keys are redacted.

**Recorded actions include:**

| Area | Actions |
|---|---|
| Sign-in | successful sign-ins, failed sign-ins, lockouts, sign-outs |
| Accounts | created, updated, role changed, enabled/disabled |
| Imports | committed, verified, withdrawn, reinstated (including failures) |
| Location review | location decisions, group decisions, synonym creation/updates/deletion (including failures) |
| AI | analyses run, reused or failed; analysis reviews |
| Backups | created, verified, restored (including failures) |

**To check that the log has not been altered:**
- Dashboard: **Administration → verify audit log**.
- Command line:
  ```powershell
  cd "E:\Synergy Vessel\dashboard\backend"
  .venv\Scripts\python -m app.cli verify-audit
  ```

---

## 17. Backup and restore

Backups are made from the command line only. Run all commands from `E:\Synergy Vessel\dashboard\backend`.

**Default backup location:** `E:\Synergy Vessel Backups`. This folder sits next to the project
(both are in `E:\`), not inside it. It is created automatically by the first backup. To use another
location, set `SYNERGY_BACKUP_DIR` **in the same PowerShell window** before running any backup
command (`create-backup`, `list-backups`, `verify-backup`, `restore-backup`); a variable set in
another window has no effect. The backup tool refuses a location that overlaps a week folder or a
`dashboard\data` folder. Always choose a folder outside `E:\Synergy Vessel`.

The default location is on the same disk as the project, so it does not protect against disk
failure. After each backup, also copy the whole `BKP-...` folder to another drive.

**What a backup contains:**
- the historical week folders and the data files in the project root. A root folder is included
  only if it is named `week N` (for example `Week 43`) or contains
  `Final Official vs AI Location Report.json`. Other root folders are skipped and are not listed
  in the backup;
- AI condition analyses;
- imports;
- accounts and the audit log;
- the viewpoint mapping (`dashboard\data\locations`, section `app/locations`; SV-08). Backups made
  before SV-08 simply have no mapping in them and still verify and restore.

**What a backup does not contain:**
- the application itself: `dashboard\` source code, `dashboard\backend\.venv`,
  `dashboard\frontend` and `node_modules`;
- the `automation\` folder (pipeline code, settings, output, logs and its own `automation\backups`);
- `.env` files and other secrets, `*.log`, `*.tmp`, Excel lock files (`~$...`) and cache folders.

The project is not under version control, so the application code needs its own **source backup**
(`create-source-backup`, see "Source backups" below). A data backup cannot be used without it. API
keys are never backed up and must be re-entered by hand after a rebuild.

### Before you create a backup
Stop the backend (Ctrl+C in its window, §3) and wait until no import or review is in progress.
The backup tool does not stop or lock the dashboard. If the dashboard changes data while a backup
runs, the backup can contain a mix of old and new files.

### Create
```powershell
.venv\Scripts\python -m app.cli create-backup --note "before monthly import"
```
`--note` is optional. Before copying, it checks that there is enough free space. Every file is
hashed while copying and checked again afterwards. Any mismatch aborts the backup. Each backup is
a folder named `BKP-YYYYMMDD-HHMMSS-xxxxxx` (date and time in UTC, plus a random suffix), so an
existing backup is never overwritten.

The backup is built in a `.staging-BKP-...` folder. Only once it is complete is that folder
renamed to `BKP-...`. If the copy fails, the staging folder is removed. If the process is killed
or the computer loses power, a `.staging-BKP-...` folder can be left behind. `list-backups`
shows it as `NOT USABLE`. Once you are sure no backup is running, delete it by hand; it is
never a usable backup.

Backup commands add entries (created, verified, restored, failed) to the production audit log
(§16). This is the only change they make to production data.

### List, inspect and verify
```powershell
.venv\Scripts\python -m app.cli list-backups
.venv\Scripts\python -m app.cli show-backup BKP-...
.venv\Scripts\python -m app.cli verify-backup BKP-...
```
`verify-backup` checks the manifest's own checksum, then re-hashes every file in the backup
against the manifest. It also reports missing files and any extra files. It prints `VALID`
(exit code 0) or `INVALID` with a list of problems (exit code 1).

### Restore
```powershell
New-Item -ItemType Directory "D:\Restore"     # the parent folder must already exist
.venv\Scripts\python -m app.cli restore-backup BKP-... --to "D:\Restore\2026-11-20"
```
- `--to` must be an **absolute path** to a folder that is **new or empty**. Its **parent folder
  must already exist**. The restore creates only the last folder in the path.
- The destination must be outside every production folder: the project (`E:\Synergy Vessel`),
  the data folders, the backup folder and `dashboard\backend`. The tool refuses anything else.
  **Restore never overwrites the live data.**
- Make sure the destination drive has free space at least equal to the backup's size
  (`show-backup`). The restore does not check this itself.
- The backup is verified first. If it is not `VALID`, nothing is restored. The files are then
  copied into a `.restore-staging-...` folder next to the destination, checked against the
  manifest, and renamed into place. If a restore is interrupted, delete any leftover
  `.restore-staging-...` folder by hand.
- The restored folder contains `historical\`, `app\condition_analyses\`, `app\imports\` and
  `app\auth\`, plus `RESTORED_FROM.json` (which backup it came from) and `RESTORE_README.txt`
  (the environment variables to point the dashboard at the copy).
- Putting restored files back into use is a separate, manual decision. Stop the dashboard first.
  Then either point the environment variables in §20 at the restored copy, or copy files back by hand.
  The tool does not do this for you.

Recommended routine: create and verify a backup **before every import session** and at regular intervals.

### Source backups (application code)

There are two kinds of backup, and a full recovery needs **both**:

| | DATA backup (`create-backup`) | SOURCE backup (`create-source-backup`) |
|---|---|---|
| Contains | historical week folders, root data files, accounts, audit log, imports, analyses | application source code and recovery documentation |
| Folder | `E:\Synergy Vessel Backups\BKP-...` | `E:\Synergy Vessel Backups\Source Backups\SRC-...` |
| When | before every import session | after every change to the code, and before upgrades |

```powershell
.venv\Scripts\python -m app.cli create-source-backup --note "after Phase 14.1"
.venv\Scripts\python -m app.cli list-source-backups
.venv\Scripts\python -m app.cli verify-source-backup SRC-...
```

The source backup folder is `Source Backups` inside the backup folder, so it follows `SYNERGY_BACKUP_DIR`.
Each backup is named `SRC-YYYYMMDD-HHMMSS-xxxxxx` (UTC) and is never overwritten. It is built in a
`.staging-SRC-...` folder and published only when complete. `list-source-backups` shows an interrupted
one as `NOT USABLE`; delete it by hand once no backup is running. Source and data backups are listed
by separate commands and neither tool accepts the other's backups.

**What is copied:** only an explicit list of folders and files:
- backend: `app\`, `tests\`, `experiments\`, the requirements files and `pytest.ini`;
- frontend: `src\`, `public\`, `index.html`, `package.json`, `package-lock.json` and the Vite,
  Tailwind and PostCSS configuration;
- automation: `src\`, `tests\`, `config\`, `run_pipeline.py`, `extract_images.py`, the requirements
  files, `README.md`, `OPERATOR_RUNBOOK.md`; and small evidence files from `output\`, `live_tests\`
  and `logs\` (CSV/JSON only);
- this guide (`dashboard\OPERATOR_GUIDE.md`).

**Never copied** (each exclusion is recorded in the manifest):
- `.env` files of any name, including `.env.example`, and key or credential files. Their contents are
  never opened;
- `dashboard\data\` (accounts, audit log, imports): use DATA backups;
- week folders, root data files, photographs and Excel workbooks (only small synthetic Excel test
  fixtures under `tests\...\fixtures\` are kept);
- `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `dist`, build and temporary folders, `*.log`
  files, `.state` run-state folders, and `automation\backups\`.

Each backup also contains `ENVIRONMENT.txt`: operating system, Python, Node.js and npm versions, the
exact installed Python packages, rebuild steps, and the **names** of the environment variables (never
their values).

**Verify:** `verify-source-backup` checks the manifest checksum, `ENVIRONMENT.txt`, and every file's
presence, size and SHA-256. It also reports unexpected files and any file that should have been
excluded. It only reads; it never repairs or deletes anything. It prints `VALID` (exit code 0) or
`INVALID` with the problems (exit code 1).

**Recovery after losing the project folder:**
1. Copy the `files\` folder of the latest valid `SRC-...` backup to a new project folder
   (for example `E:\Synergy Vessel`).
2. Rebuild the environments as described in its `ENVIRONMENT.txt`: install Python 3.11 and Node.js,
   create `dashboard\backend\.venv` and install the requirements, run `npm ci` in `dashboard\frontend`.
3. Restore the latest valid DATA backup with `restore-backup` and point the `SYNERGY_*` variables at it
   (or copy it into place as described above).
4. Re-enter API keys by hand if AI features are ever authorised. No backup contains them.
5. Run the backend tests, start the dashboard (§3, §4) and check `/api/health`.

**Limitations:**
- Source and data backups on `E:` are on the **same physical disk** as the project (C:, D: and E: are
  partitions of one disk). They protect against deleted or damaged files, **not against disk failure**.
  Copy the whole `E:\Synergy Vessel Backups` folder to an external drive or another computer regularly.
- Backend package versions are pinned exactly, with hashes, in `requirements.txt` (SV-07), so
  `pip install --require-hashes -r requirements.txt` rebuilds the same environment.
- There is no `restore-source-backup` command: restoring source code is the manual copy in step 1.

### Recovery-artefact backups (pre-change snapshots)

`automation\backups\` and `automation\.state\` hold snapshots taken before earlier data changes (for
example the image registers from before the 16 November Official Location fix). They are **not** in
data or source backups, and some exist nowhere else, so they have their own backup type:

```powershell
.venv\Scripts\python -m app.cli create-recovery-backup --note "pre-fix snapshots"
.venv\Scripts\python -m app.cli list-recovery-backups
.venv\Scripts\python -m app.cli verify-recovery-backup RA-...
```

- **Location:** `E:\Synergy Vessel Backups\Recovery Artefacts\RA-YYYYMMDD-HHMMSS-xxxxxx` (UTC; follows
  `SYNERGY_BACKUP_DIR`). Never overwritten; built in `.staging-RA-...` and published only when complete.
- **Contains:** only files inside `automation\backups\` and `automation\.state\`, with their folder
  structure, and only snapshot file types (Excel, CSV, JSON, images). `automation\backups\` must exist;
  a missing `.state\` is noted in the manifest.
- **Never contains:** `.env` files, key or credential files (never opened), logs, temporary and lock
  files, caches, other file types, and symbolic links. Each exclusion is recorded in the manifest.
  Nothing outside the two folders is read.
- **Verify:** `verify-recovery-backup` checks the manifest checksum and every file's presence, size and
  SHA-256, and reports unexpected, unsafe or forbidden files. It only reads. Prints `VALID` / `INVALID`
  (exit code 0 / 1).
- **When:** these snapshots do not change, so one verified backup is enough until a new snapshot
  is added to `automation\backups\`.
- **Restoring:** there is no restore command. If needed, copy files from the backup's `files\` folder
  by hand to a separate location and compare them; never overwrite production week folders with them.
- Each backup command adds an audit entry (`recovery_backup_created`, `_verified`, `_failed`).
- Like the other backups, this folder is on the same physical disk as the project: copy it to an
  external drive.

### Verification reports (evidence of each check)

Add `--report` to any verify command to save its result as a text file:

```powershell
.venv\Scripts\python -m app.cli verify-backup BKP-... --report
.venv\Scripts\python -m app.cli verify-source-backup SRC-... --report
.venv\Scripts\python -m app.cli verify-recovery-backup RA-... --report
```

- **Location:** `E:\Synergy Vessel Backups\Backup Reports\` (follows `SYNERGY_BACKUP_DIR`). Reports
  are kept beside the backups, never inside a backup folder; backups and manifests are not touched.
- **Names:** `DATA_<BKP-id>_<UTC>.txt`, `SOURCE_<SRC-id>_<UTC>.txt`, `RECOVERY_<RA-id>_<UTC>.txt`,
  with the UTC time as `YYYYMMDDTHHMMSSZ` (e.g. `RECOVERY_RA-20261120-052643-bc97f5_20261120T060000Z.txt`).
  An existing report is never overwritten (a second one in the same second gets `_2`). An
  unrecognisable ID is saved as `INVALID-ID`.
- **Contents:** time, type, ID, location, result, exit status, files checked, size, manifest status,
  counts of missing / unexpected / hash-mismatched / size-mismatched / forbidden files, every
  problem line, and the command used. Paths and counts only: no file contents, secrets or
  environment values.
- **Reading the result:** `VALID` (exit 0) means the manifest is intact and every file matched its
  size and SHA-256. `INVALID` (exit 1) means at least one problem is listed: do not rely on that
  backup; keep it (do not delete or "repair" it) and make a new backup from production. If the
  report itself cannot be saved, the command prints `Error: the verification report was NOT saved`
  and exits 3 (or 1 if the backup was also invalid).
- **Do not use `> file` redirection** instead: Windows PowerShell 5.1 writes UTF-16 and the exit status
  is not recorded.
- **Routine:** after creating any backup, and before and after copying backups to another drive,
  verify every backup with `--report`. Keep the reports with the backups (they are small text files)
  and copy the `Backup Reports` folder along with them.
- Reports are **evidence that a check passed or failed. They are not backup copies** and cannot
  restore anything.

### Offline copy to an external drive

All backups above live on the same physical disk as production (C:, D: and E: are one NVMe disk).
`copy-backups-offline` puts a verified copy of the **whole backup collection** on a separate drive:

```powershell
.venv\Scripts\python -m app.cli copy-backups-offline --to F:\
```

**Drive requirements:** a USB drive (SSD or HDD, 250 GB or more recommended; the collection is
currently about 0.7 GB), formatted NTFS or exFAT, with a normal drive letter. The command refuses:
relative paths, missing or disconnected drives, network/UNC paths, drives mounted into a folder,
symlinks/junctions, **Disk 0 and any drive on the same physical disk as the backups or production**,
destinations inside (or containing) the backup folder or project, and too little free space. If the
physical disk cannot be identified, it refuses (it never guesses).

**What happens:**
1. Every backup in `E:\Synergy Vessel Backups` is verified first (read-only). If any is INVALID,
   nothing is copied.
2. Only valid `BKP-`, `Source Backups\SRC-`, `Recovery Artefacts\RA-` folders and report `.txt`
   files are copied. Staging folders, stray files and anything else are skipped and listed.
3. Files go into `F:\Synergy Vessel Offline Backups\YYYY-MM-DD_HHMMSSZ.incomplete\`. Nothing is
   overwritten or deleted, on either side.
4. Every copied backup is verified against its own manifest with the normal verify commands, and a
   report for each is written into the copy's `Backup Reports\`. File counts, total size and the
   source collection (unchanged) are also checked.
5. Only if everything passed, the folder is renamed to `YYYY-MM-DD_HHMMSSZ` (no `.incomplete`).

**Confirming success:** the command prints `OFFLINE COPY COMPLETE and VERIFIED` and exits 0; the
dated folder has no `.incomplete` suffix; `OFFLINE_COPY_STATUS.txt` starts with `COMPLETE`; and every
new report in its `Backup Reports\` says `Result : VALID`.

**If it fails:** the command prints `OFFLINE COPY FAILED` and exits 1. A `….incomplete` folder is
**not a usable backup**. Its `OFFLINE_COPY_STATUS.txt` says `FAILED:` and why (or `IN PROGRESS` if the
computer stopped mid-copy). It is kept for inspection, never deleted automatically. Fix the cause
(reconnect the drive, free space, fix an INVALID source backup) and run the command again. It
creates a new dated folder. Delete old `.incomplete` folders by hand only after reading their status.

**Routine (manual, not scheduled):**
1. Plug in the drive and note its letter.
2. Run `copy-backups-offline --to <letter>:\` and wait for `COMPLETE and VERIFIED`.
3. Click **Safely Remove Hardware**, unplug the drive and store it away from the computer (another
   room or building; ideally rotate two drives, one kept off-site).
4. Repeat after each import session or new backup, and at least monthly.

This command is **not** for automatic or scheduled mirroring. It never syncs or deletes, so old dated
copies accumulate until you remove them by hand. Keep the external drive **disconnected** except
during this routine: a connected drive can be damaged by the same ransomware or accident as the
computer. There is no restore command: to recover, copy a verified dated folder back to the
computer and use the normal restore steps above.

---

## 18. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Dashboard shows "service unavailable" / backend unreachable | Backend not running | Start it (§3); check http://127.0.0.1:8000/api/health |
| `npm run dev` fails: port 5173 in use | Another copy is already running | Use the running copy, or close the other window |
| `uvicorn` fails: address already in use | Backend already running on 8000 | Use the running copy, or stop it with Ctrl+C in its window |
| http://localhost:5173 does not open | Name resolution of `localhost` | Start with `npm run dev -- --host 127.0.0.1` and open http://127.0.0.1:5173 |
| "Sign in to perform this action" (401) | Not signed in, idle timeout, tab closed, or backend restarted | Sign in again |
| "Forbidden" (403) | Action needs an administrator | Ask an administrator |
| "Too many failed sign-in attempts" | Lockout | Wait the minutes shown (15 at most) |
| Lost the only administrator password | — | Run `create-admin` (§6) to create another administrator, then reset the password on the Administration page |
| AI analysis unavailable | AI disabled (default) | Expected. See §13 |
| AI error / 503 | AI service busy | Try later. Nothing was saved |
| Import rejected as duplicate workbook | Same file already in the dataset | Open or reinstate the existing import named in the message |
| Import "busy" | Another import change in progress | Wait and retry |
| "Preview mismatch" on commit | File changed after preview | Preview again, then commit |
| Group decision "group changed" | Queue changed since preview | Review the group again and confirm the new count |
| "… unreadable" errors | A data file under `dashboard\data\` is damaged | Restore that file from a verified backup (§17) |
| `create-admin must be run in an interactive terminal` | Run from a script or redirected input | Run it directly in a PowerShell window |
| Restore: "the parent folder of the destination does not exist" | `--to` points inside a folder that does not exist | Create the parent folder first (§17) |
| Restore: "the restore destination is not empty" | `--to` already contains files | Choose a new folder name |
| Restore: "refusing to restore into or around a production/application folder" | `--to` is inside, or contains, the project, data or backup folder | Restore somewhere else, for example `D:\Restore\...` |
| `list-backups` shows `NOT USABLE .staging-BKP-...` | A backup was interrupted | Make sure no backup is running, then delete that folder (§17) |
| Backups appear in the wrong place, or `list-backups` shows none | `SYNERGY_BACKUP_DIR` set in a different window, or not set | Set it in the window where you run the command (§17, §20) |

---

## 19. Known limitations and cautions

- **Local use only.** No HTTPS, and viewing is public. Never bind the backend to `0.0.0.0` or
  publish the ports.
- **Restart signs everyone out.** Sessions and lockouts are not kept across backend restarts.
- **Settings page is a placeholder.** Configuration is done only with environment variables (§20).
- **Backups are CLI only.** The dashboard has no backup screen.
- **Data backups contain data, not the application.** Make a source backup as well
  (`create-source-backup`, §17). Stop the backend before a data backup; the tool does not lock the
  dashboard. All local backups share one physical disk: copy them to an external drive.
- **AI is off by default.** Live AI requires explicit authorisation and both variables in §13.
  Location AI (Gemini, §15) is also off unless separately authorised.
- **Historical data is read-only.** Corrections to imported data are made through withdraw/reinstate
  and location decisions, not by editing files.
- **Do not edit files under `dashboard\data\` by hand.** Damaged files are reported as "unreadable"
  and must be restored from a backup.

---

## 20. Reference: environment variables

All are optional. Set them in the PowerShell window before starting the backend **or running a
command-line tool**, for example `$env:SYNERGY_BACKUP_DIR = "F:\SV Backups"`. They apply only to
that window. Set the same values for the backend and for the CLI (`create-admin`, `verify-audit`,
backup commands), otherwise they may use different folders.

| Variable | Default | Purpose |
|---|---|---|
| `SYNERGY_DATA_ROOT` | the project folder `E:\Synergy Vessel` | Where the historical week folders are read from |
| `SYNERGY_IMPORT_DIR` | `dashboard\data\imports` | Import store |
| `SYNERGY_ANALYSIS_DIR` | `dashboard\data\condition_analyses` | AI analysis store |
| `SYNERGY_AUTH_DIR` | `dashboard\data\auth` | Accounts and audit log |
| `SYNERGY_BACKUP_DIR` | `E:\Synergy Vessel Backups` | Backup location |
| `SYNERGY_VIEWPOINT_DIR` | `dashboard\data\locations` | Viewpoint mapping (`viewpoints.json`, SV-08). There is deliberately no variable that lets the AI analyse unapproved pairs |
| `SYNERGY_AI_PROVIDER` | `disabled` | `disabled`, `mock` or `groq` (any other value, including `gemini`, means disabled) |
| `SYNERGY_AI_ALLOW_LIVE` | off | `1` permits live Groq requests (authorisation required) |
| `SYNERGY_AI_MOCK_SCENARIO` | `stable` | Mock result: `stable`, `deteriorated`, `ameliorated`, `needs_review` |
| `SYNERGY_AI_MODEL` | `qwen/qwen3.8-27b` | Groq vision-model override (documented image-capable Groq models only) |
| `SYNERGY_LOCATION_AI_PROVIDER` | `disabled` | `disabled`, `mock`, `gemini` (or `groq`); Location AI only (§15) |
| `SYNERGY_LOCATION_AI_ALLOW_LIVE` | off | `1` permits live Location AI requests (authorisation required) |
| `SYNERGY_LOCATION_AI_MODEL` | `gemini-3.8-flash` for Gemini | Location AI model override |
| `SYNERGY_RATE_LIMIT_ENABLED` | on | `0` turns API rate limiting off (see `dashboard\INFRASTRUCTURE.md`) |
| `SYNERGY_RATE_LIMIT_<CATEGORY>` | see `INFRASTRUCTURE.md` | Requests per minute per user/address for `LOGIN`, `AI_ANALYSIS`, `REPORTS`, `IMPORTS`, `WRITES`, `IMAGES`, `READS` (`0` = unlimited) |
| `SYNERGY_TRUSTED_PROXIES` | empty | Proxy addresses whose `X-Forwarded-For` is trusted, e.g. `127.0.0.1` behind the local nginx proxy. The only way that header is trusted (the backend is started with `--no-proxy-headers`, §3) |
| `SYNERGY_CORS_ORIGINS` | `http://localhost:5173,http://localhost:3000` | Browser origins allowed to call the API directly |
| `SYNERGY_CACHE_TTL_SECONDS` | `300` | Lifetime of cached read-only summaries (`0` disables the cache) |
| `SYNERGY_AI_BREAKER_THRESHOLD` | `3` | Consecutive AI provider failures before condition analysis is paused |
| `SYNERGY_AI_BREAKER_RECOVERY_SECONDS` | `60` | Pause before one trial request is allowed through again |

Frontend (in `dashboard\frontend\.env.local`, optional):

| Variable | Default | Purpose |
|---|---|---|
| `VITE_API_PROXY_TARGET` | `http://127.0.0.1:8000` | Backend address the dev server forwards `/api` to |
| `VITE_API_BASE_URL` | empty (same origin) | Only for calling a backend on another origin directly; not needed normally |
