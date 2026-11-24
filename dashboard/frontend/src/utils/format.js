const numberFmt = new Intl.NumberFormat('en-GB')

export const formatNumber = (n) => (typeof n === 'number' && Number.isFinite(n) ? numberFmt.format(n) : '—')

/** Parse an ISO 'YYYY-MM-DD' as a local calendar date (no timezone shift). */
export function parseIsoDate(iso) {
  if (typeof iso !== 'string') return null
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso)
  if (!m) return null
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
}

export function formatDate(iso, opts = { day: '2-digit', month: 'short', year: 'numeric' }) {
  const d = iso instanceof Date ? iso : parseIsoDate(iso)
  return d ? d.toLocaleDateString('en-GB', opts) : '—'
}

export const formatShortDate = (iso) => formatDate(iso, { day: '2-digit', month: 'short' })

export const formatToday = (now = new Date()) =>
  now.toLocaleDateString('en-GB', { weekday: 'long', day: '2-digit', month: 'long', year: 'numeric' })

/** 'week 31' / 'Week 30' -> 'Week 31' (display only; the API value is unchanged). */
export const formatWeekLabel = (week) =>
  typeof week === 'string' ? week.replace(/^\s*week\b/i, 'Week').trim() : '—'

/** Display-only tidy of a file or sheet name: runs of whitespace become one space, ends trimmed. The stored name is
    never changed (show the original in a tooltip). Returns null for a missing or blank name. */
export const formatFilename = (name) => (typeof name === 'string' ? name.replace(/\s+/g, ' ').trim() || null : null)
