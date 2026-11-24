/*
 * Plain-language outcome of an image comparison, shared by the Image Comparison page and the simple result screen.
 * Reuses existing data only: the read-only image-pair check (duplicates) and saved AI condition analyses (result).
 * Nothing here calls the AI or performs any new detection.
 */

const DUPLICATE_CATEGORIES = ['IDENTICAL_FILE', 'VISUALLY_IDENTICAL', 'NEAR_DUPLICATE']

/** True when the existing pair check says both photographs are the same picture (exact, visual or near-duplicate). */
export const isDuplicatePair = (v) => Boolean(v && (v.sha256_duplicate || DUPLICATE_CATEGORIES.includes(v.perceptual?.category)))

/** Same photograph file according to the SHA-256 already held on each record (used while the pair check loads). */
export const isSameFile = (a, b) => {
  const sha = a?.extracted_photograph?.sha256
  return Boolean(sha && sha === b?.extracted_photograph?.sha256)
}

/**
 * Maps a saved AI condition analysis to the plain result. Simulated (mock/test) and reviewer-rejected results are
 * never presented as a result; a reviewer's request for reassessment shows as "needs review".
 */
export function simpleResult(analysis) {
  if (!analysis || analysis.simulated) return 'NOT_ASSESSED'
  const review = analysis.review?.status
  if (review === 'REJECTED') return 'NOT_ASSESSED'
  if (review === 'NEEDS_REASSESSMENT') return 'NEEDS_REVIEW'
  if (analysis.classification === 'AMELIORATED') return 'IMPROVED'
  if (analysis.classification === 'DETERIORATED') return 'DETERIORATED'
  if (analysis.classification === 'STABLE') return 'NO_CHANGE'
  return 'NEEDS_REVIEW'
}

/**
 * SV-05: the four result groups of the Dashboard cards, also used as the Reviews `?result=` filter. One definition, so a
 * card's count always equals the number of assessments its filter shows. "Needs review" deliberately includes
 * assessments without a usable result (rejected, test results), exactly as the Dashboard has always counted them.
 */
export const RESULT_GROUPS = [
  { key: 'improved', result: 'IMPROVED', label: 'Improved', match: ['IMPROVED'] },
  { key: 'neutral', result: 'NO_CHANGE', label: 'Neutral', match: ['NO_CHANGE'] },
  { key: 'deteriorated', result: 'DETERIORATED', label: 'Deteriorated', match: ['DETERIORATED'] },
  { key: 'needs_review', result: 'NEEDS_REVIEW', label: 'Needs review', match: ['NEEDS_REVIEW', 'NOT_ASSESSED'] },
]

/** The result group key ('improved' | 'neutral' | 'deteriorated' | 'needs_review') of a saved assessment. */
export const resultGroup = (analysis) => RESULT_GROUPS.find((g) => g.match.includes(simpleResult(analysis)))?.key ?? null

/** One short, non-technical sentence under the result. */
export function resultNote(analysis, key) {
  if (key === 'NOT_ASSESSED') {
    if (analysis?.simulated) return 'Only a test result exists for these photos.'
    if (analysis?.review?.status === 'REJECTED') return 'The previous assessment was rejected by a reviewer.'
    return 'These photos have not been assessed yet.'
  }
  const review = analysis?.review?.status
  if (review === 'PENDING_REVIEW') return 'Awaiting confirmation by a reviewer.'
  if (review === 'NEEDS_REASSESSMENT') return 'A reviewer has asked for these photos to be assessed again.'
  if (review === 'APPROVED') return 'Confirmed by a reviewer.'
  return null
}

export const RESULTS = {
  IMPROVED: { emoji: '🟢', label: 'IMPROVED', cls: 'border-ok/60 bg-ok/10 text-ok' },
  DETERIORATED: { emoji: '🔴', label: 'DETERIORATED', cls: 'border-bad/60 bg-bad/10 text-bad' },
  NO_CHANGE: { emoji: '⚪', label: 'NEUTRAL', cls: 'border-surface-500 bg-surface-800 text-ink' },
  NEEDS_REVIEW: { emoji: '🟡', label: 'NEEDS REVIEW', cls: 'border-warn/60 bg-warn/10 text-warn' },
  // No usable saved assessment (none yet, test-only or rejected): shown as "needs review", never as a condition change.
  NOT_ASSESSED: { emoji: '🟡', label: 'NEEDS REVIEW', cls: 'border-warn/60 bg-warn/10 text-warn' },
}

export function ResultBadge({ result }) {
  const r = RESULTS[result] ?? RESULTS.NOT_ASSESSED
  return (
    <p aria-label="Inspection result"
      className={`inline-flex items-center gap-3 rounded-2xl border-2 px-6 py-3 text-2xl font-extrabold tracking-wide sm:px-8 sm:py-4 sm:text-4xl ${r.cls}`}>
      <span aria-hidden="true">{r.emoji}</span> {r.label}
    </p>
  )
}

export function DuplicateWarning() {
  return (
    <div role="alert" aria-label="Duplicate image" className="flex flex-col items-center gap-1 rounded-xl border-2 border-bad bg-bad/15 px-4 py-4 text-center">
      <p className="text-xl font-extrabold tracking-wide text-bad sm:text-2xl">🔴 DUPLICATE IMAGE</p>
      <p className="text-sm text-ink">These images show the same picture and cannot be used to identify a change.</p>
    </div>
  )
}
