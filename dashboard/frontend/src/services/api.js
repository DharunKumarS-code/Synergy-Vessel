import axios from 'axios'

/**
 * Central client for the Synergy Vessel FastAPI backend (dashboard/backend).
 * In development, Vite proxies /api to 127.0.0.1:8000 (see vite.config.js).
 * VITE_API_BASE_URL can point at another origin when not using the proxy.
 */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || ''

export const http = axios.create({
  baseURL: API_BASE_URL,
  timeout: 20000,
  headers: { Accept: 'application/json' },
})

/** Friendly error that never carries raw backend details to the UI. */
export class ApiError extends Error {
  constructor(kind, message, status = null) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind // 'offline' | 'timeout' | 'not_found' | 'bad_request' | 'unauthenticated' | 'forbidden' | 'server' | 'unknown'
    this.status = status
  }
}

export function toApiError(err) {
  if (err instanceof ApiError) return err
  if (axios.isCancel?.(err) || err?.code === 'ERR_CANCELED') {
    return new ApiError('cancelled', 'Request cancelled.')
  }
  if (err?.code === 'ECONNABORTED' || err?.code === 'ETIMEDOUT') {
    return new ApiError('timeout', 'The inspection service took too long to respond.')
  }
  const status = err?.response?.status
  if (!status) {
    return new ApiError('offline', 'Unable to reach the inspection service.')
  }
  // SV-02: data needs a signed-in account. A 401 means the session has ended (the app returns to the sign-in
  // screen); a 403 means the account's role does not allow it. Neither is a "something went wrong" failure.
  if (status === 401) return new ApiError('unauthenticated', 'Your session has ended. Please sign in again.', status)
  if (status === 403) return new ApiError('forbidden', 'Your account does not have permission for this.', status)
  if (status === 404) return new ApiError('not_found', 'The requested information was not found.', status)
  if (status === 429) return new ApiError('too_many_requests', 'Too many requests. Please wait a moment and try again.', status)
  if (status === 400 || status === 422) {
    return new ApiError('bad_request', 'The request could not be processed.', status)
  }
  if (status === 502 || status === 503 || status === 504) {
    return new ApiError('offline', 'The inspection service is currently unavailable.', status)
  }
  if (status >= 500) return new ApiError('server', 'The inspection service encountered a problem.', status)
  return new ApiError('unknown', 'Something went wrong while loading data.', status)
}

async function get(url, params, signal) {
  try {
    const res = await http.get(url, { params, signal })
    return res.data
  } catch (err) {
    throw toApiError(err)
  }
}

/** GET /api/health -> { status, weeks_loaded, records, resolved_images, loaded_at } */
export const getHealth = (signal) => get('/api/health', undefined, signal)

/** GET /api/vessels -> [{ vessel_id, vessel_name, record_count, weeks[], first_inspection_date, last_inspection_date, name_source }] */
export const getVessels = (signal) => get('/api/vessels', undefined, signal)

/** GET /api/locations -> [{ location, variants[], is_placeholder, record_count, date_count, vessel_ids[] }] */
export const getLocations = ({ vessel } = {}, signal) =>
  get('/api/locations', vessel ? { vessel } : undefined, signal)

/** GET /api/inspections -> { total, count, offset, limit, items[] } (limit max 2000) */
export const getInspections = ({ vessel, week, date, location, limit = 100, offset = 0 } = {}, signal) =>
  get('/api/inspections', clean({ vessel, week, date, location, limit, offset }), signal)

/**
 * All inspection records for a vessel, paging through /api/inspections until `total` is reached.
 */
export async function getAllInspections({ vessel } = {}, signal) {
  const pageSize = 2000
  const items = []
  let total = Infinity
  // Advance by what was actually returned, in case the server caps the page size.
  while (items.length < total) {
    const page = await getInspections({ vessel, limit: pageSize, offset: items.length }, signal)
    total = page.total
    if (!page.items?.length) break
    items.push(...page.items)
  }
  return { total: items.length, items }
}

/**
 * GET /api/inspections/search — partial, case-insensitive search across Official and
 * AI Suggested Location. -> { location, match, include_ai_location, count, items[] },
 * each item an inspection record plus `matched_on` (the field(s) that matched).
 */
export const searchInspections = ({ location, vessel, match = 'contains', includeAi = true } = {}, signal) =>
  get(
    '/api/inspections/search',
    clean({ location: location?.trim(), vessel, match, include_ai_location: includeAi ? 'true' : 'false' }),
    signal,
  )

/** URL for an image served by GET /api/images/{image_id}. */
export const imageUrl = (imageId) => `${API_BASE_URL}/api/images/${encodeURIComponent(imageId)}`

function clean(obj) {
  return Object.fromEntries(Object.entries(obj).filter(([, v]) => v !== undefined && v !== null && v !== ''))
}

/* ------------------------------------------------------------------ Phase 5: AI condition analysis */

/** Messages for the backend's AI error codes (authored here; backend text is never shown raw). */
export const AI_ERROR_MESSAGES = {
  ai_unavailable: 'AI condition analysis is currently unavailable.',
  quota_exhausted: 'The AI service usage quota has been reached. Please try again later.',
  rate_limited: 'The AI service is busy. Please wait a minute and try again.',
  ai_timeout: 'The AI service took too long to respond. No result was saved.',
  ai_circuit_open: 'The AI service failed several times in a row, so analysis is paused for a short while. No result was saved. Please try again in a minute or two.',
  too_many_requests: 'Too many analysis requests in a short time. Please wait a minute and try again.',
  invalid_ai_response: 'The AI service returned an invalid response. No result was saved.',
  ai_rejected: 'The AI service rejected the request. Please check the AI configuration.',
  same_image: 'Both selections are the same photograph file, so they cannot be compared.',
  photograph_unavailable: 'One of the selected photographs is unavailable.',
  invalid_date_order: 'The earlier photograph must come from an earlier inspection date.',
  date_mismatch: 'The selected inspection dates do not match the records.',
  undated_record: 'Both photographs need an inspection date.',
  different_vessels: 'Both photographs must belong to the same vessel.',
  analysis_in_progress: 'This pair is already being analysed.',
  already_analysed: 'This pair has already been analysed.',
  unknown_record: 'One of the selected inspection records was not found.',
  same_record: 'The same inspection record was selected twice.',
  same_inspection_date: 'Both photographs are from the same inspection date.',
  photograph_unreadable: 'One of the selected photographs is damaged or unreadable.',
  file_integrity: 'A photograph file no longer matches its recorded fingerprint.',
  validation_warnings: 'This pair has validation warnings. Read and acknowledge them before analysing.',
  unknown_analysis: 'The analysis was not found.',
  // SV-03 review rules (enforced by the server)
  invalid_review_transition: 'An assessment cannot be returned to Awaiting review.',
  review_unchanged: 'This assessment already has that review status.',
  review_already_decided: 'This assessment has already been decided. Only an administrator can correct a decision. Reload to see the latest status.',
  correction_reason_required: 'Correcting a decision requires a reason of at least 10 characters.',
  self_approval_not_allowed: 'You cannot approve an assessment that you analysed yourself. Another reviewer or an administrator must approve it.',
  storage_error: 'The result could not be saved.',
  empty_report: 'No inspection records match these filters, so there is nothing to report.',
  no_analyses: 'No saved AI analyses match these filters.',
  invalid_date_range: 'The start date must be on or before the end date.',
  unknown_week: 'The selected inspection week was not found.',
  unknown_vessel: 'The selected vessel was not found.',
  invalid_location: 'The location filter is too long.',
}

export function toAiError(err) {
  const code = err?.response?.data?.code
  if (code && AI_ERROR_MESSAGES[code]) return new ApiError(code, AI_ERROR_MESSAGES[code], err.response.status)
  const base = toApiError(err)
  if (base.kind === 'offline' && base.status) return new ApiError('ai_unavailable', AI_ERROR_MESSAGES.ai_unavailable, base.status)
  return base
}

async function post(url, body, { timeout } = {}) {
  try {
    const res = await http.post(url, body, timeout ? { timeout } : undefined)
    return res.data
  } catch (err) {
    throw toAiError(err)
  }
}

/** GET /api/ai-analysis/status -> { available, message, provider, model, simulated, live, prompt_version, ... } */
export const getAiStatus = (signal) => get('/api/ai-analysis/status', undefined, signal)

/** GET /api/ai-analysis/history -> { total, count, offset, limit, items[] } (never triggers analysis) */
export const getAnalysisHistory = (filters = {}, signal) => get('/api/ai-analysis/history', clean(filters), signal)

/** Every saved analysis matching the filters, paging through /api/ai-analysis/history (max 200 per page). */
export async function getAllAnalyses(filters = {}, signal) {
  const items = []
  let total = Infinity
  while (items.length < total) {
    const page = await getAnalysisHistory({ ...filters, limit: 200, offset: items.length, sort: 'analysed_desc' }, signal)
    total = page.total
    if (!page.items?.length) break
    items.push(...page.items)
  }
  return { total: items.length, items }
}

/** GET /api/ai-analysis/validate -> image-pair eligibility (read-only, never calls the AI) */
export const validateImagePair = (earlierRecordId, laterRecordId, signal) =>
  get('/api/ai-analysis/validate', { earlier_record_id: earlierRecordId, later_record_id: laterRecordId }, signal)

/** POST /api/ai-analysis/compare -> { reused, analysis } (may take minutes on a live provider) */
export const compareImages = (body) => post('/api/ai-analysis/compare', body, { timeout: 330000 })

/** POST /api/ai-analysis/{id}/review -> updated analysis (AI result is preserved) */
export const reviewAnalysis = (analysisId, body) =>
  post(`/api/ai-analysis/${encodeURIComponent(analysisId)}/review`, body)

/** GET /api/ai-analysis/{id} -> saved analysis */
export const getAnalysis = (analysisId, signal) => get(`/api/ai-analysis/${encodeURIComponent(analysisId)}`, undefined, signal)

/* ------------------------------------------------------------------ Phase 6: reports */

/** GET /api/reports/preview -> { vessel_name, period, summary, analyses[] } */
export const getReportPreview = (filters = {}, signal) => get('/api/reports/preview', clean(filters), signal)

export const REPORT_FILES = {
  pdf: '/api/reports/inspection.pdf',
  inspectionsCsv: '/api/reports/inspections.csv',
  analysesCsv: '/api/reports/analyses.csv',
}

/** Download a generated report as a Blob; error bodies (JSON inside a Blob) become friendly errors. */
export async function downloadReport(kind, filters = {}) {
  try {
    const res = await http.get(REPORT_FILES[kind], { params: clean(filters), responseType: 'blob', timeout: 120000 })
    const cd = res.headers?.['content-disposition'] || ''
    const filename = /filename="([^"]+)"/.exec(cd)?.[1] || `synergy-report.${kind === 'pdf' ? 'pdf' : 'csv'}`
    return { blob: res.data, filename }
  } catch (err) {
    const data = err?.response?.data
    if (typeof Blob !== 'undefined' && data instanceof Blob) {
      try {
        err.response.data = JSON.parse(await readBlobText(data))
      } catch {
        /* not JSON -- fall through to the generic message */
      }
    }
    throw toAiError(err)
  }
}

function readBlobText(blob) {
  if (typeof blob.text === 'function') return blob.text()
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(blob)
  })
}

/** Save a Blob through a temporary object URL (nothing is kept in the page). */
export function saveBlob({ blob, filename }) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/* ------------------------------------------------------------------ Phase 7: Excel import */
/** Server import errors carry { detail, code, guidance }; keep all three. */
export function toImportError(err) {
  const data = err?.response?.data
  if (data?.code && data?.detail) {
    const e = new ApiError(data.code, data.detail, err.response.status)
    e.guidance = data.guidance || ''
    return e
  }
  return toApiError(err)
}

async function upload(url, file, params) {
  try {
    const res = await http.post(url, file, {
      params: { filename: file.name, ...params },
      headers: { 'Content-Type': 'application/octet-stream' },
      timeout: 300000,
    })
    return res.data
  } catch (err) {
    throw toImportError(err)
  }
}

/** POST /api/imports/preview -> validation + per-row results. Writes nothing on the server. */
export const previewImport = (file) => upload('/api/imports/preview', file)

/** POST /api/imports -> commits exactly the previewed file (fingerprint = its SHA-256). */
export const confirmImport = (file, fingerprint) => upload('/api/imports', file, { fingerprint })

/** GET /api/imports -> { imports[], failed_attempts[], interrupted_imports, problems[] } */
export const getImportHistory = (signal) => get('/api/imports', undefined, signal)

/** GET /api/imports/{id} -> one import with its per-row results */
export const getImport = (importId, signal) => get(`/api/imports/${encodeURIComponent(importId)}`, undefined, signal)

/* ------------------------------------------------------------------ Phase 8.2: authentication */
const TOKEN_KEY = 'synergy.session'

/** The bearer token lives in sessionStorage (per browser tab, cleared when the tab closes); never logged. */
export const tokenStore = {
  get() {
    try { return sessionStorage.getItem(TOKEN_KEY) } catch { return null }
  },
  set(token) {
    try { token ? sessionStorage.setItem(TOKEN_KEY, token) : sessionStorage.removeItem(TOKEN_KEY) } catch { /* storage unavailable */ }
  },
}

export const SESSION_EXPIRED_EVENT = 'synergy:session-expired'

http.interceptors.request.use((config) => {
  const token = tokenStore.get()
  if (token) config.headers = { ...config.headers, Authorization: `Bearer ${token}` }
  return config
})

http.interceptors.response.use(undefined, (err) => {
  // A rejected session (expired, revoked, account disabled) clears the local session everywhere and returns the
  // app to the sign-in screen (SV-02: also when no token was stored, e.g. a request that raced a sign-out).
  if (err?.response?.status === 401 && err.response.data?.code === 'not_authenticated') {
    tokenStore.set(null)
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
  }
  return Promise.reject(err)
})

Object.assign(AI_ERROR_MESSAGES, {
  not_authenticated: 'Sign in to perform this action. Your session may have expired.',
  forbidden: 'Your account does not have permission for this action.',
})

/** Auth/admin errors: the server's message is already user-facing ({ detail, code }). */
export function toAuthError(err) {
  const data = err?.response?.data
  if (data?.code && typeof data.detail === 'string') return new ApiError(data.code, data.detail, err.response.status)
  return toApiError(err)
}

async function send(method, url, body) {
  try {
    const res = await http.request({ method, url, data: body })
    return res.data
  } catch (err) {
    throw toAuthError(err)
  }
}

/** POST /api/auth/login -> { token, user, idle_timeout_seconds } (stores the token) */
export async function login(username, password) {
  const res = await send('post', '/api/auth/login', { username, password })
  tokenStore.set(res.token)
  return res.user
}

/** POST /api/auth/logout -> invalidates the session on the server and clears the photo cookie (HttpOnly, so only
 * the server can remove it). Sent even without a token, so an expired session's cookie is cleared too. The local
 * token is always cleared. */
export async function logout() {
  try {
    await send('post', '/api/auth/logout')
  } catch {
    /* already signed out on the server, or offline: the local session is cleared regardless */
  } finally {
    tokenStore.set(null)
  }
}

/** GET /api/auth/session -> { authenticated, user } (public; never 401). For a live session the server also renews
 * the photo cookie (SV-02), e.g. after a reload or after another tab signed out. */
export async function getSession() {
  if (!tokenStore.get()) return { authenticated: false, user: null }
  const res = await send('get', '/api/auth/session')
  if (!res.authenticated) tokenStore.set(null)
  return res
}

/**
 * SV-02: a photo failed to load. Photos are authorised by an HttpOnly cookie that JavaScript cannot read, so ask
 * the server: a live session renews the cookie (resolves true -- retry the photo); an ended session returns the
 * app to the sign-in screen (resolves false). Concurrent failures share one check.
 */
let photoCheck = null
export function recheckPhotoAccess() {
  if (!tokenStore.get()) return Promise.resolve(false)   // no session in this tab: nothing to renew or expire
  if (!photoCheck) {
    photoCheck = getSession()
      .then((s) => {
        if (!s.authenticated) window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
        return Boolean(s.authenticated)
      })
      .catch(() => false)
      .finally(() => { setTimeout(() => { photoCheck = null }, 5000) })
  }
  return photoCheck
}

export const listUsers = () => send('get', '/api/admin/users')
export const createUser = (body) => send('post', '/api/admin/users', body)
export const updateUser = (userId, body) => send('patch', `/api/admin/users/${encodeURIComponent(userId)}`, body)
export const verifyAudit = () => send('get', '/api/admin/audit/verify')
export const getAuditEntries = (limit = 50) => send('get', `/api/admin/audit?limit=${limit}`)

/* ------------------------------------------------------------------ Phase 8.4: import rollback (admin) */
async function importAction(method, url, body) {
  try {
    const res = await http.request({ method, url, data: body })
    return res.data
  } catch (err) {
    throw toImportError(err)
  }
}
/** POST /api/imports/{id}/withdraw -> reversible rollback (records leave the dataset; files are kept) */
export const withdrawImport = (importId, reason) =>
  importAction('post', `/api/imports/${encodeURIComponent(importId)}/withdraw`, { reason, confirm_import_id: importId })
/** POST /api/imports/{id}/reinstate */
export const reinstateImport = (importId, reason) =>
  importAction('post', `/api/imports/${encodeURIComponent(importId)}/reinstate`, { reason, confirm_import_id: importId })
/** GET /api/imports/{id}/verify -> { valid, problems[], photographs_checked } */
export const verifyImport = (importId) => importAction('get', `/api/imports/${encodeURIComponent(importId)}/verify`)

/** POST /api/imports/{id}/locations -> { decision, effective_location }. action: accept | reject | keep_original | manual */
export const decideLocation = (importId, rowNumber, action, location) =>
  importAction('post', `/api/imports/${encodeURIComponent(importId)}/locations`,
    { row_number: rowNumber, action, ...(action === 'manual' || (action === 'accept' && location) ? { location } : {}) })

/* ------------------------------------------------------------------ SV-08: physical viewpoint mapping */
async function viewpointRequest(method, url, { params, data, signal } = {}) {
  try {
    const res = await http.request({ method, url, params, data, signal })
    return res.data
  } catch (err) {
    throw toAuthError(err)   // the server's { detail, code } is already user-facing
  }
}
/** GET /api/inspections/viewpoints -> { location, mapping: {available, revision, problem}, weeks[], viewpoints[], unlinked[] } */
export const getViewpoints = ({ location, vessel } = {}, signal) =>
  viewpointRequest('get', '/api/inspections/viewpoints', { params: clean({ location, vessel }), signal })
/** GET /api/inspections/viewpoints/groups -> { mapping, groups: [{ group_id, display_name, locations: [{location, viewpoint_count}] }] } */
export const getViewpointGroups = ({ vessel } = {}, signal) =>
  viewpointRequest('get', '/api/inspections/viewpoints/groups', { params: clean({ vessel }), signal })
/** GET /api/admin/viewpoints/review (administrators) */
export const getViewpointReview = (filters = {}, signal) =>
  viewpointRequest('get', '/api/admin/viewpoints/review', { params: clean(filters), signal })
/** GET /api/admin/viewpoints/history (administrators) */
export const getViewpointHistory = (filters = {}, signal) =>
  viewpointRequest('get', '/api/admin/viewpoints/history', { params: clean(filters), signal })
/** POST /api/admin/viewpoints/decisions { action, revision, viewpoint_id, record_ids?, target_viewpoint_id?, at_week?, display_name?, notes? } */
export const decideViewpoint = (body) => viewpointRequest('post', '/api/admin/viewpoints/decisions', { data: body })
