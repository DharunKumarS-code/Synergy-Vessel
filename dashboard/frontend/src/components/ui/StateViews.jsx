import { AlertTriangle, Inbox, RefreshCw } from 'lucide-react'

export function Skeleton({ className = '' }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />
}

export function LoadingState({ label = 'Loading…', rows = 3 }) {
  return (
    <div role="status" aria-live="polite" className="space-y-3 p-5">
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className={`h-4 ${i % 2 ? 'w-2/3' : 'w-full'}`} />
      ))}
    </div>
  )
}

export function ErrorState({ error, title = 'Unable to load data', onRetry, compact = false }) {
  // Only the friendly ApiError message is shown; raw backend details never reach the UI.
  const message = error?.message || 'Something went wrong while loading data.'
  return (
    <div role="alert" className={`flex ${compact ? 'items-center gap-3 p-4' : 'flex-col items-center gap-3 p-8 text-center'}`}>
      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-bad/10 text-bad">
        <AlertTriangle size={18} aria-hidden="true" />
      </span>
      <div className={compact ? 'min-w-0 flex-1' : ''}>
        <p className="text-sm font-medium text-ink">{title}</p>
        <p className="mt-0.5 text-xs text-ink-muted">{message}</p>
      </div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="focus-ring inline-flex items-center gap-1.5 rounded-lg border border-surface-600 bg-surface-800 px-3 py-1.5 text-xs font-medium text-ink-muted transition-colors hover:border-accent/50 hover:text-ink"
        >
          <RefreshCw size={13} aria-hidden="true" /> Retry
        </button>
      )}
    </div>
  )
}

export function EmptyState({ title = 'No data available', description, icon: Icon = Inbox }) {
  return (
    <div className="flex flex-col items-center gap-2 p-8 text-center">
      <span className="grid h-10 w-10 place-items-center rounded-full bg-surface-700/60 text-ink-faint">
        <Icon size={18} aria-hidden="true" />
      </span>
      <p className="text-sm font-medium text-ink-muted">{title}</p>
      {description && <p className="max-w-sm text-xs text-ink-faint">{description}</p>}
    </div>
  )
}
