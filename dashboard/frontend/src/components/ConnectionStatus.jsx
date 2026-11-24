const STYLES = {
  online: { dot: 'bg-ok', ring: 'bg-ok/30', text: 'API connected' },
  offline: { dot: 'bg-bad', ring: 'bg-bad/30', text: 'API unavailable' },
  checking: { dot: 'bg-warn', ring: 'bg-warn/30', text: 'Connecting…' },
}

export default function ConnectionStatus({ status, compact = false }) {
  const s = STYLES[status] ?? STYLES.checking
  return (
    <span
      className="inline-flex items-center gap-2 text-xs text-ink-muted"
      role="status"
      aria-label={s.text}
      title={s.text}
    >
      <span className="relative flex h-2.5 w-2.5">
        {status === 'online' && <span className={`absolute inline-flex h-full w-full animate-ping rounded-full ${s.ring}`} />}
        <span className={`relative inline-flex h-2.5 w-2.5 rounded-full ${s.dot}`} />
      </span>
      {!compact && <span>{s.text}</span>}
    </span>
  )
}
