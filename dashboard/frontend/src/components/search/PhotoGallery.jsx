import { CalendarDays, Copy, Images, MapPin, Sparkles } from 'lucide-react'
import { Fragment } from 'react'
import { formatDate, formatNumber, formatWeekLabel } from '../../utils/format'
import { isDerivedDate } from '../../utils/searchResults'
import { EmptyState } from '../ui/StateViews'
import { DerivedDateBadge, MatchBadge } from './Badges'
import Photo from './Photo'

function PhotoCard({ item, onOpen }) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(item)}
        className="focus-ring group block w-full overflow-hidden rounded-xl border border-surface-700 bg-surface-850 text-left transition-colors hover:border-accent/40"
        aria-label={`Open photograph ${item.image_id ?? item.record_id}, ${formatDate(item.inspection_date)}`}
      >
        <Photo item={item} className="aspect-[4/3] w-full" />
        <div className="space-y-2 p-3.5">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate font-mono text-[13px] font-medium text-ink group-hover:text-accent">
              {item.image_id ?? item.record_id}
            </span>
            <MatchBadge item={item} />
          </div>
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-muted">
            <span className="inline-flex items-center gap-1">
              <CalendarDays size={12} className="text-ink-faint" aria-hidden="true" />
              {item.inspection_date ? formatDate(item.inspection_date) : 'Undated'}
            </span>
            <span className="text-ink-faint">·</span>
            <span>{formatWeekLabel(item.week)}</span>
            {isDerivedDate(item) && <DerivedDateBadge compact reportDate={item.report_date} />}
          </p>
          <p className="flex items-start gap-1.5 text-xs">
            <MapPin size={12} className="mt-0.5 shrink-0 text-ink-faint" aria-hidden="true" />
            {item.official_location_labelled ? (
              <span className="line-clamp-2 text-ink">{item.official_location}</span>
            ) : (
              <span className="italic text-ink-faint">No Official Location</span>
            )}
          </p>
          {item.ai_suggested_location && (
            <p className="flex items-start gap-1.5 text-xs text-ink-muted">
              <Sparkles size={12} className="mt-0.5 shrink-0 text-ink-faint" aria-hidden="true" />
              <span className="line-clamp-2">
                AI: {item.ai_suggested_location}
                {item.location_confidence && <span className="text-ink-faint"> · {item.location_confidence}</span>}
              </span>
            </p>
          )}
          {item.same_photo_appearances > 1 && (
            <p className="flex items-center gap-1.5 text-[11px] text-ink-faint" title="The same photograph (identical SHA-256) appears in other inspection records">
              <Copy size={11} aria-hidden="true" /> Same photo in {item.same_photo_appearances} records
            </p>
          )}
        </div>
      </button>
    </li>
  )
}

/**
 * Responsive, progressively rendered gallery. `limit` items are rendered; when
 * `showDateHeaders`, a heading is inserted whenever the inspection date changes.
 */
export default function PhotoGallery({ items, limit, onLoadMore, onOpen, showDateHeaders = false }) {
  if (!items.length) {
    return (
      <EmptyState
        icon={Images}
        title="No photographs match these filters"
        description="Clear the filters or choose another inspection date."
      />
    )
  }
  const shown = items.slice(0, limit)
  let lastDate = null
  return (
    <div className="space-y-5 p-5">
      <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4" aria-label="Inspection photographs">
        {shown.map((item) => {
          const header = showDateHeaders && item.inspection_date !== lastDate
          lastDate = item.inspection_date
          return (
            <Fragment key={item.record_id}>
              {header && (
                <li className="col-span-full flex items-center gap-3 pt-2 first:pt-0" aria-hidden="true">
                  <span className="text-sm font-semibold text-ink">
                    {item.inspection_date ? formatDate(item.inspection_date) : 'Undated'}
                  </span>
                  <span className="h-px flex-1 bg-surface-700" />
                </li>
              )}
              <PhotoCard item={item} onOpen={onOpen} />
            </Fragment>
          )
        })}
      </ul>
      <div className="flex flex-col items-center gap-2">
        <p className="text-xs text-ink-faint">
          Showing {formatNumber(shown.length)} of {formatNumber(items.length)} photographs
        </p>
        {shown.length < items.length && (
          <button
            type="button"
            onClick={onLoadMore}
            className="focus-ring rounded-lg border border-surface-600 bg-surface-800 px-4 py-2 text-sm text-ink-muted transition-colors hover:border-accent/50 hover:text-ink"
          >
            Load more
          </button>
        )}
      </div>
    </div>
  )
}
