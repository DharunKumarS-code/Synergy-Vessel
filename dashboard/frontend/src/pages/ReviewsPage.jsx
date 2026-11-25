import { ClipboardCheck, Columns2, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { RESULT_GROUPS, RESULTS, ResultBadge, resultGroup, resultNote, simpleResult } from '../components/compare/InspectionOutcome'
import ReviewActions, { REVIEW_LABELS, ReviewHistory } from '../components/compare/ReviewActions'
import Photo from '../components/search/Photo'
import { EmptyState, ErrorState, Skeleton } from '../components/ui/StateViews'
import { useAuth } from '../context/AuthContext'
import { useVessel } from '../context/VesselContext'
import { useApi } from '../hooks/useApi'
import { useUrlParams } from '../hooks/useUrlParams'
import { getAllAnalyses, getAnalysis } from '../services/api'
import { locationKey } from '../utils/compare'
import { formatDate, formatNumber } from '../utils/format'

/*
 * Reviews: saved AI condition assessments that need a person's decision, awaiting-review first.
 * Reads the existing analysis history (never runs an analysis) and records decisions through ReviewActions, i.e. the
 * existing review API (server-side session/role check, audit log, original AI result kept unchanged).
 * Technical details (provider, model, prompt, token usage, confidence levels) are deliberately not shown here.
 */

// Awaiting a decision first, then reassessment requests, then decided assessments; newest first within each group.
const ORDER = { PENDING_REVIEW: 0, NEEDS_REASSESSMENT: 1, APPROVED: 2, REJECTED: 3 }
export const sortForReview = (items) => [...items].sort((a, b) =>
  (ORDER[a.review?.status] ?? 9) - (ORDER[b.review?.status] ?? 9) || String(b.created_at).localeCompare(String(a.created_at)))
export const isAwaitingReview = (a) => a.review?.status === 'PENDING_REVIEW'

export const locationOf = (a) => a.location || a.earlier?.official_location || a.earlier?.ai_suggested_location || 'Location not recorded'

/** Link that reopens exactly this saved pair on Compare Photos. */
export function compareLink(a) {
  const loc = a.location || a.earlier.official_location || a.earlier.ai_suggested_location || ''
  const p = new URLSearchParams({
    location: loc,
    earlier: a.earlier.inspection_date,
    later: a.later.inspection_date,
    a: a.earlier.record_id,
    b: a.later.record_id,
  })
  // Exact Official Location matching is the comparison default; use it only when both saved photographs have that
  // Official Location, otherwise the broader match is needed for the saved pair to be found again.
  const exact = Boolean(loc) && [a.earlier, a.later].every((r) => locationKey(r.official_location) === locationKey(loc))
  if (!exact) p.set('match', 'contains')
  return `/compare?${p.toString()}`
}

function SmallResult({ analysis }) {
  const r = RESULTS[simpleResult(analysis)]
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg border px-2.5 py-1 text-xs font-bold tracking-wide ${r.cls}`}>
      <span aria-hidden="true">{r.emoji}</span> {r.label}
    </span>
  )
}

function StatusChip({ status }) {
  const tone = status === 'PENDING_REVIEW' ? 'border-accent/50 text-accent' : 'border-surface-600 text-ink-muted'
  return <span className={`inline-flex whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${tone}`}>{REVIEW_LABELS[status] ?? status}</span>
}

function ReviewList({ items, selected, onOpen }) {
  return (
    <ul className="divide-y divide-surface-700/60" aria-label="Assessments">
      {items.map((a) => (
        <li key={a.analysis_id} className={selected === a.analysis_id ? 'bg-accent/5' : ''}>
          <button type="button" onClick={() => onOpen(a.analysis_id)} aria-pressed={selected === a.analysis_id}
            aria-label={`Open assessment: ${locationOf(a)}, ${formatDate(a.earlier?.inspection_date)} to ${formatDate(a.later?.inspection_date)}`}
            className="focus-ring grid w-full gap-2 px-5 py-4 text-left transition-colors hover:bg-surface-800/60 sm:grid-cols-[1fr_auto_auto] sm:items-center sm:gap-4">
            <span className="min-w-0">
              <span className="block truncate text-sm font-semibold text-ink">{locationOf(a)}</span>
              <span className="block text-xs text-ink-muted">
                {a.vessel_name} · {formatDate(a.earlier?.inspection_date)} → {formatDate(a.later?.inspection_date)}
              </span>
            </span>
            <SmallResult analysis={a} />
            <StatusChip status={a.review?.status} />
          </button>
        </li>
      ))}
    </ul>
  )
}

function Evidence({ title, items }) {
  if (!items?.length) return null
  return (
    <div>
      <h4 className="mb-1 text-xs font-semibold uppercase tracking-[0.12em] text-ink-faint">{title}</h4>
      <ul className="list-disc space-y-1 pl-5 text-sm text-ink-muted">
        {items.map((e, i) => <li key={i}>{typeof e === 'string' ? e : <><span className="font-medium text-ink">{e.region}:</span> {e.change}</>}</li>)}
      </ul>
    </div>
  )
}

function DetailPhoto({ label, photo }) {
  return (
    <figure className="space-y-2">
      <figcaption className="text-xs font-semibold uppercase tracking-[0.12em] text-ink-faint">
        {label} · <span className="normal-case tracking-normal text-ink">{formatDate(photo.inspection_date)}</span>
      </figcaption>
      <Photo item={photo} eager alt={`${label} photo ${photo.image_id}`} className="aspect-[4/3] overflow-hidden rounded-xl border border-surface-700" />
    </figure>
  )
}

function ReviewDetail({ id, onClose, onReviewed }) {
  const ref = useRef(null)
  const detail = useApi((s) => getAnalysis(id, s), [id])
  const [local, setLocal] = useState(null)
  // Braces matter: newer browsers return a Promise from scrollIntoView, and an effect may only return a clean-up function.
  useEffect(() => { ref.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' }) }, [id])
  const a = local ?? detail.data
  const key = a ? simpleResult(a) : null
  const r = a?.ai_result
  return (
    <section ref={ref} className="card scroll-mt-20" aria-label="Assessment details">
      <div className="card-header flex-wrap">
        <div>
          <h3 className="card-title">{a ? locationOf(a) : 'Assessment'}</h3>
          {a && <p className="card-subtitle">{a.vessel_name} · {formatDate(a.earlier.inspection_date)} → {formatDate(a.later.inspection_date)}</p>}
        </div>
        <div className="flex items-center gap-2">
          {a && (
            <Link to={compareLink(a)} className="focus-ring inline-flex h-8 items-center gap-1.5 rounded-lg border border-surface-600 bg-surface-800 px-3 text-xs text-ink-muted hover:border-accent/50 hover:text-ink">
              <Columns2 size={13} aria-hidden="true" /> Open in Compare Photos
            </Link>
          )}
          <button type="button" onClick={onClose} aria-label="Close assessment" className="focus-ring grid h-8 w-8 place-items-center rounded-lg border border-surface-600 bg-surface-800 text-ink-muted hover:text-ink">
            <X size={14} />
          </button>
        </div>
      </div>
      <div className="space-y-6 p-5">
        {detail.loading && !local ? (
          <Skeleton className="h-60 w-full rounded-lg" />
        ) : detail.error && !local ? (
          <ErrorState error={detail.error} title="The assessment could not be loaded" onRetry={detail.reload} compact />
        ) : a ? (
          <>
            <div className="grid gap-4 md:grid-cols-2">
              <DetailPhoto label="Earlier" photo={a.earlier} />
              <DetailPhoto label="Later" photo={a.later} />
            </div>
            <div className="flex flex-col items-center gap-2 text-center">
              <ResultBadge result={key} />
              {resultNote(a, key) && <p className="text-sm text-ink-muted">{resultNote(a, key)}</p>}
              <p className="text-xs text-ink-faint">AI-generated assessment. It is a suggestion until a reviewer confirms it.</p>
            </div>
            {a.simulated && (
              <p role="note" className="rounded-lg border border-warn/50 bg-warn/10 px-3 py-2 text-sm text-warn">
                This is a test result from the development mock provider. It is not an assessment of these photographs.
              </p>
            )}
            {a.policy_adjustments?.length > 0 && (
              <div role="note" aria-label="Safety checks" className="rounded-lg border border-warn/40 bg-warn/5 px-4 py-3 text-sm text-ink">
                <p className="font-medium">The automatic safety checks changed this result to “needs review” because:</p>
                <ul className="mt-1 list-disc pl-5 text-ink-muted">{a.policy_adjustments.map((x, i) => <li key={i}>{x}</li>)}</ul>
              </div>
            )}
            {r?.explanation && (
              <div>
                <h4 className="mb-1 text-xs font-semibold uppercase tracking-[0.12em] text-ink-faint">What the AI saw</h4>
                <p className="text-sm leading-relaxed text-ink">{r.explanation}</p>
              </div>
            )}
            <div className="grid gap-5 md:grid-cols-2">
              <Evidence title="Signs of deterioration" items={r?.deterioration_evidence} />
              <Evidence title="Signs of improvement" items={r?.improvement_evidence} />
              <Evidence title="Limitations of this comparison" items={r?.limitations} />
            </div>
            <div className="space-y-3 rounded-xl border border-surface-700 bg-surface-900 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h4 className="text-sm font-semibold text-ink">Review</h4>
                <span aria-label="Review status"><StatusChip status={a.review?.status} /></span>
              </div>
              <ReviewActions analysis={a} onReviewed={(updated) => { setLocal(updated); onReviewed() }} />
              <ReviewHistory analysis={a} />
              <p className="text-[11px] text-ink-faint">A review records your decision; the original AI result is kept unchanged.</p>
            </div>
          </>
        ) : null}
      </div>
    </section>
  )
}

export default function ReviewsPage() {
  const { canReview } = useAuth()
  const [params, mutate] = useUrlParams()
  const showAll = params.get('show') === 'all'
  const openId = params.get('id')
  const { selectedVessel, loading: vesselLoading } = useVessel()
  const vessel = selectedVessel?.vessel_id
  const [nonce, setNonce] = useState(0)
  const all = useApi((s) => getAllAnalyses({ vessel }, s), [vessel, nonce], { enabled: Boolean(vessel) })
  // SV-05: ?result=improved|neutral|deteriorated|needs_review (the Dashboard cards link here). Unknown values are ignored.
  const result = RESULT_GROUPS.find((g) => g.key === params.get('result')) ?? null
  const sorted = useMemo(() => sortForReview(all.data?.items ?? []), [all.data])
  const inResult = result ? sorted.filter((a) => resultGroup(a) === result.key) : sorted
  const awaiting = inResult.filter(isAwaitingReview)
  const shown = showAll ? inResult : awaiting
  const base = showAll ? sorted : sorted.filter(isAwaitingReview)          // the chip counts follow the open tab
  const groupCount = (key) => base.filter((a) => resultGroup(a) === key).length
  // Tab and filter changes are new history entries (browser back/forward); opening an assessment is not.
  const set = (patch, { push = false } = {}) => mutate((next) => {
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v)
      else next.delete(k)
    }
  }, { replace: !push })
  const tab = (active) => `focus-ring rounded-lg px-4 py-2 text-sm font-semibold transition-colors ${active ? 'bg-accent/15 text-ink' : 'text-ink-muted hover:text-ink'}`
  const chip = (active) => `focus-ring inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
    active ? 'border-accent/60 bg-accent/15 text-ink' : 'border-surface-600 text-ink-muted hover:border-accent/40 hover:text-ink'}`

  return (
    <div className="space-y-6">
      <section className="card p-6" aria-labelledby="reviews-title">
        <div className="flex items-start gap-4">
          <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-accent/10 text-accent ring-1 ring-accent/20">
            <ClipboardCheck size={20} aria-hidden="true" />
          </span>
          <div>
            <h2 id="reviews-title" className="text-xl font-semibold tracking-tight text-ink">Reviews</h2>
            <p className="mt-1 text-sm text-ink-muted">
              AI condition assessments are suggestions until a reviewer approves them.{' '}
              {canReview ? 'Open an assessment to see both photos and record your decision.' : 'Open an assessment to see both photos and its review status.'}
            </p>
          </div>
        </div>
      </section>

      <section className="card" aria-label="Assessments to review">
        <div className="card-header flex-wrap">
          <div role="tablist" aria-label="Which assessments" className="flex gap-1">
            <button type="button" role="tab" aria-selected={!showAll} className={tab(!showAll)} onClick={() => set({ show: null }, { push: true })}>
              Awaiting review{all.data ? ` (${formatNumber(awaiting.length)})` : ''}
            </button>
            <button type="button" role="tab" aria-selected={showAll} className={tab(showAll)} onClick={() => set({ show: 'all' }, { push: true })}>
              All assessments{all.data ? ` (${formatNumber(inResult.length)})` : ''}
            </button>
          </div>
          {selectedVessel && <p className="card-subtitle">{selectedVessel.vessel_name}</p>}
        </div>
        {all.data && (
          <div role="group" aria-label="Filter by result" className="flex flex-wrap gap-2 border-b border-surface-700/60 px-5 py-3">
            <button type="button" aria-pressed={!result} onClick={() => set({ result: null }, { push: true })}
              className={chip(!result)}>All results</button>
            {RESULT_GROUPS.map((g) => (
              <button key={g.key} type="button" aria-pressed={result?.key === g.key} onClick={() => set({ result: g.key }, { push: true })}
                className={chip(result?.key === g.key)}>
                <span aria-hidden="true">{RESULTS[g.result].emoji}</span> {g.label} <span className="tabular-nums text-ink-faint">{formatNumber(groupCount(g.key))}</span>
              </button>
            ))}
          </div>
        )}
        {!vessel && !vesselLoading ? (
          <EmptyState title="No vessels available" />
        ) : all.loading || vesselLoading ? (
          <div className="space-y-2 p-5" role="status" aria-label="Loading assessments">
            {[0, 1, 2].map((i) => <Skeleton key={i} className="h-14 w-full rounded-lg" />)}
          </div>
        ) : all.error ? (
          <ErrorState error={all.error} title="The assessments could not be loaded" onRetry={all.reload} />
        ) : !shown.length && result ? (
          <div className="flex flex-col items-center gap-3 px-6 py-12 text-center" role="status" aria-label="No matching assessments">
            <p className="text-sm font-medium text-ink">
              No {showAll ? '' : 'awaiting-review '}assessments with the result “{result.label}” for this vessel.
            </p>
            <button type="button" onClick={() => set({ result: null }, { push: true })}
              className="focus-ring inline-flex h-9 items-center gap-2 rounded-lg border border-surface-600 bg-surface-800 px-4 text-sm text-ink hover:border-accent/50">
              Show all results
            </button>
          </div>
        ) : !shown.length ? (
          <div className="flex flex-col items-center gap-3 px-6 py-12 text-center">
            <p className="text-sm font-medium text-ink">
              {sorted.length ? 'Nothing is awaiting review. All assessments have a decision.' : 'No assessments yet.'}
            </p>
            <p className="max-w-md text-xs text-ink-muted">Assessments are created when you analyse an earlier and a later photo on Compare Photos.</p>
            <Link to="/compare" className="focus-ring inline-flex h-9 items-center gap-2 rounded-lg border border-surface-600 bg-surface-800 px-4 text-sm text-ink hover:border-accent/50">
              <Columns2 size={14} aria-hidden="true" /> Compare Photos
            </Link>
          </div>
        ) : (
          <ReviewList items={shown} selected={openId} onOpen={(id) => set({ id })} />
        )}
      </section>

      {openId && /^CA-\d{8}-[0-9a-f]{10}$/.test(openId) && (
        <ReviewDetail key={openId} id={openId} onClose={() => set({ id: null })} onReviewed={() => setNonce((n) => n + 1)} />
      )}
    </div>
  )
}
