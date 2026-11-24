import { AlertTriangle, CalendarRange, ChevronLeft, ChevronRight, Expand, LayoutGrid, Loader2, MapPin, RotateCcw, SearchX, Ship } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { DuplicateWarning, ResultBadge, isDuplicatePair, isSameFile, resultNote, simpleResult } from '../components/compare/InspectionOutcome'
import { pairState } from '../components/compare/PairValidation'
import PhotoPicker from '../components/compare/PhotoPicker'
import ReviewActions from '../components/compare/ReviewActions'
import { DerivedDateBadge } from '../components/search/Badges'
import ImageViewer from '../components/search/ImageViewer'
import Photo from '../components/search/Photo'
import { EmptyState, ErrorState, Skeleton } from '../components/ui/StateViews'
import { useAuth } from '../context/AuthContext'
import { useVessel } from '../context/VesselContext'
import { useApi } from '../hooks/useApi'
import { useAutoAnalysis, useAutoAnalysisSetting } from '../hooks/useAutoAnalysis'
import { useUrlParams } from '../hooks/useUrlParams'
import { compareImages, getAiStatus, getAnalysisHistory, getLocations, searchInspections, validateImagePair } from '../services/api'
import { COMPARE_MODES, MATCH_MODES, chronologicalGroups, differentLocations, locationKey, officialLocations, officialOf, resolveDates } from '../utils/compare'
import { formatDate } from '../utils/format'
import { SOURCE_FILTERS, filterItems } from '../utils/searchResults'

/*
 * Image Comparison (simplified for managers, 2026-11-21). Primary view: earlier and later images, a duplicate warning
 * when the existing pair check finds the same picture, and the plain inspection result. SV-05: an eligible pair is
 * analysed automatically for reviewers and administrators (see AnalysePanel / useAutoAnalysis), through the existing
 * POST /api/ai-analysis/compare (same request, sign-in, pair check and acknowledgement rules as before; audit and
 * human review stay on the server). A saved
 * assessment awaiting human review can be approved, rejected or sent for reassessment right here (ReviewActions, the same
 * review API as the Reviews page).
 */

const KEYS = ['location', 'match', 'source', 'earlier', 'later', 'a', 'b', 'mode']

// SV-06: the "Analyse automatically" switch (OFF by default, this tab only) is useAutoAnalysisSetting in hooks/useAutoAnalysis.

/** All comparison state lives in the URL (shareable, survives reloads). */
function useCompareState() {
  const [params, mutate] = useUrlParams()
  const get = (k) => params.get(k) ?? ''
  const state = {
    location: get('location'),
    match: MATCH_MODES.includes(get('match')) ? get('match') : 'exact',
    source: SOURCE_FILTERS.includes(get('source')) ? get('source') : 'all',
    earlier: get('earlier'),
    later: get('later'),
    a: get('a'),
    b: get('b'),
    mode: COMPARE_MODES.includes(get('mode')) ? get('mode') : 'side',
  }
  const update = useCallback(
    (patch, { replace = true } = {}) =>
      mutate(
        (next) => {
          for (const [k, v] of Object.entries(patch)) {
            if (!KEYS.includes(k)) continue
            if (!v || (k === 'source' && v === 'all') || (k === 'mode' && v === 'side') || (k === 'match' && v === 'exact')) next.delete(k)
            else next.set(k, v)
          }
        },
        { replace },
      ),
    [mutate],
  )
  return [state, update]
}

/** Inspection date picker for one side; the visible panel heading names the side, the label is for assistive tech. */
function DateSelect({ id, label, value, groups, isAllowed, onChange }) {
  const group = groups.find((g) => g.date === value)
  return (
    <div className="flex min-w-0 flex-col items-end gap-1">
      <label htmlFor={id} className="sr-only">{label}</label>
      <select
        id={id}
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value)}
        className="focus-ring h-9 max-w-full cursor-pointer rounded-lg border border-surface-600 bg-surface-800 px-2.5 text-sm text-ink transition-colors hover:border-accent/50"
      >
        {groups.map((g) => (
          <option key={g.date} value={g.date} disabled={!isAllowed(g.date)}>
            {formatDate(g.date)} · {g.count} {g.count === 1 ? 'photo' : 'photos'}
          </option>
        ))}
      </select>
      {group?.derived && <DerivedDateBadge reportDate={group.reportDates.join(', ')} />}
    </div>
  )
}

const stepBtn =
  'focus-ring grid h-9 w-9 place-items-center rounded-lg border border-surface-600 text-ink-muted transition-colors hover:border-accent/50 hover:text-ink disabled:pointer-events-none disabled:opacity-30'

/** One side of the primary comparison: heading + date, a large photograph, and stepping through that date's photos. */
function ImagePanel({ side, item, items, dateSelect, onStep, onOpen }) {
  const tag = side === 'earlier' ? 'Earlier' : 'Later'
  const index = items.findIndex((r) => r.record_id === item?.record_id)
  return (
    <section className="flex min-w-0 flex-col" aria-label={`${tag} image`}>
      <div className="mb-2 flex items-start justify-between gap-3">
        <h3 className="pt-1.5 text-sm font-semibold uppercase tracking-[0.14em] text-ink">{tag} image</h3>
        {dateSelect}
      </div>
      {item ? (
        <button type="button" onClick={onOpen} className="focus-ring group relative block overflow-hidden rounded-xl border border-surface-700"
          aria-label={`Open ${side} image full size`}>
          <Photo item={item} eager alt={`${tag} image ${item.image_id ?? item.record_id}`} className="aspect-[4/3] w-full bg-surface-950" />
          <span className="absolute right-2 top-2 rounded-lg bg-surface-950/70 p-1.5 text-ink opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden="true">
            <Expand size={16} />
          </span>
        </button>
      ) : (
        <div className="grid aspect-[4/3] place-items-center rounded-xl border border-surface-700 bg-surface-950 text-sm text-ink-faint">No photograph</div>
      )}
      <div className="mt-2 flex min-h-9 items-center justify-between gap-3">
        <p className="min-w-0 truncate text-xs text-ink-muted" aria-label={`${tag} image location`}>
          {item && (officialOf(item) ?? <span className="italic text-ink-faint">No Official Location</span>)}
        </p>
        {items.length > 1 && (
          <div className="flex shrink-0 items-center gap-2">
            <button type="button" className={stepBtn} onClick={() => onStep(-1)} disabled={index <= 0} aria-label={`Previous ${side} photograph`}>
              <ChevronLeft size={16} />
            </button>
            <span className="text-xs tabular-nums text-ink-muted">Photo {index + 1} of {items.length}</span>
            <button type="button" className={stepBtn} onClick={() => onStep(1)} disabled={index >= items.length - 1} aria-label={`Next ${side} photograph`}>
              <ChevronRight size={16} />
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

// A saved assessment with one of these review decisions may be analysed again (same rule as the backend).
const REANALYSABLE = ['REJECTED', 'NEEDS_REASSESSMENT']

/** The plain result for the pair: the latest assessment of exactly these two photographs, or the analysis in progress. */
function InspectionResultCard({ history, analysis, running, elapsed, onReviewed }) {
  const key = simpleResult(analysis)
  const note = analysis ? resultNote(analysis, key) : 'These photos have not been analysed yet.'
  const review = analysis?.review?.status
  const reviewable = key !== 'NOT_ASSESSED' && (review === 'PENDING_REVIEW' || review === 'NEEDS_REASSESSMENT')
  return (
    <section className="card flex flex-col items-center gap-3 p-5 text-center sm:p-6" aria-labelledby="result-title" aria-busy={running}>
      <h2 id="result-title" className="text-sm font-semibold uppercase tracking-[0.16em] text-ink-faint">Inspection Result</h2>
      {running ? (
        <p role="status" aria-label="Analysing photographs" className="flex items-center gap-2 py-3 text-base text-ink">
          <Loader2 size={18} className="animate-spin text-accent" aria-hidden="true" />
          Analysing the selected photos… <span className="tabular-nums text-ink-faint">{elapsed}s</span>
        </p>
      ) : history.loading ? (
        <Skeleton className="h-16 w-64 rounded-2xl" />
      ) : history.error ? (
        <ErrorState error={history.error} title="The inspection result could not be loaded" onRetry={history.reload} compact />
      ) : (
        <>
          <ResultBadge result={key} />
          {note && <p className="text-sm text-ink-muted">{note}</p>}
          {analysis && <ReviewActions analysis={analysis} onReviewed={onReviewed} pendingOnly offer={key !== 'NOT_ASSESSED'} />}
          {reviewable && (
            <Link to={`/reviews?id=${encodeURIComponent(analysis.analysis_id)}`}
              className="focus-ring rounded-md px-2 py-1 text-sm font-medium text-accent hover:text-accent-soft">
              See the details in Reviews
            </Link>
          )}
        </>
      )}
    </section>
  )
}

/**
 * The analysis of exactly the selected pair, and its result. The parent keys this by the pair, so choosing another
 * photograph starts with a clean state and a result can never be shown for photographs it does not belong to.
 *
 * SV-05: there is no "Analyse Images" button. For a reviewer or administrator the pair is analysed automatically
 * (useAutoAnalysis: 2 s debounce, one attempt per pair, no automatic retry) once it is eligible: both photos, pair check
 * passed (not a duplicate or blocked), warnings acknowledged, AI available and no saved assessment yet. A pair with a
 * saved assessment is never analysed automatically; after a Rejected / Needs-reassessment decision the deliberate
 * "Analyse Again" button is the only way to analyse it again. Viewers never start an analysis (the server refuses
 * them too). New results are saved as awaiting review by the server; nothing is approved or rejected here.
 */
function AnalysePanel({ earlier, later, location, validation, duplicate, autoEnabled, onAutoChange }) {
  const status = useApi((s) => getAiStatus(s), [])
  const history = useApi(
    (s) => getAnalysisHistory({ earlier_record_id: earlier.record_id, later_record_id: later.record_id, limit: 1 }, s),
    [earlier.record_id, later.record_id],
  )
  const { user, canReview } = useAuth()
  const [local, setLocal] = useState(null)
  const [running, setRunning] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [error, setError] = useState(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const inFlight = useRef(false) // blocks a second request even before the "running" state has re-rendered

  const analysis = local ?? history.data?.items?.[0] ?? null
  const reanalyse = Boolean(analysis && REANALYSABLE.includes(analysis.review?.status))
  const available = Boolean(status.data?.available)
  const check = pairState(validation.data)
  const warnings = check === 'warnings' ? validation.data.warnings : []
  const mayRun = Boolean(user && canReview)   // SV-02: reviewers and administrators only (enforced by the server too)
  const ready = !status.loading && available && !validation.loading && !validation.error && check !== 'blocked'
    && check !== 'unknown' && !history.loading && !history.error && (warnings.length === 0 || acknowledged)

  const request = async (again) => {
    if (inFlight.current) return false
    inFlight.current = true
    setRunning(true)
    setError(null)
    try {
      const res = await compareImages({
        earlier_record_id: earlier.record_id,
        later_record_id: later.record_id,
        location: location || null,
        earlier_inspection_date: earlier.inspection_date,
        later_inspection_date: later.inspection_date,
        reanalyse: again,
        acknowledge_warnings: warnings.length > 0 && acknowledged,
      })
      if (auto.isCurrent()) setLocal(res.analysis)   // a late reply for a pair no longer shown is ignored
      return true
    } catch (err) {
      if (auto.isCurrent()) {
        setError(err)
        if (err.kind === 'analysis_in_progress' || err.kind === 'already_analysed') history.reload()
      }
      return false
    } finally {
      inFlight.current = false
      if (auto.isCurrent()) setRunning(false)
    }
  }

  const auto = useAutoAnalysis({
    pairKey: `${earlier.record_id}|${later.record_id}`,
    userId: user?.user_id,
    eligible: mayRun && ready && !analysis,
    enabled: autoEnabled,
    start: () => request(false),
  })
  const busy = running || auto.followingEarlierRequest

  useEffect(() => {
    if (!busy) return undefined
    setElapsed(0)
    const t = setInterval(() => setElapsed((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [busy])

  // An automatic request from an earlier visit to this pair has finished: show its saved result.
  const wasFollowing = useRef(false)
  useEffect(() => {
    if (wasFollowing.current && !auto.followingEarlierRequest) history.reload()
    wasFollowing.current = auto.followingEarlierRequest
  }, [auto.followingEarlierRequest, history])

  const failedEarlier = !error && !busy && !analysis && auto.status === 'failed'
  const hint = busy || error || failedEarlier ? null
    : !mayRun ? (analysis ? null : 'You have view-only access. A reviewer can analyse these photos.')
      : status.error || (status.data && !available) ? 'Analysis is not available on this server right now.'
        : validation.error ? 'The photos could not be checked, so they cannot be analysed right now.'
          : check === 'blocked' ? (duplicate ? 'These photos are duplicates, so they cannot be analysed for a change.' : validation.data.errors[0]?.message)
            : analysis ? (reanalyse && warnings.length > 0 && !acknowledged ? 'Tick the box above to analyse these photos again.' : null)
              : warnings.length > 0 && !acknowledged ? 'Tick the box above to analyse these photos.'
                : !autoEnabled ? 'Automatic analysis is off. Turn it on to analyse these photos.'
                  : auto.waiting ? 'These photos will be analysed automatically in a moment…' : null

  return (
    <>
      <div className="flex flex-col items-center gap-2" role="group" aria-label="Analysis of the selected photos">
        {mayRun && (
          <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-muted">
            <input type="checkbox" role="switch" className="focus-ring h-4 w-4 cursor-pointer accent-accent" checked={autoEnabled}
              onChange={(e) => onAutoChange(e.target.checked)} aria-describedby="auto-analysis-help" />
            <span>Analyse automatically</span>
          </label>
        )}
        {mayRun && <span id="auto-analysis-help" className="sr-only">When on, a newly selected pair of photos is analysed after two seconds.</span>}
        {mayRun && warnings.length > 0 && (!analysis || reanalyse) && (
          <div role="note" aria-label="Check before analysing" className="w-full max-w-xl rounded-xl border border-warn/40 bg-warn/5 px-4 py-3 text-sm text-ink">
            <p className="font-medium">Please check before analysing:</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-ink-muted">
              {warnings.map((w) => <li key={w.code}>{w.message}</li>)}
            </ul>
            {duplicate && <p className="mt-1 text-ink-muted">Because the photos look the same, the result may not show a real change in condition.</p>}
            <label className="mt-2 flex items-start gap-2">
              <input type="checkbox" className="focus-ring mt-1" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} disabled={busy} />
              <span>I have checked this. Analyse these photos; the result will be marked for review by a reviewer.</span>
            </label>
          </div>
        )}
        {mayRun && reanalyse && (
          <button type="button" onClick={() => request(true)} disabled={busy || !ready}
            className="focus-ring inline-flex h-11 items-center gap-2 rounded-xl border border-accent/50 bg-surface-800 px-6 text-sm font-semibold text-ink transition-colors hover:border-accent hover:bg-surface-700 disabled:cursor-not-allowed disabled:opacity-40">
            {running ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : <RotateCcw size={16} aria-hidden="true" />}
            {running ? 'Analysing…' : 'Analyse Again'}
          </button>
        )}
        {hint && <p className="text-xs text-ink-muted" aria-live="polite">{hint}</p>}
        {(error || failedEarlier) && (
          <div role="alert" className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-ink">
            <span className="flex items-start gap-2">
              <AlertTriangle size={15} className="mt-0.5 shrink-0 text-bad" aria-hidden="true" />
              {error ? error.message : 'The automatic analysis of these photos did not complete. No result was saved.'}
            </span>
            {!reanalyse && !analysis && mayRun && (
              <button type="button" onClick={() => auto.retry()} disabled={busy}
                className="focus-ring rounded-md border border-surface-600 bg-surface-800 px-2.5 py-1 text-xs font-semibold text-ink hover:border-accent/50 disabled:opacity-40">
                Try Again
              </button>
            )}
          </div>
        )}
      </div>
      <InspectionResultCard history={history} analysis={analysis} running={busy} elapsed={elapsed} onReviewed={setLocal} />
    </>
  )
}

const selectCls =
  'focus-ring h-11 w-full cursor-pointer rounded-xl border border-surface-600 bg-surface-800 pl-10 pr-3 text-[15px] text-ink transition-colors hover:border-accent/50 disabled:cursor-not-allowed disabled:opacity-60'

/** Vessel and location for the comparison: two plain dropdowns (type a letter to jump through the list). */
function SelectionCard({ location, exact, onLocation }) {
  const { vessels, selectedVessel, selectVessel, loading, error, reload } = useVessel()
  const vesselId = selectedVessel?.vessel_id
  const locs = useApi((s) => getLocations({ vessel: vesselId }, s), [vesselId], { enabled: Boolean(vesselId) })
  const options = (locs.data ?? []).filter((l) => !l.is_placeholder)
  // A location from a link (e.g. a broad search) that is not an Official Location name is still shown as chosen.
  const extra = location && !options.some((l) => l.location === location)
  return (
    <section className="card p-4 sm:p-5" aria-label="Location selection">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-1.5">
          <label htmlFor="compare-vessel" className="text-xs font-semibold uppercase tracking-[0.12em] text-ink-faint">Vessel</label>
          {error ? (
            <button type="button" onClick={reload} className="focus-ring h-11 rounded-xl border border-bad/40 bg-bad/10 px-3 text-sm text-bad">Vessels unavailable — retry</button>
          ) : (
            <div className="relative">
              <Ship size={17} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-accent" aria-hidden="true" />
              <select id="compare-vessel" value={vesselId ?? ''} disabled={loading || !vessels.length} className={selectCls}
                onChange={(e) => { selectVessel(e.target.value); onLocation(null) }}>
                {!vessels.length && <option value="">{loading ? 'Loading…' : 'No vessels available'}</option>}
                {vessels.map((v) => <option key={v.vessel_id} value={v.vessel_id}>{v.vessel_name}</option>)}
              </select>
            </div>
          )}
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <label htmlFor="compare-location" className="text-xs font-semibold uppercase tracking-[0.12em] text-ink-faint">Location</label>
          <div className="relative">
            <MapPin size={17} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-accent" aria-hidden="true" />
            <select id="compare-location" value={location} disabled={!vesselId || locs.loading} className={selectCls}
              onChange={(e) => onLocation(e.target.value)}>
              <option value="">{locs.loading ? 'Loading locations…' : 'Choose a location'}</option>
              {extra && <option value={location}>{exact ? location : `Contains “${location}”`}</option>}
              {options.map((l) => <option key={l.location} value={l.location}>{l.location}</option>)}
            </select>
          </div>
          {locs.error && <p className="text-xs text-bad">The list of locations could not be loaded.</p>}
        </div>
      </div>
    </section>
  )
}

const LocationChip = ({ location, count, onPick }) => (
  <button type="button" onClick={() => onPick(location)} className="chip focus-ring py-1 transition-colors hover:border-accent/50 hover:text-ink"
    aria-label={`Compare only ${location}`}>
    <MapPin size={11} aria-hidden="true" /> {location}
    {count !== undefined && <span className="tabular-nums text-ink-faint">{count}</span>}
  </button>
)

/** Contains mode: say plainly when the photographs come from more than one Official Location. */
function MixedLocationsNote({ locations, onPick }) {
  if (locations.length < 2) return null
  return (
    <div role="note" aria-label="Multiple Official Locations" className="flex flex-col gap-2 rounded-lg border border-amber-400/40 bg-amber-400/5 px-3 py-2 text-xs text-ink-muted">
      <p className="flex items-start gap-2">
        <AlertTriangle size={14} className="mt-px shrink-0 text-amber-300" aria-hidden="true" />
        <span>
          These results include <strong className="text-ink">{locations.length} different Official Locations</strong>. Photographs of
          unrelated areas may appear side by side; check each photograph’s location, or compare one location only:
        </span>
      </p>
      <div className="flex flex-wrap gap-1.5">
        {locations.map((l) =>
          l.location ? (
            <LocationChip key={l.location} location={l.location} count={l.count} onPick={onPick} />
          ) : (
            <span key="none" className="chip py-1 italic text-ink-faint">No Official Location <span className="tabular-nums">{l.count}</span></span>
          ),
        )}
      </div>
    </div>
  )
}

/** Exact mode found nothing: offer the Official Locations containing the text, or a broader search. */
function ExactSuggestions({ query, vesselId, onPick, onContains }) {
  const { data } = useApi((s) => getLocations({ vessel: vesselId }, s), [vesselId], { enabled: Boolean(vesselId) })
  const q = locationKey(query)
  const near = (data ?? []).filter((l) => !l.is_placeholder && locationKey(l.location).includes(q))
  return (
    <div className="flex flex-col items-center gap-3 px-5 pb-6">
      {near.length > 0 && (
        <>
          <p className="text-xs text-ink-muted">Official Locations containing “{query}”:</p>
          <div className="flex flex-wrap justify-center gap-2">
            {near.map((l) => <LocationChip key={l.location} location={l.location} count={l.record_count} onPick={onPick} />)}
          </div>
        </>
      )}
      <button type="button" onClick={onContains} className="focus-ring rounded-md px-2 py-1 text-xs font-medium text-accent hover:text-accent-soft">
        Search all locations containing “{query}” instead
      </button>
    </div>
  )
}

export default function ImageComparisonPage() {
  const [state, update] = useCompareState()
  const { selectedVessel, loading: vesselLoading, error: vesselError } = useVessel()
  const vesselId = selectedVessel?.vessel_id
  const location = state.location.trim()

  const exact = state.match === 'exact'
  // Filtering is done by the API (exact = Official Location only); dates and photo grids both use these same items.
  const search = useApi(
    (signal) => searchInspections({ location, vessel: vesselId, match: state.match, includeAi: !exact }, signal),
    [location, vesselId, state.match],
    { enabled: Boolean(location && vesselId) },
  )
  const items = useMemo(() => search.data?.items ?? [], [search.data])
  const filtered = useMemo(() => (exact ? items : filterItems(items, { source: state.source })), [items, exact, state.source])
  const locations = useMemo(() => officialLocations(filtered), [filtered])
  const groups = useMemo(() => chronologicalGroups(filtered), [filtered])
  const dates = useMemo(() => groups.map((g) => g.date), [groups])
  const { earlier, later } = resolveDates(dates, state.earlier, state.later)
  const earlierItems = groups.find((g) => g.date === earlier)?.items ?? []
  const laterItems = groups.find((g) => g.date === later)?.items ?? []
  // The first photograph of each date is shown until another is chosen (arrows, the photo grid or the URL).
  const selA = earlierItems.find((r) => r.record_id === state.a) ?? earlierItems[0] ?? null
  const selB = laterItems.find((r) => r.record_id === state.b) ?? laterItems[0] ?? null
  const pair = selA && selB ? `${selA.record_id}|${selB.record_id}` : null

  // Existing read-only pair check (never calls the AI): drives the duplicate warning and whether analysis is allowed.
  const validation = useApi((s) => validateImagePair(selA.record_id, selB.record_id, s), [pair], { enabled: Boolean(pair) })
  const duplicate = isDuplicatePair(validation.data) || isSameFile(selA, selB)

  const [viewerIndex, setViewerIndex] = useState(-1)
  const [autoAnalysis, setAutoAnalysis] = useAutoAnalysisSetting()
  const [showPickers, setShowPickers] = useState(false)
  const reset = { source: null, earlier: null, later: null, a: null, b: null }
  const chooseLocation = (q) => update({ location: q || null, match: null, ...reset }, { replace: false })
  const chooseExact = (loc) => loc && update({ location: loc, match: 'exact', ...reset }, { replace: false })
  const setMatch = (match) => update({ match, ...reset })
  const step = (list, current, key) => (delta) => {
    const i = list.findIndex((r) => r.record_id === current?.record_id)
    const next = list[i + delta]
    if (next) update({ [key]: next.record_id })
  }
  const morePhotos = earlierItems.length > 1 || laterItems.length > 1

  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <SelectionCard location={location} exact={exact} onLocation={chooseLocation} />
      {!exact && !search.loading && !search.error && <MixedLocationsNote locations={locations} onPick={chooseExact} />}

      {!location ? (
        <section className="card px-6 py-10 text-center">
          <p className="text-base font-medium text-ink">Choose a vessel and a location to compare its earlier and later inspection photographs.</p>
        </section>
      ) : vesselError ? (
        <section className="card"><ErrorState error={vesselError} title="Vessel information unavailable" /></section>
      ) : !vesselLoading && !vesselId ? (
        <section className="card"><EmptyState title="No vessels available" /></section>
      ) : vesselLoading || search.loading ? (
        <div className="space-y-6" role="status" aria-label="Loading comparison data">
          <div className="grid gap-6 md:grid-cols-2">
            <Skeleton className="aspect-[4/3] rounded-xl" />
            <Skeleton className="aspect-[4/3] rounded-xl" />
          </div>
          <Skeleton className="h-32 w-full rounded-xl" />
        </div>
      ) : search.error ? (
        <section className="card"><ErrorState error={search.error} title="Comparison data unavailable" onRetry={search.reload} /></section>
      ) : !items.length ? (
        <section className="card">
          <EmptyState
            icon={SearchX}
            title={`No inspection records match “${location}”`}
            description={exact ? 'Exact location matching looks for this Official Location name only.' : 'Try a shorter part of the location name.'}
          />
          {exact && <ExactSuggestions query={location} vesselId={vesselId} onPick={chooseExact} onContains={() => setMatch('contains')} />}
        </section>
      ) : dates.length < 2 ? (
        <section className="card">
          <EmptyState
            icon={CalendarRange}
            title="At least two inspection dates are needed to compare"
            description={`“${location}” has ${dates.length} dated inspection${dates.length === 1 ? '' : 's'} for this filter. Try another location or location source.`}
          />
        </section>
      ) : (
        <>
          <section className="card space-y-4 p-4 sm:p-5" aria-label="Image comparison">
            <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
              <ImagePanel
                side="earlier"
                item={selA}
                items={earlierItems}
                onStep={step(earlierItems, selA, 'a')}
                onOpen={() => setViewerIndex(0)}
                dateSelect={
                  <DateSelect id="earlier-date" label="Earlier inspection" value={earlier} groups={groups} isAllowed={(d) => d < later}
                    onChange={(d) => update({ earlier: d, later, a: null })} />
                }
              />
              <ImagePanel
                side="later"
                item={selB}
                items={laterItems}
                onStep={step(laterItems, selB, 'b')}
                onOpen={() => setViewerIndex(1)}
                dateSelect={
                  <DateSelect id="later-date" label="Later inspection" value={later} groups={groups} isAllowed={(d) => d > earlier}
                    onChange={(d) => update({ later: d, earlier, b: null })} />
                }
              />
            </div>

            {morePhotos && (
              <div className="flex justify-center text-xs">
                <button type="button" onClick={() => setShowPickers((v) => !v)} aria-expanded={showPickers} aria-controls="photo-choices"
                  className="focus-ring inline-flex items-center gap-1.5 rounded-md px-2 py-1 font-medium text-accent hover:text-accent-soft">
                  <LayoutGrid size={14} aria-hidden="true" /> {showPickers ? 'Hide photo choices' : 'Choose from all photos'}
                </button>
              </div>
            )}

            {showPickers && (
              <div id="photo-choices" className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                <PhotoPicker key={`a-${earlier}`} side="earlier" title="Earlier inspection" date={earlier} items={earlierItems} selectedId={selA?.record_id} onSelect={(id) => update({ a: id })} />
                <PhotoPicker key={`b-${later}`} side="later" title="Later inspection" date={later} items={laterItems} selectedId={selB?.record_id} onSelect={(id) => update({ b: id })} />
              </div>
            )}

            {selA && selB && differentLocations(selA, selB) && (
              <p role="note" aria-label="Different locations" className="flex items-center justify-center gap-2 text-center text-sm text-amber-300">
                <AlertTriangle size={15} className="shrink-0" aria-hidden="true" /> These photos are from different locations and may not show the same area.
              </p>
            )}
            {selA && selB && duplicate && <DuplicateWarning />}
          </section>

          {selA && selB && <AnalysePanel key={pair} earlier={selA} later={selB} location={location} validation={validation} duplicate={duplicate}
            autoEnabled={autoAnalysis} onAutoChange={setAutoAnalysis} />}
        </>
      )}

      {viewerIndex >= 0 && selA && selB && (
        <ImageViewer items={[selA, selB]} index={viewerIndex} onIndex={setViewerIndex} onClose={() => setViewerIndex(-1)} />
      )}
    </div>
  )
}
