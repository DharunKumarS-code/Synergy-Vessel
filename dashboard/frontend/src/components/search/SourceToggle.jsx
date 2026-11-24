import { useId } from 'react'
import { formatNumber } from '../../utils/format'

export const SOURCE_LABELS = { all: 'All sources', official: 'Official', ai: 'AI', both: 'Both' }

/** Counts per source filter (official/ai include records matching both fields). */
export const sourceCounts = (summary) => ({
  all: summary.appearances,
  official: summary.bySource.official + summary.bySource.both,
  ai: summary.bySource.ai + summary.bySource.both,
  both: summary.bySource.both,
})

/** Segmented control choosing which location field a match must come from. */
export default function SourceToggle({ value, onChange, counts, label = 'Location source' }) {
  const id = useId()
  return (
    <div className="flex flex-col gap-1">
      <span id={id} className="text-[11px] font-medium uppercase tracking-[0.1em] text-ink-faint">
        {label}
      </span>
      <div role="radiogroup" aria-labelledby={id} className="inline-flex h-9 w-fit rounded-lg border border-surface-600 bg-surface-800 p-0.5">
        {Object.keys(SOURCE_LABELS).map((key) => (
          <button
            key={key}
            type="button"
            role="radio"
            aria-checked={value === key}
            onClick={() => onChange(key)}
            className={`focus-ring whitespace-nowrap rounded-md px-3 text-xs transition-colors ${
              value === key ? 'bg-accent font-semibold text-surface-950' : 'text-ink-muted hover:text-ink'
            }`}
          >
            {SOURCE_LABELS[key]} <span className="tabular-nums opacity-70">{formatNumber(counts[key])}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
