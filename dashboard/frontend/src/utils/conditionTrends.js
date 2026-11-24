/**
 * Condition Trends: pure summaries of saved AI condition analyses (GET /api/ai-analysis/history items).
 *
 * Only stored fields are used. The analysis schema has no severity; the outcome is a classification
 * (DETERIORATED / AMELIORATED / STABLE / NEEDS_REVIEW) produced by the AI and adjusted by validation rules.
 * An AI classification counts as human-confirmed only when a reviewer has APPROVED the analysis.
 */
import { locationKey } from './compare'

export const REVIEW_ORDER = ['PENDING_REVIEW', 'APPROVED', 'REJECTED', 'NEEDS_REASSESSMENT']
export const CLASSIFICATION_ORDER = ['DETERIORATED', 'AMELIORATED', 'STABLE', 'NEEDS_REVIEW']
export const CHANGE_CLASSIFICATIONS = ['DETERIORATED', 'AMELIORATED']
export const NO_LOCATION = 'No Official Location'

export const reviewStatusOf = (a) => a?.review?.status ?? 'PENDING_REVIEW'
export const isApproved = (a) => reviewStatusOf(a) === 'APPROVED'
/** A condition change confirmed by a person: an approved analysis whose classification is a change. */
export const isConfirmedChange = (a) => isApproved(a) && CHANGE_CLASSIFICATIONS.includes(a.classification)
export const isReviewed = (a) => ['APPROVED', 'REJECTED'].includes(reviewStatusOf(a))

/** The latest human review event, if any (reviewer name, role, time, note). */
export const lastReview = (a) => {
  const h = a?.review?.history
  return Array.isArray(h) && h.length ? h[h.length - 1] : null
}

/** The Official Location of the pair: shared value, the one present, or "earlier → later" when they differ. */
export function analysisLocation(a) {
  const e = a?.earlier?.official_location?.trim() || ''
  const l = a?.later?.official_location?.trim() || ''
  if (e && l) return locationKey(e) === locationKey(l) ? l : `${e} → ${l}`
  return e || l || NO_LOCATION
}

const count = (items, keyOf, keys) => {
  const out = Object.fromEntries(keys.map((k) => [k, 0]))
  for (const a of items) {
    const k = keyOf(a)
    if (k in out) out[k] += 1
  }
  return out
}

export const reviewDistribution = (items) => count(items, reviewStatusOf, REVIEW_ORDER)
export const classificationDistribution = (items) => count(items, (a) => a.classification, CLASSIFICATION_ORDER)

export function summarise(items) {
  const review = reviewDistribution(items)
  const confirmed = items.filter(isConfirmedChange)
  return {
    total: items.length,
    pending: review.PENDING_REVIEW,
    reviewed: review.APPROVED + review.REJECTED,
    approved: review.APPROVED,
    rejected: review.REJECTED,
    reassessment: review.NEEDS_REASSESSMENT,
    confirmedChanges: confirmed.length,
    confirmedDeteriorated: confirmed.filter((a) => a.classification === 'DETERIORATED').length,
    confirmedAmeliorated: confirmed.filter((a) => a.classification === 'AMELIORATED').length,
  }
}

/** Per Official Location: counts, review progress, AI findings and human-approved findings. */
export function byLocation(items) {
  const groups = new Map()
  for (const a of items) {
    const name = analysisLocation(a)
    if (!groups.has(name)) groups.set(name, [])
    groups.get(name).push(a)
  }
  return [...groups.entries()]
    .map(([location, list]) => ({
      location,
      ...summarise(list),
      ai: classificationDistribution(list),
      human: classificationDistribution(list.filter(isApproved)),
      lastAnalysed: list.map((a) => a.created_at).sort().at(-1) ?? null,
    }))
    .sort((x, y) => y.total - x.total || x.location.localeCompare(y.location))
}

/** Per later inspection week (in date order): AI classifications and how many of them a person approved. */
export function byLaterWeek(items) {
  const groups = new Map()
  for (const a of items) {
    const key = a.later?.week || a.later?.inspection_date || 'Unknown'
    const g = groups.get(key) ?? { week: key, date: a.later?.inspection_date ?? '', items: [] }
    if (a.later?.inspection_date && a.later.inspection_date < g.date) g.date = a.later.inspection_date
    g.items.push(a)
    groups.set(key, g)
  }
  return [...groups.values()]
    .sort((x, y) => String(x.date).localeCompare(String(y.date)))
    .map((g) => ({ week: g.week, date: g.date, total: g.items.length, ai: classificationDistribution(g.items), approved: g.items.filter(isApproved).length }))
}

const norm = (s) => String(s ?? '').replace(/\s+/g, ' ').trim().toLowerCase()

/**
 * Filters (all combine): vessel id, Official Location (exact, as grouped), date range (either inspection date in
 * range, the same rule as the history API), review status, free-text search, and whether simulated (mock)
 * results are included.
 */
export function filterAnalyses(items, { vessel, location, from, to, review, q, includeSimulated = false } = {}) {
  const needle = norm(q)
  const inRange = (d) => Boolean(d) && (!from || d >= from) && (!to || d <= to)
  return items.filter((a) => {
    if (!includeSimulated && a.simulated) return false
    if (vessel && a.vessel_id !== vessel) return false
    if (location && analysisLocation(a) !== location) return false
    if ((from || to) && !inRange(a.earlier?.inspection_date) && !inRange(a.later?.inspection_date)) return false
    if (review && reviewStatusOf(a) !== review) return false
    if (needle) {
      const hay = norm([a.analysis_id, a.vessel_name, a.location, analysisLocation(a), a.earlier?.image_id, a.later?.image_id,
        a.earlier?.record_id, a.later?.record_id, a.earlier?.ai_suggested_location, a.later?.ai_suggested_location].filter(Boolean).join(' '))
      if (!hay.includes(needle)) return false
    }
    return true
  })
}

export const newestFirst = (items) => [...items].sort((x, y) => String(y.created_at).localeCompare(String(x.created_at)))
