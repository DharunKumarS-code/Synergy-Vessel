import { CalendarClock } from 'lucide-react'
import { matchSource } from '../../utils/searchResults'

const MATCH = {
  official: { label: 'Official', title: 'Matched on Official Location', cls: 'border-accent/40 bg-accent/10 text-accent' },
  ai: { label: 'AI', title: 'Matched on AI Suggested Location', cls: 'border-surface-600 bg-surface-800 text-ink-muted' },
  both: { label: 'Official + AI', title: 'Matched on both Official and AI Suggested Location', cls: 'border-accent/60 bg-accent/15 text-accent-soft' },
}

export function MatchBadge({ item }) {
  const m = MATCH[matchSource(item)]
  if (!m) return null
  return (
    <span title={m.title} className={`inline-flex items-center rounded border px-1.5 py-px text-[10px] font-medium ${m.cls}`}>
      {m.label}
    </span>
  )
}

/** Marks dates inferred from the worksheet name (not independently verified). */
export function DerivedDateBadge({ compact = false, reportDate }) {
  const title = `Date derived from worksheet name${reportDate ? ` (report date: ${reportDate})` : ''}; not independently verified`
  return (
    <span
      title={title}
      className="inline-flex items-center gap-1 rounded border border-warn/30 bg-warn/10 px-1.5 py-px text-[10px] font-medium text-warn"
    >
      <CalendarClock size={10} aria-hidden="true" />
      {compact ? 'Derived' : 'Date derived from worksheet'}
    </span>
  )
}
