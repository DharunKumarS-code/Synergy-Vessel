/**
 * Pure summaries derived from GET /api/inspections items and GET /api/locations.
 * Nothing is estimated: a metric is null when the source data cannot support it.
 */

export function summariseInspections(items) {
  if (!Array.isArray(items)) return null
  const shas = new Set()
  let shaMissing = 0
  let resolved = 0
  let labelled = 0
  const weeks = new Map()

  for (const r of items) {
    const sha = r?.extracted_photograph?.sha256
    if (sha) shas.add(sha)
    else shaMissing += 1
    if (r?.image_status === 'resolved') resolved += 1
    if (r?.official_location_labelled) labelled += 1

    const key = r?.week ?? 'Unknown'
    if (!weeks.has(key)) {
      weeks.set(key, {
        week: key,
        weekNumber: typeof r?.week_number === 'number' ? r.week_number : null,
        dates: new Set(),
        dateSources: new Set(),
        appearances: 0,
        labelled: 0,
        unlabelled: 0,
        resolved: 0,
      })
    }
    const w = weeks.get(key)
    w.appearances += 1
    if (r?.official_location_labelled) w.labelled += 1
    else w.unlabelled += 1
    if (r?.image_status === 'resolved') w.resolved += 1
    if (r?.inspection_date) w.dates.add(r.inspection_date)
    if (r?.date_source) w.dateSources.add(r.date_source)
  }

  const weekRows = [...weeks.values()]
    .map((w) => ({
      ...w,
      dates: [...w.dates].sort(),
      dateSources: [...w.dateSources],
    }))
    .sort((a, b) => (a.weekNumber ?? Infinity) - (b.weekNumber ?? Infinity) || a.week.localeCompare(b.week))

  return {
    appearances: items.length,
    resolvedImages: resolved,
    // unique photographs only when every record carries a hash; otherwise unreliable
    uniquePhotographs: shaMissing === 0 ? shas.size : null,
    labelled,
    unlabelled: items.length - labelled,
    weeks: weekRows,
    inspectionDates: [...new Set(weekRows.flatMap((w) => w.dates))].sort(),
  }
}

export function summariseLocations(locations) {
  if (!Array.isArray(locations)) return null
  const real = locations.filter((l) => !l.is_placeholder)
  const placeholder = locations.filter((l) => l.is_placeholder)
  return {
    named: [...real].sort((a, b) => b.record_count - a.record_count || a.location.localeCompare(b.location)),
    namedCount: real.length,
    placeholderCount: placeholder.reduce((s, l) => s + l.record_count, 0),
    placeholderLabel: placeholder[0]?.location ?? null,
  }
}

/** Chart rows: one per inspection date, split by Official Location labelled/unlabelled. */
export function activityByDate(items) {
  if (!Array.isArray(items)) return []
  const byDate = new Map()
  for (const r of items) {
    if (!r?.inspection_date) continue
    const row = byDate.get(r.inspection_date) ?? { date: r.inspection_date, week: r.week, labelled: 0, unlabelled: 0 }
    if (r.official_location_labelled) row.labelled += 1
    else row.unlabelled += 1
    byDate.set(r.inspection_date, row)
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date))
}
