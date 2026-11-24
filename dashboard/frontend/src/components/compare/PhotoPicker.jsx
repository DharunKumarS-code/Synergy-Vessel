import { CheckCircle2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { formatDate, formatWeekLabel } from '../../utils/format'
import { isDerivedDate } from '../../utils/searchResults'
import { DerivedDateBadge, MatchBadge } from '../search/Badges'
import Photo from '../search/Photo'
import { EmptyState } from '../ui/StateViews'

const INITIAL = 12

/** Grid of one date's photographs; the user picks exactly one (nothing is pre-selected). */
export default function PhotoPicker({ side, title, date, items, selectedId, onSelect }) {
  const [showAll, setShowAll] = useState(false)
  const selectedIndex = items.findIndex((r) => r.record_id === selectedId)
  // A selection beyond the first 12 (arrows, URL, full list) always expands the grid so it is never hidden.
  const expanded = showAll || selectedIndex >= INITIAL
  const shown = expanded ? items : items.slice(0, INITIAL)
  const derived = items.some(isDerivedDate)
  const selectedRef = useRef(null)
  const listRef = useRef(null)
  useEffect(() => {
    // Bring the selected photograph into view inside the grid's own scroll area only (the page itself never jumps).
    const el = selectedRef.current
    const list = listRef.current
    if (!el || !list) return
    const lr = list.getBoundingClientRect()
    const er = el.getBoundingClientRect()
    if (er.top < lr.top) list.scrollTop += er.top - lr.top
    else if (er.bottom > lr.bottom) list.scrollTop += er.bottom - lr.bottom
  }, [selectedId, expanded])
  return (
    <section className="card flex min-w-0 flex-col" aria-label={`${title} photographs`}>
      <div className="card-header">
        <div className="min-w-0">
          <h3 className="card-title">{title}</h3>
          <p className="card-subtitle flex flex-wrap items-center gap-2">
            {date ? formatDate(date, { day: '2-digit', month: 'long', year: 'numeric' }) : 'No date selected'}
            {' · '}
            {items.length} {items.length === 1 ? 'photograph' : 'photographs'}
            {derived && <DerivedDateBadge compact reportDate={items[0]?.report_date} />}
          </p>
        </div>
        <span className={`chip ${selectedIndex >= 0 ? 'border-accent/50 text-accent' : ''}`} aria-live="polite">
          {selectedIndex >= 0 ? `Selected ${selectedId} · ${selectedIndex + 1} of ${items.length}` : 'Select one'}
        </span>
      </div>
      {!items.length ? (
        <EmptyState title="No photographs on this date" />
      ) : (
        <>
          <ul ref={listRef} className="grid max-h-[30rem] grid-cols-2 gap-3 overflow-y-auto p-4 sm:grid-cols-3" aria-label={`${title} photograph choices`}>
            {shown.map((item) => {
              const selected = item.record_id === selectedId
              return (
                <li key={item.record_id} ref={selected ? selectedRef : undefined}>
                  <button
                    type="button"
                    onClick={() => onSelect(item.record_id)}
                    aria-pressed={selected}
                    aria-label={`Select photograph ${item.image_id ?? item.record_id} for the ${side} inspection`}
                    className={`focus-ring group relative block w-full overflow-hidden rounded-lg border text-left transition-colors ${
                      selected ? 'border-accent ring-2 ring-accent/40' : 'border-surface-700 hover:border-accent/40'
                    }`}
                  >
                    <Photo item={item} className="aspect-[4/3] w-full" />
                    {selected && (
                      <CheckCircle2 size={20} className="absolute right-2 top-2 rounded-full bg-surface-950 text-accent" aria-hidden="true" />
                    )}
                    <div className="space-y-1 bg-surface-850 p-2">
                      <div className="flex items-center justify-between gap-1">
                        <span className="truncate font-mono text-[12px] text-ink">{item.image_id ?? item.record_id}</span>
                        <MatchBadge item={item} />
                      </div>
                      <p className="truncate text-[11px] text-ink-faint">{formatWeekLabel(item.week)}</p>
                      <p className="line-clamp-2 text-[11px] text-ink-muted" title="Official Location">
                        {item.official_location_labelled ? item.official_location : <span className="italic text-ink-faint">No Official Location</span>}
                      </p>
                      {item.ai_suggested_location && (
                        <p className="truncate text-[11px] text-ink-faint" title="AI Suggested Location">AI: {item.ai_suggested_location}</p>
                      )}
                      {item.activity_identified?.trim() && (
                        <p className="truncate text-[11px] text-ink-faint" title="Activity identified">{item.activity_identified.trim()}</p>
                      )}
                    </div>
                  </button>
                </li>
              )
            })}
          </ul>
          {items.length > INITIAL && (
            <div className="border-t border-surface-700/60 px-4 py-2.5 text-center">
              {selectedIndex >= INITIAL ? (
                <span className="text-xs text-ink-faint">All {items.length} photographs shown · the selected photograph is number {selectedIndex + 1}</span>
              ) : (
                <button
                  type="button"
                  onClick={() => setShowAll((v) => !v)}
                  className="focus-ring rounded-md px-2 py-1 text-xs text-ink-muted hover:text-ink"
                >
                  {showAll ? 'Show fewer' : `Show all ${items.length} photographs`}
                </button>
              )}
            </div>
          )}
        </>
      )}
    </section>
  )
}
