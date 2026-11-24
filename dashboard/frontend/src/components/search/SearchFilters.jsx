import { FilterX, Ship } from 'lucide-react'
import { formatDate, formatWeekLabel } from '../../utils/format'
import { UNDATED } from '../../utils/searchResults'
import SourceToggle, { sourceCounts } from './SourceToggle'

function Select({ label, value, onChange, children }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] font-medium uppercase tracking-[0.1em] text-ink-faint">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="focus-ring h-9 min-w-[10rem] cursor-pointer rounded-lg border border-surface-600 bg-surface-800 px-3 text-sm text-ink transition-colors hover:border-accent/50"
      >
        {children}
      </select>
    </label>
  )
}

export default function SearchFilters({ vesselName, summary, dates, filters, onChange, onClear }) {
  const active = Boolean(filters.date || filters.week || filters.source !== 'all')
  return (
    <section className="card flex flex-wrap items-end gap-x-5 gap-y-4 px-5 py-4" aria-label="Search filters">
      <div className="flex flex-col gap-1">
        <span className="text-[11px] font-medium uppercase tracking-[0.1em] text-ink-faint">Vessel</span>
        <span className="inline-flex h-9 items-center gap-2 rounded-lg border border-surface-700 px-3 text-sm text-ink-muted" title="Change the vessel in the top bar">
          <Ship size={14} className="text-accent" aria-hidden="true" />
          {vesselName ?? '—'}
        </span>
      </div>

      <Select label="Inspection date" value={filters.date} onChange={(v) => onChange({ date: v })}>
        <option value="">All dates</option>
        {dates.map((d) => (
          <option key={d} value={d}>
            {d === UNDATED ? 'Undated' : formatDate(d)}
          </option>
        ))}
      </Select>

      <Select label="Inspection week" value={filters.week} onChange={(v) => onChange({ week: v })}>
        <option value="">All weeks</option>
        {summary.weeks.map((w) => (
          <option key={w} value={w}>
            {formatWeekLabel(w)}
          </option>
        ))}
      </Select>

      <SourceToggle value={filters.source} onChange={(source) => onChange({ source })} counts={sourceCounts(summary)} />

      <button
        type="button"
        onClick={onClear}
        disabled={!active}
        className="focus-ring ml-auto inline-flex h-9 items-center gap-1.5 rounded-lg border border-surface-600 px-3 text-xs text-ink-muted transition-colors hover:border-accent/50 hover:text-ink disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-surface-600 disabled:hover:text-ink-muted"
      >
        <FilterX size={14} aria-hidden="true" /> Clear filters
      </button>
    </section>
  )
}
