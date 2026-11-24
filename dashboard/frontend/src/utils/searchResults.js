/**
 * Pure helpers over GET /api/inspections/search items (each item carries `matched_on`).
 * Nothing is merged or de-duplicated: every inspection appearance (record_id) is kept.
 */

export const SOURCE_FILTERS = ['all', 'official', 'ai', 'both']
export const SORTS = ['newest', 'oldest']
export const UNDATED = 'undated'

/** 'official' | 'ai' | 'both' | 'none' from the backend's matched_on list. */
export function matchSource(item) {
  const m = item?.matched_on ?? []
  const off = m.includes('official_location')
  const ai = m.includes('ai_suggested_location')
  if (off && ai) return 'both'
  if (off) return 'official'
  if (ai) return 'ai'
  return 'none'
}

export const isDerivedDate = (item) => item?.date_source === 'worksheet_name'

export function summariseSearch(items) {
  const list = Array.isArray(items) ? items : []
  const shas = new Set()
  let shaMissing = 0
  const dates = new Set()
  const weeks = new Map()
  const bySource = { official: 0, ai: 0, both: 0 }
  for (const r of list) {
    const sha = r?.extracted_photograph?.sha256
    if (sha) shas.add(sha)
    else shaMissing += 1
    if (r?.inspection_date) dates.add(r.inspection_date)
    if (r?.week && !weeks.has(r.week)) weeks.set(r.week, r.week_number ?? null)
    const s = matchSource(r)
    if (s in bySource) bySource[s] += 1
  }
  return {
    appearances: list.length,
    uniquePhotographs: shaMissing === 0 ? shas.size : null,
    dates: [...dates].sort(),
    weeks: [...weeks.entries()]
      .sort((a, b) => (a[1] ?? Infinity) - (b[1] ?? Infinity) || a[0].localeCompare(b[0]))
      .map(([week]) => week),
    bySource,
    missingImages: list.filter((r) => r?.image_status === 'missing' || !r?.image_url).length,
  }
}

/** Apply week and match-source filters (date selection is handled by grouping). */
export function filterItems(items, { week = '', source = 'all' } = {}) {
  if (!Array.isArray(items)) return []
  return items.filter((r) => {
    if (week && r.week !== week) return false
    if (source === 'all') return true
    const s = matchSource(r)
    if (source === 'both') return s === 'both'
    if (source === 'official') return s === 'official' || s === 'both'
    if (source === 'ai') return s === 'ai' || s === 'both'
    return true
  })
}

/**
 * Group by inspection_date. Groups are ordered by date (newest|oldest first); an
 * 'undated' group, if any, always goes last. Item order inside a group is preserved.
 */
export function groupByDate(items, sort = 'newest') {
  const groups = new Map()
  for (const r of items ?? []) {
    const key = r?.inspection_date || UNDATED
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(r)
  }
  const dir = sort === 'oldest' ? 1 : -1
  return [...groups.entries()]
    .sort(([a], [b]) => {
      if (a === UNDATED) return 1
      if (b === UNDATED) return -1
      return a.localeCompare(b) * dir
    })
    .map(([date, list]) => ({
      date,
      items: list,
      count: list.length,
      weeks: [...new Set(list.map((r) => r.week))],
      derived: list.some(isDerivedDate),
      allDerived: list.every(isDerivedDate),
      reportDates: [...new Set(list.map((r) => r.report_date).filter(Boolean))],
      unavailable: list.filter((r) => r.image_status === 'missing' || !r.image_url).length,
    }))
}

/** Items shown in the gallery: one date's items, or every group flattened in order. */
export function visibleItems(groups, date) {
  if (date) return groups.find((g) => g.date === date)?.items ?? []
  return groups.flatMap((g) => g.items)
}

/** Neighbouring dates of `date` in chronological order: { older, newer }. */
export function adjacentDates(groups, date) {
  const dated = groups.map((g) => g.date).filter((d) => d !== UNDATED).sort()
  const i = dated.indexOf(date)
  if (i === -1) return { older: null, newer: null }
  return { older: dated[i - 1] ?? null, newer: dated[i + 1] ?? null }
}
