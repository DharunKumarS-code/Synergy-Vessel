import { formatNumber } from '../utils/format'

// One accent for every card keeps the summary row calm and consistent.
const TONES = {
  accent: 'bg-accent/10 text-accent ring-accent/20',
}

/**
 * value === null/undefined (after loading) renders an explicit "Unavailable" state
 * rather than a fabricated number.
 */
export default function StatCard({ label, value, icon: Icon, detail, loading, error }) {
  const unavailable = !loading && (error || value === null || value === undefined)
  return (
    <article className="card group relative overflow-hidden p-5 transition-colors hover:border-accent/25" aria-busy={loading || undefined}>
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs font-medium uppercase tracking-[0.12em] text-ink-faint">{label}</p>
        <span className={`grid h-10 w-10 shrink-0 place-items-center rounded-lg ring-1 ${TONES.accent}`}>
          <Icon size={19} aria-hidden="true" />
        </span>
      </div>
      <div className="mt-3 min-h-[2.25rem]">
        {loading ? (
          <div className="skeleton mt-1 h-8 w-24" role="status" aria-label={`Loading ${label}`} />
        ) : unavailable ? (
          <p className="text-lg font-medium text-ink-faint" title="This metric could not be derived from the available data">
            Unavailable
          </p>
        ) : (
          <p className="text-3xl font-semibold tabular-nums tracking-tight text-ink">
            {typeof value === 'number' ? formatNumber(value) : value}
          </p>
        )}
      </div>
      <p className="mt-2 min-h-[1rem] text-xs text-ink-muted">{loading ? ' ' : unavailable ? 'Data not available' : detail}</p>
    </article>
  )
}
