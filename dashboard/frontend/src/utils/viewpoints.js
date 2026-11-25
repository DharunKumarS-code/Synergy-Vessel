/*
 * SV-08: physical viewpoint helpers for Image Comparison. A viewpoint (from GET /api/inspections/viewpoints) lists,
 * for every inspection week, the photographs of one physical view. Both sides of a comparison always come from the
 * same viewpoint; the AI is only ever offered for two photographs an administrator approved for that viewpoint.
 */

export const PAIR = {
  UNAVAILABLE: 'unavailable',   // at least one side has no photograph for its week
  NOT_RETAKEN: 'not_retaken',   // byte-identical file: the photograph was reused, nothing to compare
  REVIEW: 'review',             // mapping not approved for both photographs: no AI
  APPROVED: 'approved',         // both approved members of the same viewpoint: AI allowed (still checked by the server)
}

export const shaOf = (p) => p?.extracted_photograph?.sha256 ?? null

/** Weeks that have at least one photograph of the viewpoint. */
export const weeksWithPhotos = (view) => (view?.weeks ?? []).filter((w) => w.photos?.length > 0)

/** Parse a week URL value; anything else is ignored. */
export const toWeek = (v) => (/^\d{1,2}$/.test(String(v ?? '')) ? Number(v) : null)

/**
 * Earlier and later weeks: the requested ones when valid (earlier strictly before later), otherwise the last two
 * weeks that have a photograph. Weeks without a photograph may still be chosen on purpose (shown as not available).
 */
export function resolveWeeks(view, earlier, later) {
  const all = (view?.weeks ?? []).map((w) => w.week)
  const e = all.includes(earlier) ? earlier : null
  const l = all.includes(later) ? later : null
  if (e !== null && l !== null && e < l) return { earlier: e, later: l }
  const withPhotos = weeksWithPhotos(view).map((w) => w.week)
  if (e !== null) return { earlier: e, later: withPhotos.find((w) => w > e) ?? all.find((w) => w > e) ?? null }
  if (l !== null) return { earlier: [...withPhotos].reverse().find((w) => w < l) ?? [...all].reverse().find((w) => w < l) ?? null, later: l }
  if (withPhotos.length >= 2) return { earlier: withPhotos[withPhotos.length - 2], later: withPhotos[withPhotos.length - 1] }
  return { earlier: all[0] ?? null, later: all[1] ?? null }
}

export const weekOf = (view, week) => (view?.weeks ?? []).find((w) => w.week === week) ?? null

/** The chosen photograph of a week (by record ID from the URL), else the first one. */
export const pickPhoto = (photos, recordId) => (photos ?? []).find((p) => p.record_id === recordId) ?? photos?.[0] ?? null

/** How the two selected photographs relate (the server's pair check stays the authority for AI eligibility). */
export function pairStatus(a, b) {
  if (!a || !b) return PAIR.UNAVAILABLE
  if (shaOf(a) && shaOf(a) === shaOf(b)) return PAIR.NOT_RETAKEN
  if (a.viewpoint_member?.status === 'approved' && b.viewpoint_member?.status === 'approved') return PAIR.APPROVED
  return PAIR.REVIEW
}

/** The first week after `week` with a photograph that is not the same file as `sha` (a new photograph). */
export function nextNewPhotoWeek(view, week, sha) {
  for (const w of view?.weeks ?? []) {
    if (w.week <= week) continue
    const fresh = (w.photos ?? []).find((p) => !sha || shaOf(p) !== sha)
    if (fresh) return { week: w.week, recordId: fresh.record_id }
  }
  return null
}

/** The viewpoint (of the given list) that holds a record: used to open links that carry only a/b record IDs. */
export const viewpointOfRecord = (views, recordId) =>
  recordId ? (views ?? []).find((v) => v.weeks.some((w) => w.photos.some((p) => p.record_id === recordId))) ?? null : null

/** The week (number) in a viewpoint that holds a record. */
export const weekOfRecord = (view, recordId) =>
  (view?.weeks ?? []).find((w) => w.photos.some((p) => p.record_id === recordId))?.week ?? null

const STATUS_WORDS = { proposed: 'awaiting review', needs_review: 'needs review', approved: 'approved', rejected: 'rejected' }
export const statusWords = (s) => STATUS_WORDS[s] ?? s

/** Short summary for the viewpoint dropdown, e.g. "8 of 11 approved". */
export function viewpointSummary(view) {
  const c = view?.counts ?? {}
  const active = (c.proposed ?? 0) + (c.needs_review ?? 0) + (c.approved ?? 0)
  if (!active) return 'no photographs'
  if ((c.approved ?? 0) === active) return 'approved'
  return `${c.approved ?? 0} of ${active} approved`
}

/** Week label for selects: "Week 38 · 27 Sep 2026 · 2 photos" / "· no photograph". */
export function weekOptionLabel(w, formatDate) {
  const n = w.photos?.length ?? 0
  const date = w.inspection_date ? ` · ${formatDate(w.inspection_date)}` : ''
  return `Week ${w.week}${date} · ${n === 0 ? 'no photograph' : n === 1 ? '1 photo' : `${n} photos`}`
}
