/**
 * Pure helpers for the historical comparison page. Visual comparison only:
 * nothing here judges condition, and no two photographs are assumed to show
 * the same physical area.
 */
import { UNDATED, groupByDate } from './searchResults'

export const COMPARE_MODES = ['side', 'slider', 'single']
/** Location matching on the comparison page: 'exact' (Official Location only, the default) or 'contains'. */
export const MATCH_MODES = ['exact', 'contains']

/** Display-only key for comparing two location names (case and spacing ignored). Filtering itself is server-side. */
export const locationKey = (s) => (typeof s === 'string' ? s.split(/\s+/).filter(Boolean).join(' ').toLowerCase() : '')

/** The record's Official Location, or null when it has none. */
export const officialOf = (r) => (r?.official_location_labelled && r.official_location ? r.official_location : null)

/** Distinct Official Locations among items, most photographs first ({ location: null } = no Official Location). */
export function officialLocations(items) {
  const counts = new Map()
  for (const r of items ?? []) {
    const loc = officialOf(r)
    const k = loc === null ? null : locationKey(loc)
    const cur = counts.get(k) ?? { location: loc, count: 0 }
    cur.count += 1
    counts.set(k, cur)
  }
  return [...counts.values()].sort((a, b) => b.count - a.count || String(a.location).localeCompare(String(b.location)))
}

/**
 * Comparison link from an Inspection Search (always a broad search: Official + AI locations, partial match).
 * Exact mode only when every result's Official Location IS the searched name, so the exact comparison shows the same
 * records; otherwise Contains, carrying the Official/AI source filter (which only exists in Contains mode).
 */
export function compareHref(query, items, { source = 'all' } = {}) {
  const want = locationKey(query)
  const exact = Boolean(want) && items?.length > 0 && items.every((r) => locationKey(officialOf(r)) === want)
  const p = new URLSearchParams({ location: query })
  if (!exact) {
    p.set('match', 'contains')
    if (source && source !== 'all') p.set('source', source)
  }
  return `/compare?${p.toString()}`
}

/** True when the two selected photographs do not share the same Official Location. */
export const differentLocations = (a, b) => Boolean(a && b) && locationKey(officialOf(a)) !== locationKey(officialOf(b))
export const ZOOM_MIN = 1
export const ZOOM_MAX = 4
export const ZOOM_STEP = 0.5

/** Dated groups in chronological order (undated records cannot be compared by date). */
export function chronologicalGroups(items) {
  return groupByDate(items, 'oldest').filter((g) => g.date !== UNDATED)
}

/** Default pair: the two most recent dates, or nulls when fewer than two exist. */
export function defaultDates(dates) {
  if (!Array.isArray(dates) || dates.length < 2) return { earlier: null, later: null }
  return { earlier: dates.at(-2), later: dates.at(-1) }
}

/**
 * Keep a requested pair valid: both must exist and earlier < later (which also rules
 * out the same date on both sides). Invalid parts fall back to the defaults.
 */
export function resolveDates(dates, earlier, later) {
  const has = (d) => Boolean(d) && dates.includes(d)
  const def = defaultDates(dates)
  let e = has(earlier) ? earlier : null
  let l = has(later) ? later : null
  if (e && l && e >= l) l = null
  if (!e && !l) return def
  if (!l) l = dates.find((d) => d > e) ?? null
  if (!e) e = [...dates].reverse().find((d) => d < l) ?? null
  if (!e || !l) return def
  return { earlier: e, later: l }
}

/** Whole days between two ISO dates (later - earlier). */
export function daysBetween(earlier, later) {
  const toUtc = (iso) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? '')
    return m ? Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null
  }
  const a = toUtc(earlier)
  const b = toUtc(later)
  return a === null || b === null ? null : Math.round((b - a) / 86400000)
}

export function describeInterval(days) {
  if (days === null || days === undefined) return '—'
  const weeks = Math.floor(days / 7)
  const rest = days % 7
  const w = weeks ? `${weeks} ${weeks === 1 ? 'week' : 'weeks'}` : ''
  const d = rest ? `${rest} ${rest === 1 ? 'day' : 'days'}` : ''
  return `${days} ${days === 1 ? 'day' : 'days'}${weeks ? ` (${[w, d].filter(Boolean).join(' ')})` : ''}`
}

export const clampZoom = (z) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(z * 2) / 2))

/** Shorten a SHA-256 for display; the full value stays available as a tooltip. */
export const shortSha = (sha) => (typeof sha === 'string' && sha.length > 16 ? `${sha.slice(0, 12)}…${sha.slice(-4)}` : sha ?? null)
