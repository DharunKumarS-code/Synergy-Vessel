import { ArrowDownWideNarrow, ArrowUpNarrowWide, Layers } from 'lucide-react'
import { formatDate, formatNumber, formatWeekLabel } from '../../utils/format'
import { UNDATED } from '../../utils/searchResults'
import { DerivedDateBadge } from './Badges'

function Entry({ active, onClick, title, subtitle, count, badge }) {
  return (
    <li className="shrink-0 xl:shrink">
      <button
        type="button"
        onClick={onClick}
        aria-pressed={active}
        className={`focus-ring relative flex w-56 items-start gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors xl:w-full ${
          active
            ? 'border-accent/50 bg-accent/10'
            : 'border-transparent hover:border-surface-600 hover:bg-surface-800'
        }`}
      >
        <span
          className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ring-4 ring-surface-850 ${active ? 'bg-accent' : 'bg-surface-500'}`}
          aria-hidden="true"
        />
        <span className="min-w-0 flex-1">
          <span className={`block text-sm font-medium ${active ? 'text-ink' : 'text-ink-muted'}`}>{title}</span>
          {subtitle && <span className="block truncate text-[11px] text-ink-faint">{subtitle}</span>}
          {badge && <span className="mt-1 block">{badge}</span>}
        </span>
        <span className={`shrink-0 text-xs tabular-nums ${active ? 'text-accent' : 'text-ink-faint'}`}>
          {formatNumber(count)}
        </span>
      </button>
    </li>
  )
}

/** Inspection-date timeline. `selected` is a date key, or '' for all dates. */
export default function DateTimeline({ groups, total, selected, onSelect, sort, onSort }) {
  return (
    <section className="card flex flex-col xl:sticky xl:top-24" aria-labelledby="timeline-title">
      <div className="card-header py-3">
        <div className="min-w-0">
          <h2 id="timeline-title" className="card-title whitespace-nowrap">Timeline</h2>
          <p className="card-subtitle">{groups.length} inspection {groups.length === 1 ? 'date' : 'dates'}</p>
        </div>
        <button
          type="button"
          onClick={() => onSort(sort === 'newest' ? 'oldest' : 'newest')}
          className="focus-ring inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-surface-600 bg-surface-800 px-2.5 py-1.5 text-xs text-ink-muted transition-colors hover:border-accent/50 hover:text-ink"
          aria-label={`Sorted ${sort === 'newest' ? 'newest' : 'oldest'} first. Change sort order`}
        >
          {sort === 'newest' ? <ArrowDownWideNarrow size={13} aria-hidden="true" /> : <ArrowUpNarrowWide size={13} aria-hidden="true" />}
          {sort === 'newest' ? 'Newest' : 'Oldest'}
        </button>
      </div>
      <ol className="flex gap-1 overflow-x-auto p-3 xl:max-h-[calc(100vh-14rem)] xl:flex-col xl:overflow-y-auto xl:overflow-x-visible" aria-label="Inspection dates">
        <Entry
          active={!selected}
          onClick={() => onSelect('')}
          title="All dates"
          subtitle="Every matching appearance"
          count={total}
          badge={null}
        />
        {groups.map((g) => (
          <Entry
            key={g.date}
            active={selected === g.date}
            onClick={() => onSelect(g.date)}
            title={g.date === UNDATED ? 'Undated' : formatDate(g.date, { day: '2-digit', month: 'long', year: 'numeric' })}
            subtitle={`${g.weeks.map(formatWeekLabel).join(', ')} · ${g.count} ${g.count === 1 ? 'photograph' : 'photographs'}`}
            count={g.count}
            badge={g.derived ? <DerivedDateBadge reportDate={g.reportDates.join(', ')} /> : null}
          />
        ))}
      </ol>
      {groups.length > 1 && (
        <p className="hidden items-center gap-1.5 border-t border-surface-700/60 px-5 py-3 text-[11px] text-ink-faint xl:flex">
          <Layers size={12} aria-hidden="true" /> Select a date to view its photographs
        </p>
      )}
    </section>
  )
}
