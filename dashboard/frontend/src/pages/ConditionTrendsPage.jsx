import {
  ArrowRight, BrainCircuit, CheckCircle2, ClipboardCheck, Clock, Columns2, FlaskConical, LineChart, MapPin, RefreshCw,
  RotateCcw, Search, UserCheck, X,
} from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ClassificationBadge, CLASSIFICATIONS, REVIEW_STATUSES } from '../components/compare/ConditionAnalysis'
import { EmptyState, ErrorState, LoadingState } from '../components/ui/StateViews'
import { useVessel } from '../context/VesselContext'
import { useApi } from '../hooks/useApi'
import { usePhotoRetry } from '../hooks/usePhotoRetry'
import { useUrlParams } from '../hooks/useUrlParams'
import { getAllAnalyses, imageUrl } from '../services/api'
import {
  analysisLocation, byLaterWeek, byLocation, CLASSIFICATION_ORDER, classificationDistribution, filterAnalyses, isApproved,
  lastReview, newestFirst, REVIEW_ORDER, reviewDistribution, reviewStatusOf, summarise,
} from '../utils/conditionTrends'
import { formatDate, formatNumber } from '../utils/format'

/**
 * Condition Trends: saved AI condition analyses over time, by location and review status.
 * Read-only. Uses GET /api/ai-analysis/history only (never runs an analysis). AI output is labelled as AI-generated;
 * only analyses a reviewer APPROVED count as human-confirmed. Details and reviews open in the existing
 * AI Condition Analysis page.
 */

const KEYS = ['vessel', 'location', 'from', 'to', 'review', 'q', 'sim']
const PAGE = 15

export const REVIEW_TONES = {
  PENDING_REVIEW: { bar: 'bg-accent', badge: 'border-accent/40 bg-accent/10 text-accent' },
  APPROVED: { bar: 'bg-ok', badge: 'border-ok/40 bg-ok/10 text-ok' },
  REJECTED: { bar: 'bg-bad', badge: 'border-bad/40 bg-bad/10 text-bad' },
  NEEDS_REASSESSMENT: { bar: 'bg-warn', badge: 'border-warn/40 bg-warn/10 text-warn' },
}
const CLASS_BARS = { DETERIORATED: 'bg-bad', AMELIORATED: 'bg-ok', STABLE: 'bg-accent', NEEDS_REVIEW: 'bg-warn' }

const detailHref = (a) => `/reviews?id=${encodeURIComponent(a.analysis_id)}`
const dateTime = (iso) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export function ReviewBadge({ status }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${REVIEW_TONES[status]?.badge ?? ''}`}>
      <UserCheck size={11} aria-hidden="true" /> {REVIEW_STATUSES[status] ?? status}
    </span>
  )
}

const AiTag = () => (
  <span className="inline-flex items-center gap-1 rounded border border-surface-600/70 bg-surface-800 px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide text-ink-faint">
    <BrainCircuit size={10} aria-hidden="true" /> AI-generated
  </span>
)
const HumanTag = () => (
  <span className="inline-flex items-center gap-1 rounded border border-ok/30 bg-ok/10 px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide text-ok">
    <UserCheck size={10} aria-hidden="true" /> Human decision
  </span>
)
const SimTag = () => (
  <span className="inline-flex items-center gap-1 rounded border border-warn/40 bg-warn/10 px-1.5 py-px text-[10px] font-semibold text-warn">
    <FlaskConical size={10} aria-hidden="true" /> Simulated
  </span>
)

/** Horizontal stacked bar plus a legend list (the list carries the numbers for screen readers). */
export function Distribution({ label, rows, total }) {
  const shown = rows.filter((r) => r.count > 0)
  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-surface-800" role="img"
        aria-label={`${label}: ${rows.map((r) => `${r.label} ${r.count}`).join(', ')}`}>
        {total > 0 && shown.map((r) => <span key={r.key} className={r.bar} style={{ width: `${(r.count / total) * 100}%` }} />)}
      </div>
      <ul className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs" aria-label={label}>
        {rows.map((r) => (
          <li key={r.key} className="flex items-center justify-between gap-2">
            <span className="flex min-w-0 items-center gap-2 text-ink-muted"><span className={`h-2 w-2 shrink-0 rounded-full ${r.bar}`} aria-hidden="true" />{r.label}</span>
            <span className="tabular-nums text-ink">{formatNumber(r.count)}{total > 0 && <span className="ml-1 text-ink-faint">({Math.round((r.count / total) * 100)}%)</span>}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

const reviewRows = (dist) => REVIEW_ORDER.map((k) => ({ key: k, label: REVIEW_STATUSES[k], count: dist[k], bar: REVIEW_TONES[k].bar }))
const classRows = (dist) => CLASSIFICATION_ORDER.map((k) => ({ key: k, label: CLASSIFICATIONS[k].label, count: dist[k], bar: CLASS_BARS[k] }))

function Metric({ label, value, detail, icon: Icon, tone = 'text-accent' }) {
  return (
    <div className="card flex items-start gap-3 p-4" role="group" aria-label={label}>
      <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-surface-800 ${tone}`}><Icon size={17} aria-hidden="true" /></span>
      <div className="min-w-0">
        <p className="text-xs text-ink-faint">{label}</p>
        <p className="text-2xl font-semibold tabular-nums text-ink" data-testid="metric-value">{formatNumber(value)}</p>
        {detail && <p className="text-[11px] text-ink-faint">{detail}</p>}
      </div>
    </div>
  )
}

const inputCls = 'focus-ring h-9 w-full rounded-lg border border-surface-600 bg-surface-800 px-3 text-sm text-ink placeholder:text-ink-faint'

function Filters({ state, update, vessels, locations, onClear, active }) {
  return (
    <form className="card grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-6" role="search" aria-label="Filter analyses" onSubmit={(e) => e.preventDefault()}>
      <label className="relative lg:col-span-2">
        <span className="sr-only">Search analyses</span>
        <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" aria-hidden="true" />
        <input type="search" value={state.q} onChange={(e) => update({ q: e.target.value })} className={`${inputCls} pl-8`}
          placeholder="Search ID, image or location" />
      </label>
      <label>
        <span className="sr-only">Vessel</span>
        <select value={state.vessel} onChange={(e) => update({ vessel: e.target.value })} className={inputCls} aria-label="Vessel">
          <option value="">All vessels</option>
          {vessels.map((v) => <option key={v.vessel_id} value={v.vessel_id}>{v.vessel_name}</option>)}
        </select>
      </label>
      <label>
        <span className="sr-only">Official Location</span>
        <select value={state.location} onChange={(e) => update({ location: e.target.value })} className={inputCls} aria-label="Official Location">
          <option value="">All locations</option>
          {locations.map((l) => <option key={l} value={l}>{l}</option>)}
          {state.location && !locations.includes(state.location) && <option value={state.location}>{state.location}</option>}
        </select>
      </label>
      <label>
        <span className="sr-only">Review status</span>
        <select value={state.review} onChange={(e) => update({ review: e.target.value })} className={inputCls} aria-label="Review status">
          <option value="">All review statuses</option>
          {REVIEW_ORDER.map((k) => <option key={k} value={k}>{REVIEW_STATUSES[k]}</option>)}
        </select>
      </label>
      <div className="flex items-center gap-2">
        <button type="button" onClick={onClear} disabled={!active}
          className="focus-ring inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-lg border border-surface-600 bg-surface-800 px-3 text-xs font-medium text-ink-muted hover:border-accent/50 hover:text-ink disabled:opacity-40">
          <RotateCcw size={13} aria-hidden="true" /> Clear filters
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted sm:col-span-2 lg:col-span-4">
        <span>Inspection dates</span>
        <input type="date" value={state.from} max={state.to || undefined} onChange={(e) => update({ from: e.target.value })} aria-label="From date" className={`${inputCls} w-auto`} />
        <span aria-hidden="true">–</span>
        <input type="date" value={state.to} min={state.from || undefined} onChange={(e) => update({ to: e.target.value })} aria-label="To date" className={`${inputCls} w-auto`} />
        <span className="text-[11px] text-ink-faint">matches when either photograph’s date is in range</span>
      </div>
      <label className="flex items-center gap-2 text-xs text-ink-muted sm:col-span-2">
        <input type="checkbox" checked={state.sim === '1'} onChange={(e) => update({ sim: e.target.checked ? '1' : '' })} className="h-4 w-4 accent-[#90CAF9]" />
        Include simulated (mock) results
      </label>
    </form>
  )
}

function Thumb({ photo, side }) {
  const retry = usePhotoRetry(photo.image_id)   // SV-02: cookie-authorised; re-check the session once on failure
  return (
    <figure className="w-16 shrink-0 sm:w-20">
      <img key={retry.attempt} onError={retry.onError} src={imageUrl(photo.image_id)} alt={`${side} photograph ${photo.image_id}, ${formatDate(photo.inspection_date)}`} loading="lazy"
        className="aspect-[4/3] w-full rounded-md border border-surface-700 bg-surface-800 object-cover" />
      <figcaption className="mt-0.5 text-center text-[10px] text-ink-faint">{side}</figcaption>
    </figure>
  )
}

function TimelineEntry({ a }) {
  const review = lastReview(a)
  const status = reviewStatusOf(a)
  return (
    <li className="relative pl-6" aria-label={`Analysis ${a.analysis_id}`}>
      <span className={`absolute left-0 top-2 h-2.5 w-2.5 rounded-full ring-4 ring-surface-850 ${REVIEW_TONES[status]?.bar}`} aria-hidden="true" />
      <div className="rounded-lg border border-surface-700/60 bg-surface-900/40 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-ink-faint">
            <time dateTime={a.created_at}>{dateTime(a.created_at)}</time> · {a.vessel_name} · <span className="font-mono">{a.analysis_id}</span>
          </p>
          <Link to={detailHref(a)} className="focus-ring inline-flex items-center gap-1 rounded-md text-xs font-medium text-accent hover:underline">
            View details<span className="sr-only"> for {a.analysis_id}</span> <ArrowRight size={12} aria-hidden="true" />
          </Link>
        </div>
        <div className="mt-2 flex gap-3">
          <div className="flex gap-1.5">
            <Thumb photo={a.earlier} side="Earlier" />
            <Thumb photo={a.later} side="Later" />
          </div>
          <div className="min-w-0 flex-1 space-y-1.5">
            <p className="flex items-center gap-1.5 text-sm font-medium text-ink"><MapPin size={13} className="shrink-0 text-ink-faint" aria-hidden="true" />{analysisLocation(a)}</p>
            <p className="text-xs text-ink-muted">
              {formatDate(a.earlier.inspection_date)} → {formatDate(a.later.inspection_date)} <span className="text-ink-faint">({a.interval_days} days)</span>
            </p>
            <div className="flex flex-wrap items-center gap-1.5" data-testid="ai-part">
              <AiTag /><ClassificationBadge value={a.classification} />
              <span className="text-[11px] text-ink-faint">confidence {a.confidence?.toLowerCase()}</span>
              {a.simulated && <SimTag />}
            </div>
            {a.ai_result?.explanation && (
              <p className="line-clamp-2 text-xs leading-relaxed text-ink-muted"><span className="sr-only">AI summary: </span>{a.ai_result.explanation}</p>
            )}
            <div className="flex flex-wrap items-center gap-1.5" data-testid="human-part">
              <HumanTag /><ReviewBadge status={status} />
              <span className="text-[11px] text-ink-faint">
                {review ? `by ${review.reviewer}${review.reviewer_role ? ` (${review.reviewer_role})` : ''} · ${dateTime(review.at)}` : 'not yet reviewed'}
              </span>
            </div>
          </div>
        </div>
      </div>
    </li>
  )
}

function LocationTable({ rows, onPick, selected }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <caption className="sr-only">AI analyses by Official Location</caption>
        <thead className="text-left text-[11px] uppercase tracking-wide text-ink-faint">
          <tr className="border-b border-surface-700/60">
            <th scope="col" className="px-4 py-2 font-medium">Official Location</th>
            <th scope="col" className="px-2 py-2 text-right font-medium">Analyses</th>
            <th scope="col" className="px-2 py-2 text-right font-medium">Pending</th>
            <th scope="col" className="px-2 py-2 text-right font-medium">Reviewed</th>
            <th scope="col" className="px-2 py-2 font-medium">AI findings</th>
            <th scope="col" className="px-4 py-2 font-medium">Human-approved</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-700/40">
          {rows.map((r) => (
            <tr key={r.location} className={selected === r.location ? 'bg-accent/5' : ''}>
              <th scope="row" className="px-4 py-2 text-left font-normal">
                <button type="button" onClick={() => onPick(r.location)} className="focus-ring rounded text-left text-ink hover:text-accent"
                  aria-pressed={selected === r.location} title="Show only this location">
                  {r.location}
                </button>
              </th>
              <td className="px-2 py-2 text-right tabular-nums text-ink">{r.total}</td>
              <td className="px-2 py-2 text-right tabular-nums text-ink-muted">{r.pending}</td>
              <td className="px-2 py-2 text-right tabular-nums text-ink-muted">{r.reviewed}</td>
              <td className="px-2 py-2"><Findings dist={r.ai} /></td>
              <td className="px-4 py-2">{r.approved ? <Findings dist={r.human} /> : <span className="text-xs text-ink-faint">None yet</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Findings({ dist }) {
  const parts = CLASSIFICATION_ORDER.filter((k) => dist[k] > 0)
  if (!parts.length) return <span className="text-xs text-ink-faint">—</span>
  return (
    <span className="flex flex-wrap gap-1">
      {parts.map((k) => (
        <span key={k} className={`rounded border px-1.5 py-px text-[11px] ${CLASSIFICATIONS[k].tone}`}>{CLASSIFICATIONS[k].label} {dist[k]}</span>
      ))}
    </span>
  )
}

function WeekTrend({ rows }) {
  const max = Math.max(1, ...rows.map((r) => r.total))
  return (
    <ul className="space-y-2" aria-label="AI classifications by later inspection week">
      {rows.map((r) => (
        <li key={r.week} className="grid grid-cols-[6.5rem_1fr_auto] items-center gap-3 text-xs">
          <span className="truncate text-ink-muted" title={formatDate(r.date)}>{r.week}</span>
          <span className="flex h-2.5 overflow-hidden rounded-full bg-surface-800" role="img"
            aria-label={`${r.week}: ${CLASSIFICATION_ORDER.map((k) => `${CLASSIFICATIONS[k].label} ${r.ai[k]}`).join(', ')}; ${r.approved} approved`}
            style={{ width: `${Math.max(8, (r.total / max) * 100)}%` }}>
            {CLASSIFICATION_ORDER.map((k) => r.ai[k] > 0 && <span key={k} className={CLASS_BARS[k]} style={{ width: `${(r.ai[k] / r.total) * 100}%` }} />)}
          </span>
          <span className="tabular-nums text-ink-faint">{r.total} · {r.approved} approved</span>
        </li>
      ))}
    </ul>
  )
}

function Card({ id, title, subtitle, chip, children, className = '' }) {
  return (
    <section className={`card ${className}`} aria-labelledby={`${id}-title`}>
      <div className="card-header">
        <div>
          <h3 id={`${id}-title`} className="card-title">{title}</h3>
          {subtitle && <p className="card-subtitle">{subtitle}</p>}
        </div>
        {chip}
      </div>
      {children}
    </section>
  )
}

function FirstAnalysisGuide() {
  return (
    <section className="card p-8 text-center" aria-label="No analyses yet">
      <EmptyState icon={LineChart} title="No AI condition analyses yet"
        description="Trends appear once analyses are saved. Nothing here runs an analysis." />
      <ol className="mx-auto mt-2 max-w-md space-y-1 text-left text-xs text-ink-muted">
        <li>1. Open <strong className="text-ink">Image Comparison</strong> and choose an earlier and a later photograph of the same location.</li>
        <li>2. Sign in as a reviewer or administrator and select <strong className="text-ink">Analyse images</strong>.</li>
        <li>3. Review the result on <strong className="text-ink">AI Condition Analysis</strong>; approved results count as human-confirmed here.</li>
      </ol>
      <Link to="/compare" className="focus-ring mt-5 inline-flex items-center gap-2 rounded-lg border border-accent/40 bg-accent/10 px-4 py-2 text-sm text-accent hover:bg-accent/20">
        <Columns2 size={15} aria-hidden="true" /> Go to Image Comparison
      </Link>
    </section>
  )
}

export default function ConditionTrendsPage() {
  const [params, mutate] = useUrlParams()
  const state = Object.fromEntries(KEYS.map((k) => [k, params.get(k) ?? '']))
  const update = useCallback((patch) => mutate((next) => {
    for (const [k, v] of Object.entries(patch)) (v ? next.set(k, v) : next.delete(k))
  }), [mutate])
  const clear = useCallback(() => mutate((next) => KEYS.forEach((k) => next.delete(k))), [mutate])
  const [shown, setShown] = useState(PAGE)
  const { vessels } = useVessel()
  const all = useApi((signal) => getAllAnalyses({}, signal), [])
  const items = useMemo(() => all.data?.items ?? [], [all.data])

  const includeSimulated = state.sim === '1'
  const filtered = newestFirst(filterAnalyses(items, {
    vessel: state.vessel, location: state.location, from: state.from, to: state.to, review: state.review, q: state.q, includeSimulated,
  }))
  const scope = useMemo(() => filterAnalyses(items, { vessel: state.vessel, includeSimulated }), [items, state.vessel, includeSimulated])
  const locations = useMemo(() => [...new Set(scope.map(analysisLocation))].sort((a, b) => a.localeCompare(b)), [scope])
  const hiddenSimulated = includeSimulated ? 0 : items.filter((a) => a.simulated).length
  const active = KEYS.some((k) => k !== 'sim' && state[k])

  const s = summarise(filtered)
  const aiDist = classificationDistribution(filtered)
  const humanDist = classificationDistribution(filtered.filter(isApproved))
  const locRows = byLocation(filtered)
  const vesselName = vessels.find((v) => v.vessel_id === state.vessel)?.vessel_name

  return (
    <div className="space-y-6">
      <section className="card p-6" aria-labelledby="trends-title">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex items-start gap-4">
            <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-accent/10 text-accent ring-1 ring-accent/20">
              <LineChart size={20} aria-hidden="true" />
            </span>
            <div>
              <h2 id="trends-title" className="text-xl font-semibold tracking-tight text-ink">Condition Trends</h2>
              <p className="mt-1 text-sm text-ink-muted">
                Saved AI condition analyses over time. Classifications are <strong className="font-medium text-ink">AI-generated</strong> and
                unverified until a reviewer approves them; only approved analyses count as human-confirmed.
              </p>
            </div>
          </div>
          <button type="button" onClick={all.reload} disabled={all.loading}
            className="focus-ring inline-flex shrink-0 items-center gap-1.5 self-start rounded-lg border border-surface-600 bg-surface-800 px-3 py-1.5 text-xs font-medium text-ink-muted hover:border-accent/50 hover:text-ink disabled:opacity-60">
            <RefreshCw size={13} className={all.loading ? 'animate-spin' : ''} aria-hidden="true" /> Refresh
          </button>
        </div>
      </section>

      {all.loading ? (
        <section className="card"><LoadingState label="Loading condition analyses…" rows={6} /></section>
      ) : all.error ? (
        <section className="card"><ErrorState error={all.error} title="Condition analyses could not be loaded" onRetry={all.reload} /></section>
      ) : items.length === 0 ? (
        <FirstAnalysisGuide />
      ) : (
        <>
          <Filters state={state} update={(p) => { setShown(PAGE); update(p) }} vessels={vessels} locations={locations}
            onClear={() => { setShown(PAGE); clear() }} active={active} />

          <p className="-mt-3 text-xs text-ink-faint" role="status" aria-live="polite" aria-label="Result count">
            {formatNumber(filtered.length)} of {formatNumber(items.length - hiddenSimulated)} analyses
            {vesselName ? ` · ${vesselName}` : ''}
            {hiddenSimulated > 0 && ` · ${formatNumber(hiddenSimulated)} simulated (mock) result${hiddenSimulated === 1 ? '' : 's'} hidden`}
          </p>

          <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5" aria-label="Summary">
            <Metric label="AI analyses" value={s.total} icon={BrainCircuit} detail="saved assessments" />
            <Metric label="Pending human review" value={s.pending} icon={Clock} detail="AI output not yet checked" />
            <Metric label="Reviewed" value={s.reviewed} icon={ClipboardCheck} tone="text-ok" detail={`${formatNumber(s.approved)} approved · ${formatNumber(s.rejected)} rejected`} />
            <Metric label="Needs reassessment" value={s.reassessment} icon={RotateCcw} tone="text-warn" detail="flagged by a reviewer" />
            <Metric label="Human-approved changes" value={s.confirmedChanges} icon={CheckCircle2} tone="text-ok"
              detail={`${formatNumber(s.confirmedDeteriorated)} deteriorated · ${formatNumber(s.confirmedAmeliorated)} ameliorated`} />
          </section>

          {filtered.length === 0 ? (
            <section className="card">
              <EmptyState icon={X} title="No analyses match these filters" description="Change or clear the filters to see more analyses." />
              <div className="pb-6 text-center">
                <button type="button" onClick={clear} className="focus-ring rounded-lg border border-surface-600 bg-surface-800 px-3 py-1.5 text-xs text-ink-muted hover:text-ink">Clear filters</button>
              </div>
            </section>
          ) : (
            <>
              <div className="grid gap-6 lg:grid-cols-2">
                <Card id="review-dist" title="Review Status" subtitle="Human review of AI analyses" chip={<HumanTag />}>
                  <div className="p-5"><Distribution label="Review status distribution" rows={reviewRows(reviewDistribution(filtered))} total={filtered.length} /></div>
                </Card>
                <Card id="class-dist" title="Condition Classification" subtitle="The schema has classifications, not severity levels">
                  <div className="space-y-5 p-5">
                    <div>
                      <p className="mb-2 flex items-center gap-2 text-xs text-ink-muted"><AiTag /> All analyses ({formatNumber(filtered.length)})</p>
                      <Distribution label="AI-generated classifications" rows={classRows(aiDist)} total={filtered.length} />
                    </div>
                    <div>
                      <p className="mb-2 flex items-center gap-2 text-xs text-ink-muted"><HumanTag /> Approved by a reviewer ({formatNumber(s.approved)})</p>
                      {s.approved
                        ? <Distribution label="Human-approved classifications" rows={classRows(humanDist)} total={s.approved} />
                        : <p className="text-xs text-ink-faint">No analyses have been approved yet, so there are no human-confirmed findings.</p>}
                    </div>
                  </div>
                </Card>
              </div>

              <Card id="weeks" title="Classifications Over Time" subtitle="By the later photograph’s inspection week" chip={<AiTag />}>
                <div className="p-5"><WeekTrend rows={byLaterWeek(filtered)} /></div>
              </Card>

              <Card id="locations" title="By Official Location" subtitle="Select a location to filter" chip={<span className="chip">{locRows.length} location{locRows.length === 1 ? "" : "s"}</span>}>
                <LocationTable rows={locRows} selected={state.location} onPick={(l) => { setShown(PAGE); update({ location: state.location === l ? '' : l }) }} />
              </Card>

              <Card id="timeline" title="Analysis Timeline" subtitle="Newest first · details and review open in AI Condition Analysis"
                chip={<span className="chip">{formatNumber(filtered.length)}</span>}>
                <ol className="relative space-y-3 p-5 before:absolute before:bottom-5 before:left-[1.55rem] before:top-5 before:w-px before:bg-surface-700" aria-label="Analysis timeline">
                  {filtered.slice(0, shown).map((a) => <TimelineEntry key={a.analysis_id} a={a} />)}
                </ol>
                {filtered.length > shown && (
                  <div className="border-t border-surface-700/60 px-5 py-3 text-center">
                    <button type="button" onClick={() => setShown((n) => n + PAGE)} className="focus-ring rounded-lg border border-surface-600 bg-surface-800 px-3 py-1.5 text-xs text-ink-muted hover:text-ink">
                      Show more ({formatNumber(filtered.length - shown)} remaining)
                    </button>
                  </div>
                )}
              </Card>
            </>
          )}
        </>
      )}
    </div>
  )
}
