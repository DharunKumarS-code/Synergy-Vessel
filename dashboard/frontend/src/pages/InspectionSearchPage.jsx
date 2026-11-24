import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Columns2, MapPin, SearchX } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import SearchBar from '../components/SearchBar'
import { DerivedDateBadge } from '../components/search/Badges'
import DateTimeline from '../components/search/DateTimeline'
import ImageViewer from '../components/search/ImageViewer'
import PhotoGallery from '../components/search/PhotoGallery'
import BrowseLocations from '../components/search/BrowseLocations'
import SearchFilters from '../components/search/SearchFilters'
import { EmptyState, ErrorState, Skeleton } from '../components/ui/StateViews'
import { useVessel } from '../context/VesselContext'
import { useApi } from '../hooks/useApi'
import { useUrlParams } from '../hooks/useUrlParams'
import { searchInspections } from '../services/api'
import { compareHref } from '../utils/compare'
import { formatDate, formatNumber } from '../utils/format'
import {
  SORTS,
  SOURCE_FILTERS,
  UNDATED,
  adjacentDates,
  filterItems,
  groupByDate,
  summariseSearch,
  visibleItems,
} from '../utils/searchResults'

export const PAGE_SIZE = 24

/** Search state lives in the URL so it survives opening/closing the viewer and reloads. */
function useSearchState() {
  const [params, mutate] = useUrlParams()
  const state = {
    location: params.get('location') ?? '',
    date: params.get('date') ?? '',
    week: params.get('week') ?? '',
    source: SOURCE_FILTERS.includes(params.get('source')) ? params.get('source') : 'all',
    sort: SORTS.includes(params.get('sort')) ? params.get('sort') : 'newest',
    image: params.get('image') ?? '',
  }
  const update = useCallback(
    (patch, { replace = true } = {}) =>
      mutate(
        (next) => {
          for (const [k, v] of Object.entries(patch)) {
            if (v === null || v === undefined || v === '' || (k === 'source' && v === 'all') || (k === 'sort' && v === 'newest')) {
              next.delete(k)
            } else next.set(k, v)
          }
        },
        { replace },
      ),
    [mutate],
  )
  return [state, update]
}

function Metric({ label, value, children }) {
  return (
    <div className="min-w-0 px-5 py-4">
      <p className="text-[11px] font-medium uppercase tracking-[0.1em] text-ink-faint">{label}</p>
      <p className="mt-1 truncate text-xl font-semibold tabular-nums text-ink">{value}</p>
      {children && <p className="mt-0.5 truncate text-xs text-ink-muted">{children}</p>}
    </div>
  )
}

function SearchSummary({ location, summary, compareTo }) {
  const first = summary.dates[0]
  const last = summary.dates.at(-1)
  return (
    <section className="card grid grid-cols-2 divide-surface-700/60 sm:grid-cols-3 lg:grid-cols-5 lg:divide-x" aria-label="Search summary">
      <div className="col-span-2 min-w-0 px-5 py-4 sm:col-span-3 lg:col-span-1">
        <p className="text-[11px] font-medium uppercase tracking-[0.1em] text-ink-faint">Searched location</p>
        <p className="mt-1 flex items-center gap-2 truncate text-lg font-semibold text-accent">
          <MapPin size={16} className="shrink-0" aria-hidden="true" />
          <span className="truncate">“{location}”</span>
        </p>
        <p className="mt-0.5 text-xs text-ink-muted">Official and AI locations · partial match</p>
        <Link to={compareTo} className="focus-ring mt-2 inline-flex items-center gap-1 rounded text-xs font-medium text-accent hover:text-accent-soft">
          <Columns2 size={12} aria-hidden="true" /> Compare inspection dates
        </Link>
      </div>
      <Metric label="Matching appearances" value={formatNumber(summary.appearances)}>
        {formatNumber(summary.bySource.official + summary.bySource.both)} official ·{' '}
        {formatNumber(summary.bySource.ai + summary.bySource.both)} AI
      </Metric>
      <Metric label="Unique photographs" value={summary.uniquePhotographs === null ? 'Unavailable' : formatNumber(summary.uniquePhotographs)}>
        Distinct by SHA-256
      </Metric>
      <Metric label="Inspection dates" value={formatNumber(summary.dates.length)}>
        {first ? `${formatDate(first)} – ${formatDate(last)}` : 'No dated records'}
      </Metric>
      <Metric label="Inspection weeks" value={formatNumber(summary.weeks.length)}>
        {summary.missingImages ? `${summary.missingImages} photographs unavailable` : 'All photographs available'}
      </Metric>
    </section>
  )
}

function DateNav({ groups, date, onSelect }) {
  const dated = groups.map((g) => g.date).filter((d) => d !== UNDATED).sort()
  if (!dated.length) return null
  const { older, newer } = adjacentDates(groups, date)
  const btn =
    'focus-ring inline-flex h-8 items-center gap-1 rounded-lg border border-surface-600 bg-surface-800 px-2.5 text-xs text-ink-muted transition-colors hover:border-accent/50 hover:text-ink disabled:pointer-events-none disabled:opacity-35'
  return (
    <nav className="flex flex-wrap items-center gap-1.5" aria-label="Inspection date navigation">
      <button type="button" className={btn} onClick={() => onSelect(dated[0])} disabled={date === dated[0]} aria-label="Oldest inspection date">
        <ChevronsLeft size={14} aria-hidden="true" /> Oldest
      </button>
      <button type="button" className={btn} onClick={() => onSelect(older)} disabled={!older} aria-label="Previous (older) inspection date">
        <ChevronLeft size={14} aria-hidden="true" /> Older
      </button>
      <button type="button" className={btn} onClick={() => onSelect(newer)} disabled={!newer} aria-label="Next (newer) inspection date">
        Newer <ChevronRight size={14} aria-hidden="true" />
      </button>
      <button type="button" className={btn} onClick={() => onSelect(dated.at(-1))} disabled={date === dated.at(-1)} aria-label="Newest inspection date">
        Newest <ChevronsRight size={14} aria-hidden="true" />
      </button>
    </nav>
  )
}

function ResultsSkeleton() {
  return (
    <div className="space-y-6" role="status" aria-label="Searching inspection records">
      <Skeleton className="h-24 w-full rounded-xl" />
      <div className="grid gap-6 xl:grid-cols-[18rem_1fr]">
        <Skeleton className="h-80 rounded-xl" />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="aspect-[4/3] rounded-xl" />
          ))}
        </div>
      </div>
    </div>
  )
}

export default function InspectionSearchPage() {
  const [state, update] = useSearchState()
  const { selectedVessel, loading: vesselLoading, error: vesselError } = useVessel()
  const vesselId = selectedVessel?.vessel_id
  const location = state.location.trim()
  const [query, setQuery] = useState(state.location)
  useEffect(() => setQuery(state.location), [state.location])

  const search = useApi((signal) => searchInspections({ location, vessel: vesselId }, signal), [location, vesselId], {
    enabled: Boolean(location && vesselId),
  })
  const items = useMemo(() => search.data?.items ?? [], [search.data])
  const summary = useMemo(() => summariseSearch(items), [items])
  const filtered = useMemo(() => filterItems(items, state), [items, state.week, state.source]) // eslint-disable-line react-hooks/exhaustive-deps
  const groups = useMemo(() => groupByDate(filtered, state.sort), [filtered, state.sort])
  const visible = useMemo(() => visibleItems(groups, state.date), [groups, state.date])
  const selectedGroup = groups.find((g) => g.date === state.date)

  const [limit, setLimit] = useState(PAGE_SIZE)
  useEffect(() => setLimit(PAGE_SIZE), [location, vesselId, state.date, state.week, state.source, state.sort])

  const viewerIndex = state.image ? visible.findIndex((r) => r.record_id === state.image) : -1

  const runSearch = (q) => q && update({ location: q, date: null, week: null, source: null, image: null }, { replace: false })
  const clearFilters = () => update({ date: null, week: null, source: null, image: null })
  const selectDate = (d) => update({ date: d || null, image: null })

  return (
    <div className="space-y-6">
      <section className="card p-5 sm:p-6" aria-label="Location search">
        <SearchBar value={query} onChange={setQuery} onSubmit={runSearch} />
        <p className="mt-3 text-xs text-ink-faint">
          Searches Official Location and AI Suggested Location · case-insensitive · partial names allowed
        </p>
      </section>

      {!location ? (
        <BrowseLocations vesselId={vesselId} onPick={runSearch} />
      ) : vesselError ? (
        <section className="card">
          <ErrorState error={vesselError} title="Vessel information unavailable" />
        </section>
      ) : !vesselLoading && !vesselId ? (
        <section className="card">
          <EmptyState title="No vessels available" description="There is no vessel inspection data to search." />
        </section>
      ) : vesselLoading || search.loading ? (
        <ResultsSkeleton />
      ) : search.error ? (
        <section className="card">
          <ErrorState error={search.error} title="Search unavailable" onRetry={search.reload} />
        </section>
      ) : !items.length ? (
        <section className="card">
          <EmptyState
            icon={SearchX}
            title={`No inspection records match “${location}”`}
            description="Check the spelling or try a shorter part of the location name, e.g. “deck” or “hold”."
          />
        </section>
      ) : (
        <>
          <SearchSummary location={location} summary={summary} compareTo={compareHref(location, items, { source: state.source })} />
          <SearchFilters
            vesselName={selectedVessel?.vessel_name}
            summary={summary}
            dates={groupByDate(items, state.sort).map((g) => g.date)}
            filters={state}
            onChange={(patch) => update({ ...patch, image: null })}
            onClear={clearFilters}
          />
          <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[18rem_minmax(0,1fr)]">
            <DateTimeline
              groups={groups}
              total={filtered.length}
              selected={state.date}
              onSelect={selectDate}
              sort={state.sort}
              onSort={(sort) => update({ sort })}
            />
            <section className="card min-w-0" aria-labelledby="gallery-title">
              <div className="card-header flex-wrap">
                <div className="min-w-0">
                  <h2 id="gallery-title" className="card-title flex flex-wrap items-center gap-2">
                    {state.date
                      ? state.date === UNDATED
                        ? 'Undated photographs'
                        : formatDate(state.date, { weekday: 'long', day: '2-digit', month: 'long', year: 'numeric' })
                      : 'All inspection dates'}
                    {selectedGroup?.derived && <DerivedDateBadge reportDate={selectedGroup.reportDates.join(', ')} />}
                  </h2>
                  <p className="card-subtitle">
                    {formatNumber(visible.length)} {visible.length === 1 ? 'photograph' : 'photographs'}
                    {!state.date && groups.length > 1 && ` across ${groups.length} dates · ${state.sort} first`}
                  </p>
                </div>
                <DateNav groups={groups} date={state.date} onSelect={selectDate} />
              </div>
              <PhotoGallery
                items={visible}
                limit={limit}
                onLoadMore={() => setLimit((l) => l + PAGE_SIZE)}
                onOpen={(item) => update({ image: item.record_id }, { replace: false })}
                showDateHeaders={!state.date}
              />
            </section>
          </div>
        </>
      )}

      {viewerIndex >= 0 && (
        <ImageViewer
          items={visible}
          index={viewerIndex}
          onIndex={(i) => visible[i] && update({ image: visible[i].record_id })}
          onClose={() => update({ image: null })}
        />
      )}
    </div>
  )
}
