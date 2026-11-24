# SV-07 — production cutover to nginx + built frontend

**Status (2026-11-24): COMPLETED with approval.** nginx serves production on 127.0.0.1:8088; the backend was
restarted with `--no-proxy-headers` and `SYNERGY_TRUSTED_PROXIES=127.0.0.1` (4.9 s downtime); Vite stopped;
`npm ci` + tests (330/330) + build done (byte-identical to the served `dist`). The steps below are kept as the
reference procedure and for rollback.

| | Before | After |
|---|---|---|
| Browser opens | `http://localhost:5173` (Vite **development** server) | `http://127.0.0.1:8088` (nginx) |
| Frontend | Vite dev server serving `src/` live (hot reload) | static files from `dashboard/frontend/dist` |
| `/api/*` | Vite dev proxy → `127.0.0.1:8000` | nginx → `127.0.0.1:8000` |
| Backend start | `uvicorn app.main:app --port 8000` (**no** `--no-proxy-headers`) | same **plus** `--no-proxy-headers` and `SYNERGY_TRUSTED_PROXIES=127.0.0.1` |

nginx for Windows 1.30.8: `C:\tools\nginx-1.30.8` (official nginx.org zip, PGP signature verified).

Already done during preparation: the production build (`dashboard/frontend/dist`, updated dependencies),
`logs`/`temp` folders, `nginx -t` on this configuration (syntax ok, test successful).

## Downtime and sessions

* Step 1 runs **beside** the current servers: no downtime.
* Step 2 (backend restart): about **5–15 seconds** without the API. Sessions are held in memory, so **every
  signed-in user is signed out** and must sign in again. Do it when no Excel import or AI analysis is running.
* Steps 4–5 have no effect on users of port 8088.

## 1. Start nginx (new PowerShell window; no downtime)

```powershell
cd "E:\Synergy Vessel\dashboard\deploy\nginx"
& "C:\tools\nginx-1.30.8\nginx.exe" -p "E:/Synergy Vessel/dashboard/deploy/nginx/" -c synergy-vessel.conf -t
& "C:\tools\nginx-1.30.8\nginx.exe" -p "E:/Synergy Vessel/dashboard/deploy/nginx/" -c synergy-vessel.conf
```

Quick check (no sign-in): `curl.exe -s http://127.0.0.1:8088/api/health` → `{"status":"ok"}`.

## 2. Restart the backend with the SV-04 proxy settings (5–15 s downtime, everyone signed out)

In the **backend's own window** (so the AI settings already set there are kept) press **Ctrl+C**, then:

```powershell
cd "E:\Synergy Vessel\dashboard\backend"
$env:SYNERGY_TRUSTED_PROXIES = "127.0.0.1"
.venv\Scripts\python -m uvicorn app.main:app --port 8000 --no-proxy-headers
```

If a new window is used instead, first set the same `SYNERGY_AI_*` / `SYNERGY_LOCATION_AI_*` variables as before
(OPERATOR_GUIDE.md, "AI provider"); without them AI analysis shows "not available", which is safe.

## 3. Verify

```powershell
curl.exe -s http://127.0.0.1:8088/api/health                                   # {"status":"ok"}
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8088/api/nope          # 404 (JSON, not the app page)
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8088/.env              # 404
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8088/api/inspections   # 401 (sign-in required)
curl.exe -s -I http://127.0.0.1:8088/compare | Select-String "200|Content-Security-Policy|Cache-Control"
```

Browser checklist at `http://127.0.0.1:8088`:

- [ ] Sign in works; the menu matches the role (admin: + Excel Import, Administration, Settings).
- [ ] Dashboard: **1,078** inspection appearances, **609** unique photographs, 11 inspection weeks.
- [ ] Compare Photos: photographs load; **"Analyse automatically" is OFF**; no analysis starts by itself.
- [ ] Reviews: existing assessments listed (the same number as before the migration, including approved ones).
- [ ] Refresh on `/reviews` and on a `/compare?location=…` address: page reloads, still signed in.
- [ ] Reports and Search open.
- [ ] Do **not** turn on automatic analysis or press Analyse Again just to test (that calls the live AI).

## 4. Retire the development server from production use

When step 3 is good: Ctrl+C in the Vite window (`npm run dev`, port 5173). Keep the folder and `npm run dev`
for development only.

## 5. Finish the frontend dependency update (needs step 4: the dev server locks node_modules)

```powershell
cd "E:\Synergy Vessel\dashboard\frontend"
npm ci                      # installs exactly package-lock.json (axios 1.21.1, react-router-dom 6.30.6, vite 5.4.21 ...)
# vite.config.js: remove the unused `charts: ['recharts']` chunk (prepared copy: see SV-07 report)
npm test
npm run build               # nginx serves the new files immediately; index.html is never cached
```

From now on frontend source edits are **not** live until `npm run build` is run again.

## Rollback (any time)

1. Stop nginx:
   ```powershell
   & "C:\tools\nginx-1.30.8\nginx.exe" -p "E:/Synergy Vessel/dashboard/deploy/nginx/" -c synergy-vessel.conf -s quit
   ```
2. If the Vite dev server was stopped: `cd "E:\Synergy Vessel\dashboard\frontend"; npm run dev`, and use
   `http://localhost:5173` as before. (If step 5 was done, the dev server runs on the updated packages; to go back
   to the old ones as well: restore package.json / package-lock.json from Git, then `npm ci`.)
3. The backend may keep `--no-proxy-headers` and `SYNERGY_TRUSTED_PROXIES=127.0.0.1` — both are correct with the
   Vite dev proxy too (OPERATOR_GUIDE.md §3 already requires `--no-proxy-headers`). No data, accounts,
   assessments or audit entries are touched by this migration.

## If this application is ever reachable from other computers (HTTPS requirements)

This setup is **localhost-only** and plain HTTP on purpose. Before exposing it beyond this computer:

1. Serve it only over **HTTPS**: enable the commented `listen 443 ssl` server block with a real certificate and
   key (never commit them; `*.pem`/`*.key` are git-ignored), TLS 1.2/1.3 only, and redirect port 80 to 443.
2. Add `Strict-Transport-Security` (already in the template) once HTTPS works.
3. Set `SYNERGY_COOKIE_SECURE=1` so the photo cookie is sent only over HTTPS (the backend also does this
   automatically when it sees an HTTPS request).
4. Change `listen` from 127.0.0.1 to the specific interface needed — never `0.0.0.0` without a firewall rule —
   and keep `SYNERGY_TRUSTED_PROXIES` limited to nginx's own address.
5. Consider rate limiting at nginx as well (`limit_req`) and an access-control layer (VPN or IP allow-list).

## Files

* `synergy-vessel.conf` — server, routing, limits, caching.
* `security_headers.conf` — security headers (included by every location; nginx does not inherit `add_header`).
* `proxy_api.conf` — forwarding to FastAPI (client address, no caching, status codes passed through).
