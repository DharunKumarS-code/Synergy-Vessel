import { AlertTriangle, CheckCircle2, Info, ShieldCheck, XCircle } from 'lucide-react'
import { Skeleton } from '../ui/StateViews'

/* Phase 5A: pre-analysis image-pair check. Purely presentational; the parent owns acknowledgement state. */

export const LOCATION_LABELS = {
  OFFICIAL: 'Official Locations match',
  AI_ONLY: 'AI-suggested locations only (unverified)',
  MIXED: 'Official Location vs AI-suggested location',
  CONFLICT: 'Locations conflict',
  INSUFFICIENT: 'Not enough location information',
}

export const SIMILARITY_LABELS = {
  IDENTICAL_FILE: 'Same file (byte-for-byte identical)',
  VISUALLY_IDENTICAL: 'Visually identical content',
  NEAR_DUPLICATE: 'Near-duplicate content',
  NOT_DUPLICATE: 'Separate photographs',
  UNKNOWN: 'Not measured',
}

export function pairState(v) {
  if (!v) return 'unknown'
  if (!v.valid) return 'blocked'
  return v.warnings.length ? 'warnings' : 'passed'
}

const STATE = {
  passed: { label: 'Checks passed', cls: 'border-ok/40 bg-ok/10 text-ok', Icon: ShieldCheck },
  warnings: { label: 'Warnings – review required', cls: 'border-warn/50 bg-warn/10 text-warn', Icon: AlertTriangle },
  blocked: { label: 'Analysis blocked', cls: 'border-bad/40 bg-bad/10 text-bad', Icon: XCircle },
}

function IssueList({ items, Icon, tone, label }) {
  if (!items?.length) return null
  return (
    <ul aria-label={label} className="space-y-1.5">
      {items.map((i) => (
        <li key={i.code + i.message} data-code={i.code} className="flex items-start gap-2 text-xs text-ink">
          <Icon size={14} className={`mt-px shrink-0 ${tone}`} aria-hidden="true" />
          <span>{i.message}</span>
        </li>
      ))}
    </ul>
  )
}

function Fact({ label, children }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] uppercase tracking-wide text-ink-faint">{label}</dt>
      <dd className="mt-0.5 text-xs text-ink">{children}</dd>
    </div>
  )
}

const quality = (side) => (side ? `${side.quality.charAt(0)}${side.quality.slice(1).toLowerCase()}${side.width ? ` (${side.width}×${side.height})` : ''}` : '—')

export default function PairValidation({ validation, acknowledged, onAcknowledge }) {
  if (validation.loading) return <Skeleton className="h-20 w-full rounded-lg" />
  if (validation.error) {
    return (
      <p role="alert" className="flex items-start gap-2 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-ink">
        <XCircle size={14} className="mt-px shrink-0 text-bad" aria-hidden="true" />
        The image-pair check could not be completed, so AI analysis is unavailable for this pair.
      </p>
    )
  }
  const v = validation.data
  if (!v) return null
  const state = pairState(v)
  const { label, cls, Icon } = STATE[state]
  const p = v.perceptual
  return (
    <section aria-label="Image-pair check" className="space-y-3 rounded-lg border border-surface-600 bg-surface-900 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-ink">Image-pair check</h3>
        <span data-testid="pair-state" className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold ${cls}`}>
          <Icon size={12} aria-hidden="true" /> {label}
        </span>
      </div>

      <IssueList items={v.errors} Icon={XCircle} tone="text-bad" label="Blocking problems" />
      <IssueList items={v.warnings} Icon={AlertTriangle} tone="text-warn" label="Warnings" />
      <IssueList items={v.info} Icon={Info} tone="text-ink-faint" label="Notes" />

      <dl className="grid gap-3 sm:grid-cols-3">
        <Fact label="Location evidence">
          {v.location ? LOCATION_LABELS[v.location.match] : '—'}
        </Fact>
        <Fact label="Photograph content">
          {SIMILARITY_LABELS[p.category]}
          {p.phash_distance != null && (
            <span className="block text-[11px] text-ink-faint">pHash {p.phash_distance}/64 · dHash {p.dhash_distance}/64 bits differ</span>
          )}
        </Fact>
        <Fact label="Image quality">
          Earlier: {quality(v.earlier)}
          <span className="block">Later: {quality(v.later)}</span>
        </Fact>
      </dl>
      <p className="text-[11px] text-ink-faint">{v.notice}</p>

      {state === 'warnings' && (
        <label className="flex items-start gap-2 rounded-lg border border-warn/30 bg-warn/5 px-3 py-2 text-xs text-ink">
          <input
            type="checkbox"
            className="focus-ring mt-0.5"
            checked={acknowledged}
            onChange={(e) => onAcknowledge(e.target.checked)}
          />
          <span>I have checked these warnings. Any AI result for this pair will be flagged for human review.</span>
        </label>
      )}
      {state === 'passed' && (
        <p className="flex items-center gap-1.5 text-[11px] text-ink-muted">
          <CheckCircle2 size={12} className="text-ok" aria-hidden="true" /> Suitable for comparison. You chose this pair; matching locations do not guarantee matching camera views.
        </p>
      )}
    </section>
  )
}
