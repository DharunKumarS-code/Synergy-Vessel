# Synergy Vessel – Infrastructure and Production Readiness

Last updated 2026-11-23. This document covers rate limiting, caching, the circuit breaker, the reverse
proxy, CI and the readiness for load balancing, an API gateway and service discovery. Business logic
(inspection data, AI assessment, reviews, reports, imports) is unchanged; the infrastructure code lives in
`dashboard/backend/app/infra/`.

---

## 1. Current architecture

```
                 Browser (React app)
                        |
        development: Vite dev server :5173  ------ or ------  local proxy: nginx 127.0.0.1:8088
        (/api proxied to the backend)                         (static files + /api to the backend)
                        |                                                |
                        +-------------------+----------------------------+
                                            |
                         FastAPI backend, ONE process, 127.0.0.1:8000
                         middleware: CORS -> rate limiter -> routes
                         auth (bearer sessions in memory), validation, error handling, audit log
                                            |
              +--------------------+--------+-----------+-----------------------+
              |                    |                    |                       |
      InspectionStore       AnalysisService       ImportService           AuthService
      (built in memory      + circuit breaker     (Excel imports;         (accounts, sessions,
       at start-up; read    + AnalysisStore       reload of the            sign-in lockout)
       only)                  (JSON files)         InspectionStore)
              |                    |                    |                       |
      week folders, Excel    dashboard/data/      dashboard/data/         dashboard/data/auth
      registers, photos      condition_analyses   imports                 (JSON + audit log)
      (never modified)              |
                             AI provider (disabled / mock / Gemini / Groq) -- external, opt-in only
```

As a gateway view:

```
Client
   |
Reverse proxy / gateway   (nginx: single entry point, routing, size limits, timeouts, compression)
   |
FastAPI backend           (authentication, rate limiting, validation, error handling, audit)
   |
Application services      (inspections, AI analysis + circuit breaker, reviews, reports, imports)
   |
Data storage              (read-only historical data; JSON stores for assessments, imports, accounts)
```

---

## 2. Decisions

| Concept | Before | Decision | What was done |
|---|---|---|---|
| Rate limiting | Only a per-username sign-in lockout | **IMPLEMENT NOW** | In-process sliding-window middleware (`app/infra/rate_limit.py`) |
| Caching | None (reads 3–25 ms from memory) | **IMPLEMENT NOW (limited)** | Small TTL cache for vessels, locations and the report preview (`app/infra/cache.py`) |
| Load balancing | Not possible (state in one process) | **PREPARE FOR FUTURE** | Blockers and the path forward documented (§5) |
| Reverse proxy | None (Vite proxy in development) | **IMPLEMENT NOW (configuration)** | `dashboard/deploy/nginx/synergy-vessel.conf` (§6) |
| API gateway | FastAPI already enforces auth / validation / errors | **NOT REQUIRED CURRENTLY** | No extra service; responsibilities split between nginx and FastAPI (§7) |
| CI/CD | None | **IMPLEMENT NOW (CI only)** | `.github/workflows/ci.yml` (§8); no deployment |
| Service discovery | Two services with configurable addresses | **NOT REQUIRED CURRENTLY** | `SYNERGY_CORS_ORIGINS` added; readiness documented (§9) |
| Circuit breaker | Retries inside the Gemini client only | **IMPLEMENT NOW** | Breaker around the AI condition-analysis provider (`app/infra/circuit_breaker.py`) |

---

## 3. Rate limiting

Implemented as FastAPI middleware with no extra dependency. Each request under `/api/` falls into one
category; each category has a limit **per client per 60 seconds** (sliding window). The client is the
signed-in user when the request carries a valid session token, otherwise the client IP address. Sign-in
attempts are always counted per address (this adds to the existing lockout of a username after 5 failures).

| Category | Requests | Default per minute | Environment variable |
|---|---|---|---|
| `login` | `POST /api/auth/login` | 10 per address | `SYNERGY_RATE_LIMIT_LOGIN` |
| `ai_analysis` | `POST /api/ai-analysis/compare` | 6 | `SYNERGY_RATE_LIMIT_AI_ANALYSIS` |
| `reports` | PDF / CSV downloads under `/api/reports/` (not the preview) | 10 | `SYNERGY_RATE_LIMIT_REPORTS` |
| `imports` | `POST /api/imports`, `POST /api/imports/preview` (workbook upload) | 10 | `SYNERGY_RATE_LIMIT_IMPORTS` |
| `writes` | every other `POST` / `PUT` / `PATCH` / `DELETE` (reviews, withdrawals, admin changes) | 60 | `SYNERGY_RATE_LIMIT_WRITES` |
| `images` | `GET /api/images/*` | 1200 | `SYNERGY_RATE_LIMIT_IMAGES` |
| `reads` | every other `GET` under `/api/` | 600 | `SYNERGY_RATE_LIMIT_READS` |
| — | `/api/health`, `OPTIONS`/`HEAD`, frontend files | never limited | — |

* A value of `0` removes the limit for that category; `SYNERGY_RATE_LIMIT_ENABLED=0` turns limiting off.
* **Response:** HTTP 429 with `Retry-After: <seconds>` and
  `{"detail": "Too many requests. Please wait N seconds and try again.", "code": "too_many_requests"}`.
  The response carries CORS headers so the browser can show it. The frontend shows a friendly message
  (sign-in shows the server's text; page loads show "Too many requests. Please wait a moment…").
* **Client addresses and `X-Forwarded-For` (fix M1, 2026-11-23).** Two layers keep the header from being forged
  to get a fresh request budget:
  1. The backend is started with **`--no-proxy-headers`** (OPERATOR_GUIDE.md §3). By default uvicorn trusts
     `X-Forwarded-For` from 127.0.0.1/::1 and replaces the client address with it *before* the application
     sees the request – which let any local program pick its own rate-limit identity.
  2. The rate limiter uses the header only when the direct peer is listed in `SYNERGY_TRUSTED_PROXIES`. A request
     that carries the header from anyone else is put into **one shared identity** (`forwarded-untrusted`), so
     forged values never create new identities – even if the flag were forgotten. Normal browser requests do
     not send the header and keep their own address.
* **Behind the proxy:** every request then comes from 127.0.0.1. Set `SYNERGY_TRUSTED_PROXIES=127.0.0.1` (and
  keep `--no-proxy-headers`) so the address nginx forwards (`X-Forwarded-For`, right-most entry – the one nginx
  itself appended) is used for each client.
* Normal browsing stays far below the limits (the read limit is 10 requests per second, sustained).
* Logged when triggered (category and user id or address only; never tokens or passwords).

---

## 4. Caching

Measured on the real data before the change: `/api/vessels` ≈ 10 ms, `/api/locations` ≈ 11 ms,
`/api/reports/preview` 5–16 ms, `/api/inspections?limit=2000` ≈ 20 ms (1.1 MB). Everything is already
held in memory, so caching gives a modest saving; it was kept deliberately small.

| Cached | Key | Invalidated when |
|---|---|---|
| Vessel list (`GET /api/vessels`) | – | the inspection data is reloaded |
| Location list (`GET /api/locations`) | vessel, include_placeholder | the inspection data is reloaded |
| Report preview (`GET /api/reports/preview`) | all filters + assessment-store version | the inspection data is reloaded **or** any assessment is saved or reviewed |

* **TTL:** 300 seconds (`SYNERGY_CACHE_TTL_SECONDS`; `0` disables the cache). At most 256 entries (oldest
  evicted first).
* **Invalidation is exact, not time-based.** Every entry records the `InspectionStore` object it was computed
  from. Imports, withdrawals, reinstatements and location decisions all *replace* that object (they never
  edit it), so an entry from the old data can never be returned for the new data; the cache is also cleared
  on every reload. The report preview additionally keys on `AnalysisStore.version`, which increases on every
  save, so a new review is visible on the very next request.
* **Never cached:** sessions, accounts, sign-in, the audit log, individual assessments and their history,
  image files (the browser already caches them for an hour via `Cache-Control`), report downloads
  (`no-store`).
* **Limitations:** the cache lives in the backend process (lost on restart, not shared between processes).
  The interface is two methods (`get_or_compute`, `clear`), so a shared cache such as Redis could replace it
  later without touching the routes. Redis is not needed for one process.
* The biggest remaining transfer cost is the size of the inspection list; the nginx configuration compresses
  JSON (gzip) when the proxy is used.

---

## 5. Load balancing – readiness

**The backend is not stateless and must run as exactly one process** (one uvicorn worker). Running several
instances (or `--workers > 1`) today would cause:

| State | Where it lives | Problem with several instances |
|---|---|---|
| Sessions | memory (`AuthService._sessions`) | a user signed in on instance A is signed out on B |
| Sign-in lockout | memory | an attacker gets a fresh failure count per instance |
| Rate-limit counters | memory | each instance allows the full limit |
| Cache | memory | harmless (exact invalidation), but each instance must notice reloads |
| Circuit breaker | memory | each instance probes the AI provider separately |
| "Analysis in progress" guard | memory | the same pair could be sent to the AI twice |
| JSON stores (assessments, imports, accounts, audit log) | files, guarded by **in-process** locks | concurrent writers in different processes are not coordinated: lost updates, audit hash chain breaks |
| Inspection data reload after an import | each process's memory | other instances keep showing the old data |

**Future architecture (only if real demand appears):**

```
            nginx (upstream with several servers, least_conn)
              |                 |                 |
         FastAPI #1        FastAPI #2        FastAPI #3
              \                 |                 /
               +---- shared Redis: sessions, lockout, rate limits, cache, breaker, in-flight guard
               +---- a database (or a single writer service) for assessments, imports, accounts, audit
               +---- shared read-only storage for photographs and registers
```

Steps, in order: move sessions/lockout/rate limits/breaker state to Redis (the interfaces in `app/infra/` and
`AuthService` are small enough); move the write-side JSON stores behind a database or a single writer; then
add servers to the nginx `upstream` block. Until then the nginx `upstream` has one server. The storage system
was deliberately **not** migrated now.

---

## 6. Reverse proxy

File: `dashboard/deploy/nginx/synergy-vessel.conf` (self-contained nginx configuration).

* Listens on **127.0.0.1:8088 only** – nothing is exposed to the network. No domain, no certificate.
* `/api/*` → FastAPI at 127.0.0.1:8000 (keep-alive upstream) with `Host`, `X-Real-IP`, `X-Forwarded-For`,
  `X-Forwarded-Proto` and `X-Request-ID` headers. The backend's `/docs` and `/openapi.json` are not exposed.
* Everything else → the built React app (`dashboard/frontend/dist`); client-side routes fall back to
  `index.html` (never cached); hashed files under `/assets/` are cached for a year.
* Request size: 2 MB by default; 64 MB for `/api/imports` (the backend accepts workbooks up to 60 MB).
* Timeouts (aligned with the frontend's own): 60 s by default, 360 s for AI analysis, 330 s for imports,
  150 s for reports; 5 s to connect.
* gzip for JSON/JS/CSS/CSV, `server_tokens off`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`.
* HTTPS: a commented template at the end of the file for later.

**Running it locally (Windows, nginx installed separately):**

```powershell
cd "E:\Synergy Vessel\dashboard\frontend"; npm run build
# start the backend as usual (OPERATOR_GUIDE.md §3, including --no-proxy-headers), with the proxy trusted:
$env:SYNERGY_TRUSTED_PROXIES = "127.0.0.1"
cd "E:\Synergy Vessel\dashboard\backend"; .venv\Scripts\python -m uvicorn app.main:app --port 8000 --no-proxy-headers
cd "E:\Synergy Vessel\dashboard\deploy\nginx"; mkdir logs, temp -ErrorAction SilentlyContinue
nginx -p "E:/Synergy Vessel/dashboard/deploy/nginx/" -c synergy-vessel.conf -t    # check
nginx -p "E:/Synergy Vessel/dashboard/deploy/nginx/" -c synergy-vessel.conf       # start
# open http://127.0.0.1:8088 ; stop with:  nginx -p "..." -c synergy-vessel.conf -s quit
```

Port 8088 was chosen because 8080 is already used on this computer by an unrelated program (fix M2,
2026-11-23). The Vite dev server (port 5173) keeps working exactly as before; the proxy is optional. Alternatively, with
Docker Desktop running, `docker run --rm -v "<path>:/etc/nginx/nginx.conf:ro" nginx nginx -t` checks the
syntax (this downloads the official nginx image).

---

## 7. API gateway

**No separate gateway service was introduced** – it would duplicate what already exists:

| Responsibility | Provided by |
|---|---|
| Single entry point, request routing | nginx (`/api/` → FastAPI, `/` → frontend) |
| Request size limits, timeouts, compression, security headers | nginx |
| Request ID and access log | nginx (`X-Request-ID`, `rid=` in `logs/access.log`) |
| Rate limiting | FastAPI middleware (per user when signed in – nginx cannot see users) |
| Authentication and roles | FastAPI dependencies (`RequireUser`, `RequireAdmin`), unchanged |
| Request validation | FastAPI / Pydantic models, unchanged |
| Error handling | FastAPI exception handlers (`detail` + `code`, no internal details) |
| Audit | `AuthService.audit`, unchanged |
| API versioning readiness | all routes share the `/api/` prefix; a `/api/v2/` could be routed separately in nginx |

Rate limiting is deliberately in FastAPI only (not also in nginx) so limits are not enforced twice.

---

## 8. CI/CD

File: `.github/workflows/ci.yml` (at the project root, next to `dashboard/` and `automation/`).

| Job | Steps |
|---|---|
| Backend | checkout → Python 3.11 (pip cache) → `pip install --require-hashes -r requirements-dev.txt` → `pip check` → byte-compile + import the app → `pytest` |
| Frontend | checkout → Node.js 24 (npm cache) → `npm ci` → `npm test` (Vitest) → `npm run build` |
| Dependency audit (SV-07) | `npm audit --omit=dev --audit-level=high` (fails on high/critical in shipped packages) → `npm audit` of build/test tools (warning only) → `pip-audit` of the backend and automation locks (fails on any known vulnerability) |

* SV-07: every action is pinned to a full commit SHA (version in a comment), checkout does not keep the token
  (`persist-credentials: false`), a weekly scheduled run picks up new advisories, and `.github/dependabot.yml`
  opens grouped weekly pull requests for npm, pip (backend, automation) and GitHub Actions (majors separately).

* Runs on every push, pull request and manually; read-only permissions; a newer run cancels an older one.
* **No secrets, no AI:** `SYNERGY_AI_PROVIDER=disabled`, `SYNERGY_AI_ALLOW_LIVE=0` (and the same for
  Location AI). Backend tests use the mock provider and fakes; frontend tests mock every API call. Tests that
  need the real inspection data skip themselves when it is absent (verified on a code-only copy: 877 passed,
  6 skipped).
* **No deployment** job or step exists.
* No linter is configured in the project, so the backend "validation" step is compile + import.

### Known dependency advisories and limitations (SV-07, 2026-11-24)

SV-07 removed every advisory that had a fix within the current major versions (axios, postcss,
@remix-run/router, earlier vite issues; pip and setuptools in the backend environment). These remain and are
accepted until a planned major upgrade:

| Package (version) | Advisories | Severity | Exposure |
|---|---|---|---|
| react-router / react-router-dom 6.30.6 (shipped to the browser) | GHSA-wrjc-x8rr-h8h6 (backslash open redirect in `Link`/`useNavigate`), GHSA-337j-9hxr-rhxg (server-side-rendering hydration) | moderate | Low: the app does not build links from user-supplied text and does not use server-side rendering. Fixed only in v7 (major upgrade). CI tolerates them (`--audit-level=high`). |
| vite 5.4.21, esbuild 0.21.5, vitest 2.1.9, vite-node, tinypool, @vitest/mocker (build/test tools only) | e.g. GHSA-587h-7wmj-h5pm (vite), GHSA-67mh-4wv8-2f99 (esbuild), GHSA-5xrq-8626-4rwp (vitest) | up to critical | None in production: they affect the development and test servers only, and production is served by nginx from `frontend/dist`. Never use `npm run dev` to serve users. Fixed only in new major versions. CI reports them as a warning. |

* **esbuild install script:** npm 11 reports `esbuild@0.21.5 (postinstall: node install.js)` as not yet approved
  (`npm approve-scripts`). Tests and builds work without it; approving it is a separate decision.
* **HTTP on localhost only:** nginx listens on 127.0.0.1:8088 over plain HTTP. Before the application is reachable
  from any other computer, follow the HTTPS requirements in `deploy/nginx/MIGRATION_SV07_PHASE1.md`.

**After `git init` (when you decide to):**
1. Make sure `.env` files, `dashboard/data/`, the week folders, photographs, backups and `node_modules/` are in
   `.gitignore` (`automation/.gitignore` already excludes `automation/.env`; a root `.gitignore` is still needed for the rest).
2. Push to GitHub; the workflow starts automatically. If the repository root is `dashboard/` rather than the
   project folder, move the workflow file and drop the `dashboard/` prefix from its paths (noted in the file).

---

## 9. Service discovery

**Not required.** There are two services, both on one machine at fixed local addresses, plus an optional
external AI provider reached through its SDK:

| Service | Address configured by |
|---|---|
| Frontend → backend (development) | `VITE_API_PROXY_TARGET` (default `http://127.0.0.1:8000`) |
| Frontend → backend (other origin) | `VITE_API_BASE_URL` |
| Proxy → backend | `upstream synergy_api` in the nginx file |
| Backend ← browser origins | `SYNERGY_CORS_ORIGINS` (new; previously fixed in code) |
| Backend → AI provider | the provider SDK's own endpoint; enabled only by `SYNERGY_AI_PROVIDER` + `SYNERGY_AI_ALLOW_LIVE` |

Kubernetes, Consul or Eureka would add operational cost with nothing to discover. If the application is ever
split into independently deployed services, the first step is to keep every address in an environment
variable (as above) and let the reverse proxy route by path; a registry only becomes useful once instances
come and go dynamically.

---

## 10. Circuit breaker (AI condition analysis)

Wraps the call to the condition-analysis provider (Gemini, Groq or mock) in `AnalysisService.compare`.

| State | Behaviour |
|---|---|
| **Closed** | Calls go through. Consecutive provider failures are counted; a success resets the count. |
| **Open** | After `SYNERGY_AI_BREAKER_THRESHOLD` (default **3**) consecutive failures. Requests are refused **without contacting the provider** for `SYNERGY_AI_BREAKER_RECOVERY_SECONDS` (default **60 s**), or longer if the provider asked to wait (e.g. quota retry-after). |
| **Half-open** | After the wait, exactly **one** trial request goes through (others are asked to retry shortly). Success → closed; failure → open again for another full wait. |

* **Counts as a failure:** provider unavailable (503), timeout, rate limit, quota exhausted, rejected request,
  any other provider error or unexpected crash. **Not** a failure: a response that fails validation (the
  provider answered; it is reported as `invalid_ai_response` as before).
* **Timeouts** are enforced by the provider's own HTTP client (Gemini: `request_timeout_seconds` in
  `automation/config/settings.yaml`, with its existing 5xx retries; Groq: 60 s) and count as failures.
* **When open:** HTTP 503, `code: ai_circuit_open`, `Retry-After`, and the message "The AI service failed
  several times in a row, so analysis is paused for about N seconds. No request was sent and no result was
  saved." The user can simply retry later. No fake or downgraded result is ever produced, nothing is saved,
  and all existing assessments, reviews, history and reports keep working.
* `GET /api/ai-analysis/status` includes `circuit: {state, consecutive_failures, failure_threshold,
  retry_after_seconds}` for diagnosis.
* State is per process (see §5). The Excel Location Intelligence AI (disabled; its configuration is unchanged)
  is not wrapped.

---

## 11. Tests

All offline (mock provider, fake clocks, static file checks):

* `dashboard/backend/tests/test_infrastructure.py` – 37 tests: request categories, sliding window and recovery,
  per-address sign-in limit with 429 + `Retry-After` + CORS, per-user AI limit, browsing not restricted,
  switch-off, environment parsing; cache hits / expiry / bounds / ownership, invalidation on reload and on
  review; circuit breaker state machine, end-to-end pause without provider calls or saved records, recovery,
  existing records still usable, invalid answers not counted; nginx configuration (local-only, routing, limits
  aligned with the app and frontend); CI workflow (stages, no secrets, no AI, no deployment).
* `dashboard/frontend/src/test/api.test.js` – friendly messages for 429 and `ai_circuit_open`.
