import { CalendarRange, Camera, ChevronRight, ClipboardCheck, Columns2, Images, Ship } from 'lucide-react'
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { RESULT_GROUPS, RESULTS, simpleResult } from '../components/compare/InspectionOutcome'
import StatCard from '../components/StatCard'
import { ErrorState, Skeleton } from '../components/ui/StateViews'
import { useVessel } from '../context/VesselContext'
import { useApi } from '../hooks/useApi'
import { getAllAnalyses, getAllInspections } from '../services/api'
import { formatDate, formatNumber, formatWeekLabel } from '../utils/format'
import { summariseInspections } from '../utils/inspectionStats'

/*
 * Dashboard: what needs attention. Every number is read from the backend (inspection records and saved AI
 * assessments); nothing is estimated or hard-coded. The condition summary uses the same result mapping as
 * Compare Photos (InspectionOutcome.simpleResult), so a result is never shown differently on two screens.
 */

// Order and names of the condition summary. "Needs review" also counts assessments that have no usable result
// (rejected by a reviewer, or test results), exactly as Compare Photos shows them. SV-05: the groups are shared with
// the Reviews `?result=` filter each card opens, so a card's count always equals the number of rows it shows.
const SUMMARY = RESULT_GROUPS.map((g) => ({ key: g.result, match: g.match, filter: g.key }))

export function conditionSummary(items) {
  const counts = Object.fromEntries(SUMMARY.map((s) => [s.key, 0]))
  for (const a of items ?? []) {
    const r = simpleResult(a)
    const row = SUMMARY.find((s) => s.match.includes(r))
    if (row) counts[row.key] += 1
  }
  return counts
}

const btn = 'focus-ring inline-flex h-11 items-center justify-center gap-2 rounded-xl px-5 text-sm font-semibold transition-colors'

export default function DashboardPage() {
  const { vessels, selectedVessel, loading: vesselLoading, error: vesselError } = useVessel()
  const vesselId = selectedVessel?.vessel_id
  const ready = Boolean(vesselId)

  const inspections = useApi((signal) => getAllInspections({ vessel: vesselId }, signal), [vesselId], { enabled: ready })
  const analyses = useApi((signal) => getAllAnalyses({ vessel: vesselId }, signal), [vesselId], { enabled: ready })

  const summary = useMemo(() => summariseInspections(inspections.data?.items), [inspections.data])
  const latestWeek = summary?.weeks.filter((w) => w.dates.length).at(-1) ?? null
  const latestDate = summary?.inspectionDates.at(-1) ?? null
  const items = analyses.data?.items
  const pending = items ? items.filter((a) => a.review?.status === 'PENDING_REVIEW').length : null
  const counts = useMemo(() => conditionSummary(items), [items])

  const inspLoading = vesselLoading || inspections.loading
  const inspError = vesselError || inspections.error
  const aiLoading = vesselLoading || analyses.loading

  return (
    <div className="space-y-6">
      {/* What needs attention */}
      <div className="grid gap-6 lg:grid-cols-3">
        <section className="card flex flex-col gap-4 p-6 lg:col-span-2" aria-labelledby="pending-title">
          <div className="flex items-start gap-4">
            <span className="grid h-12 w-12 shrink-0 place-items-center rounded-xl bg-accent/10 text-accent ring-1 ring-accent/20">
              <ClipboardCheck size={22} aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <h2 id="pending-title" className="text-sm font-semibold uppercase tracking-[0.14em] text-ink-faint">Pending reviews</h2>
              {aiLoading ? (
                <Skeleton className="mt-2 h-9 w-72" />
              ) : analyses.error ? (
                <ErrorState error={analyses.error} title="The assessments could not be loaded" onRetry={analyses.reload} compact />
              ) : (
                <p className="mt-1 text-2xl font-bold text-ink sm:text-3xl" aria-label="Assessments awaiting review">
                  {pending === 0 ? 'No assessments awaiting review'
                    : `${formatNumber(pending)} ${pending === 1 ? 'Assessment' : 'Assessments'} Awaiting Review`}
                </p>
              )}
              <p className="mt-1 text-sm text-ink-muted">AI assessments are suggestions until a reviewer approves or rejects them.</p>
            </div>
          </div>
          <div className="flex flex-wrap gap-3">
            <Link to="/reviews" className={`${btn} bg-accent text-surface-950 hover:bg-accent-soft`}>
              <ClipboardCheck size={17} aria-hidden="true" /> Review Assessments
            </Link>
            <Link to="/compare" className={`${btn} border border-surface-600 bg-surface-800 text-ink hover:border-accent/50`}>
              <Columns2 size={17} aria-hidden="true" /> Compare Photos
            </Link>
          </div>
        </section>

        <section className="card p-6" aria-labelledby="latest-title">
          <h2 id="latest-title" className="text-sm font-semibold uppercase tracking-[0.14em] text-ink-faint">Latest inspection</h2>
          {inspLoading ? (
            <Skeleton className="mt-3 h-16 w-full" />
          ) : inspError ? (
            <ErrorState error={inspError} title="Inspection data unavailable" onRetry={inspections.reload} compact />
          ) : latestWeek ? (
            <div className="mt-2" aria-label="Latest inspection details">
              <p className="text-2xl font-bold text-ink">{formatWeekLabel(latestWeek.week)}</p>
              <p className="text-sm text-ink-muted">{formatDate(latestDate)} · {formatNumber(latestWeek.appearances)} photos</p>
            </div>
          ) : (
            <p className="mt-2 text-sm text-ink-muted">No inspections found.</p>
          )}
        </section>
      </div>

      {/* Condition summary */}
      <section className="card p-6" aria-labelledby="condition-title">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="condition-title" className="text-sm font-semibold uppercase tracking-[0.14em] text-ink-faint">Condition results</h2>
          {items && <p className="text-xs text-ink-muted">From {formatNumber(items.length)} saved {items.length === 1 ? 'assessment' : 'assessments'}</p>}
        </div>
        {aiLoading ? (
          <Skeleton className="mt-4 h-16 w-full" />
        ) : analyses.error ? null : (
          <ul className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4" aria-label="Condition results">
            {SUMMARY.map(({ key, filter }) => {
              const r = RESULTS[key]
              return (
                <li key={key} aria-label={`${r.label}: ${counts[key]}`}>
                  {/* SV-05: each card opens Reviews filtered to exactly the assessments it counts. */}
                  <Link to={`/reviews?show=all&result=${filter}`} aria-label={`Show ${r.label.toLowerCase()} assessments (${counts[key]})`}
                    className={`focus-ring group block cursor-pointer rounded-xl border-2 px-4 py-3 transition duration-150 hover:-translate-y-0.5 hover:shadow-card hover:brightness-125 active:translate-y-0 ${r.cls}`}>
                    <p className="flex items-center justify-between gap-2 text-xs font-bold tracking-wide">
                      <span><span aria-hidden="true">{r.emoji}</span> {r.label}</span>
                      <ChevronRight size={14} aria-hidden="true" className="opacity-50 transition-opacity group-hover:opacity-100" />
                    </p>
                    <p className="mt-1 text-3xl font-bold tabular-nums">{formatNumber(counts[key])}</p>
                  </Link>
                </li>
              )
            })}
          </ul>
        )}
      </section>

      {/* Overview */}
      <section aria-label="Overview" className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Vessels" value={vesselLoading ? undefined : vessels.length} icon={Ship} loading={vesselLoading}
          error={vesselError} detail={selectedVessel?.vessel_name} />
        <StatCard label="Inspection weeks" value={summary?.weeks.length} icon={CalendarRange} loading={inspLoading} error={inspError}
          detail={summary?.inspectionDates.length ? `${formatDate(summary.inspectionDates[0])} – ${formatDate(latestDate)}` : null} />
        {/* SV-06: appearances count every photo in every inspection week (a photo repeated in a later week counts
            again); unique photographs count each distinct picture once, by the SHA-256 the backend stores on every
            record. The unique count is "Unavailable" rather than guessed if any record has no hash. */}
        <StatCard label="Inspection appearances" value={summary?.appearances} icon={Camera} loading={inspLoading} error={inspError}
          detail="Every photo in every inspection week" />
        <StatCard label="Unique photographs" value={summary?.uniquePhotographs} icon={Images} loading={inspLoading} error={inspError}
          detail="Repeated photos counted once" />
      </section>
    </div>
  )
}
