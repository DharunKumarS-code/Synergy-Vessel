import { Check, CheckCheck, Expand, GitMerge, History, Loader2, MoveRight, Pencil, Scissors, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useApi } from '../../hooks/useApi'
import { decideViewpoint, getViewpointHistory, getViewpointReview } from '../../services/api'
import { statusWords } from '../../utils/viewpoints'
import ImageViewer from '../search/ImageViewer'
import Photo from '../search/Photo'
import { EmptyState, ErrorState, Skeleton } from '../ui/StateViews'

/*
 * SV-08 -- administrator review of the physical viewpoint mapping. The generator only proposes; every approval,
 * rejection or correction made here is checked and recorded by the server (admin only, revision check, audit log,
 * mapping history). Approving a pair is what allows the AI to compare those two photographs.
 */

const field = 'focus-ring h-9 rounded-lg border border-surface-600 bg-surface-800 px-2.5 text-sm text-ink'
const btn = 'focus-ring inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-sm font-semibold disabled:opacity-50'
const PAGE = 12
const TONE = { approved: 'text-ok border-ok/40', needs_review: 'text-amber-300 border-amber-400/40', proposed: 'text-ink-muted border-surface-600', rejected: 'text-bad border-bad/40' }
const METHOD = {
  same_serial_within_layout: 'Same serial within one workbook layout',
  identical_file: 'Identical file (repeated photograph)',
  proposed_cross_layout: 'Proposed across a workbook layout change',
  reviewed: 'Placed by an administrator',
}
const when = (iso) => (iso ? new Date(iso).toLocaleString('en-GB') : '—')

const StatusChip = ({ status }) => (
  <span className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${TONE[status] ?? TONE.proposed}`}>
    {statusWords(status)}
  </span>
)

/** One photograph of a match: a fixed-size thumbnail (click for full size) beside its evidence, so several matches fit
 *  on a screen. The thumbnail is the same lazily loaded photograph URL as everywhere else (fetched once per card). */
function MemberCard({ side, m, flagText, onOpen }) {
  return (
    <figure className="flex min-w-0 flex-col gap-3 sm:flex-row" aria-label={`${side} photograph ${m.record_id}`}>
      <button type="button" onClick={onOpen} aria-label={`Open ${side.toLowerCase()} photograph ${m.record_id} full size`}
        className="focus-ring group relative h-[9.75rem] w-[13rem] shrink-0 overflow-hidden rounded-lg border border-surface-700">
        <Photo item={m} alt={`${side} photograph ${m.record_id}`} className="h-full w-full" />
        <span className="absolute right-1.5 top-1.5 rounded-md bg-surface-950/70 p-1 text-ink opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden="true">
          <Expand size={13} />
        </span>
      </button>
      <figcaption className="min-w-0 flex-1 space-y-1 text-xs text-ink-muted">
        <p className="flex flex-wrap items-center gap-2 text-ink">
          <span className="font-semibold">Week {m.week}</span>
          <span>#{m.serial ?? '—'}</span>
          <span className="tabular-nums text-ink-faint">{m.record_id}</span>
          <StatusChip status={m.status} />
        </p>
        <p>Official Location: <span className="text-ink">{m.official_location ?? '—'}</span>
          {m.ai_suggested_location && <span className="text-ink-faint"> · AI hint: {m.ai_suggested_location}</span>}</p>
        <p>{METHOD[m.method] ?? m.method}</p>
        {(m.evidence ?? []).length > 0 && (
          <ul className="list-disc space-y-0.5 pl-4 text-ink-faint" aria-label="Evidence">
            {m.evidence.map((e) => <li key={e}>{e}</li>)}
          </ul>
        )}
        {(m.flags ?? []).length > 0 && (
          <ul className="space-y-0.5 text-amber-300" aria-label="Flags">
            {m.flags.map((f) => <li key={f}>⚑ {flagText?.[f] ?? f}</li>)}
          </ul>
        )}
        {m.reviewed_by && <p className="text-ink-faint">Decided by {m.reviewed_by} · {when(m.reviewed_at)}{m.notes ? ` · “${m.notes}”` : ''}</p>}
      </figcaption>
    </figure>
  )
}

function MatchCard({ match, others, flagText, busy, onDecide }) {
  const [notes, setNotes] = useState('')
  const [more, setMore] = useState(false)
  const [target, setTarget] = useState('')
  const [name, setName] = useState(match.display_name)
  const [viewerIndex, setViewerIndex] = useState(-1)
  const e = match.earlier
  const l = match.later
  const act = (action, extra = {}) => onDecide({ action, viewpoint_id: match.viewpoint_id, notes: notes.trim() || null, ...extra })
  const ids = [e.record_id, l.record_id]
  return (
    <article className="card space-y-3 p-4" aria-label={`Match ${e.record_id} to ${l.record_id}`}>
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-ink">{match.display_name}</h3>
          <p className="text-xs text-ink-faint">{match.viewpoint_id} · {match.official_location}</p>
        </div>
        <div className="flex items-center gap-2">
          {match.repeated_photograph && <span className="chip py-0.5 text-[11px]">Same file – not retaken</span>}
          <StatusChip status={match.status} />
        </div>
      </header>
      <div className="grid gap-4 xl:grid-cols-2">
        <MemberCard side="Earlier" m={e} flagText={flagText} onOpen={() => setViewerIndex(0)} />
        <MemberCard side="Later" m={l} flagText={flagText} onOpen={() => setViewerIndex(1)} />
      </div>
      {viewerIndex >= 0 && <ImageViewer items={[e, l]} index={viewerIndex} onIndex={setViewerIndex} onClose={() => setViewerIndex(-1)} />}
      <div className="flex flex-col gap-2 border-t border-surface-700/60 pt-3">
        <label className="text-xs text-ink-faint" htmlFor={`notes-${e.record_id}-${l.record_id}`}>Notes (required when changing an approved photograph, moving or joining)</label>
        <textarea id={`notes-${e.record_id}-${l.record_id}`} value={notes} onChange={(ev) => setNotes(ev.target.value)} maxLength={500} rows={2}
          className="focus-ring w-full rounded-lg border border-surface-600 bg-surface-800 px-3 py-2 text-sm text-ink" />
        <div className="flex flex-wrap gap-2">
          <button type="button" disabled={busy || (e.status === 'approved' && l.status === 'approved')} onClick={() => act('approve', { record_ids: ids })}
            className={`${btn} bg-accent text-surface-950 hover:bg-accent-soft`}><Check size={15} aria-hidden="true" /> Approve same viewpoint</button>
          <button type="button" disabled={busy} onClick={() => act('reject', { record_ids: [l.record_id] })}
            className={`${btn} border border-bad/50 text-bad hover:bg-bad/10`}><X size={15} aria-hidden="true" /> Reject later photograph</button>
          <button type="button" disabled={busy} onClick={() => act('reject', { record_ids: [e.record_id] })}
            className={`${btn} border border-bad/50 text-bad hover:bg-bad/10`}><X size={15} aria-hidden="true" /> Reject earlier photograph</button>
          <button type="button" onClick={() => setMore((v) => !v)} aria-expanded={more}
            className={`${btn} border border-surface-600 text-ink-muted hover:text-ink`}><Pencil size={15} aria-hidden="true" /> Correct…</button>
        </div>
        {more && (
          <div className="grid gap-3 rounded-lg border border-surface-700 bg-surface-900/60 p-3 text-sm lg:grid-cols-3" aria-label="Corrections">
            <div className="flex flex-col gap-1.5">
              <label htmlFor={`target-${l.record_id}`} className="text-xs text-ink-faint">Move the later photograph, or join, with viewpoint</label>
              <select id={`target-${l.record_id}`} value={target} onChange={(ev) => setTarget(ev.target.value)} className={field}>
                <option value="">Choose a viewpoint</option>
                {others.filter((o) => o.viewpoint_id !== match.viewpoint_id).map((o) => (
                  <option key={o.viewpoint_id} value={o.viewpoint_id}>{o.display_name} ({o.viewpoint_id})</option>
                ))}
              </select>
              <div className="flex flex-wrap gap-2">
                <button type="button" disabled={busy || !target} onClick={() => act('move', { record_ids: [l.record_id], target_viewpoint_id: target })}
                  className={`${btn} border border-surface-600 text-ink hover:border-accent/50`}><MoveRight size={15} aria-hidden="true" /> Move</button>
                <button type="button" disabled={busy || !target} onClick={() => act('join', { target_viewpoint_id: target })}
                  className={`${btn} border border-surface-600 text-ink hover:border-accent/50`}><GitMerge size={15} aria-hidden="true" /> Join into this viewpoint</button>
              </div>
            </div>
            <div className="flex flex-col gap-1.5">
              <span className="text-xs text-ink-faint">Split: Week {l.week} onwards becomes a separate viewpoint</span>
              <button type="button" disabled={busy} onClick={() => act('split', { at_week: l.week })}
                className={`${btn} border border-surface-600 text-ink hover:border-accent/50`}><Scissors size={15} aria-hidden="true" /> Split from Week {l.week}</button>
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor={`name-${l.record_id}`} className="text-xs text-ink-faint">Display name</label>
              <input id={`name-${l.record_id}`} value={name} onChange={(ev) => setName(ev.target.value)} maxLength={120} className={field} />
              <button type="button" disabled={busy || !name.trim() || name.trim() === match.display_name} onClick={() => act('rename', { display_name: name.trim() })}
                className={`${btn} border border-surface-600 text-ink hover:border-accent/50`}><Pencil size={15} aria-hidden="true" /> Rename</button>
            </div>
          </div>
        )}
      </div>
    </article>
  )
}

function HistoryList({ revision }) {
  const h = useApi((s) => getViewpointHistory({ limit: 25 }, s), [revision])
  if (h.loading) return <Skeleton className="h-24 w-full rounded-xl" />
  if (h.error) return <ErrorState error={h.error} title="History unavailable" onRetry={h.reload} compact />
  const items = h.data?.items ?? []
  if (!items.length) return <p className="p-4 text-sm text-ink-muted">No decisions yet. Every approval and correction is listed here and in the audit log.</p>
  return (
    <ol className="divide-y divide-surface-700/60 text-xs text-ink-muted" aria-label="Viewpoint decisions">
      {items.map((x) => (
        <li key={x.seq} className="px-4 py-2">
          <span className="text-ink">{x.action.replaceAll('_', ' ')}</span> · {x.viewpoint_ids.join(', ')}
          {x.record_ids?.length > 0 && ` · ${x.record_ids.join(', ')}`} · {x.by?.username} · {when(x.at)}
          {x.notes && <span className="text-ink-faint"> · “{x.notes}”</span>}
        </li>
      ))}
    </ol>
  )
}

export default function ViewpointReview({ initialLocation = '' }) {
  const [location, setLocation] = useState(initialLocation)
  const [week, setWeek] = useState('')
  const [status, setStatus] = useState('needs_review')
  const [shown, setShown] = useState(PAGE)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)
  const all = useApi((s) => getViewpointReview({}, s), [])
  const review = useApi((s) => getViewpointReview({ location, week, status }, s), [location, week, status])
  const locations = useMemo(() => [...new Set((all.data?.viewpoints ?? []).map((v) => v.official_location))].sort((a, b) => a.localeCompare(b)), [all.data])
  const weeks = useMemo(() => [...new Set((all.data?.viewpoints ?? []).flatMap((v) => v.members.map((m) => m.week)))].sort((a, b) => a - b), [all.data])
  // Move/Join targets: only viewpoints that still hold an active photograph. A split or join leaves the emptied
  // viewpoint behind with only rejected (historical) members; it stays in the mapping and its history, but is not offered.
  const others = useMemo(() => (all.data?.viewpoints ?? []).filter((v) => (!location || v.official_location === location)
    && (v.members ?? []).some((m) => m.status !== 'rejected')), [all.data, location])

  const decide = async (body) => {
    setBusy(true)
    setMessage(null)
    try {
      const res = await decideViewpoint({ ...body, revision: review.data.revision })
      setMessage({ ok: true, text: `Saved (${res.history_entry.action.replaceAll('_', ' ')}). Mapping revision ${res.revision}.` })
      review.reload()
      all.reload()
    } catch (err) {
      setMessage({ ok: false, text: err.message })
      if (err.kind === 'mapping_changed') review.reload()
    } finally {
      setBusy(false)
    }
  }

  if (all.error && all.error.kind === 'mapping_unavailable') {
    return (
      <section className="card p-5" aria-label="Viewpoint mapping">
        <h2 className="card-title">Viewpoint mapping</h2>
        <p className="mt-2 text-sm text-ink-muted">{all.error.message}</p>
      </section>
    )
  }
  const s = review.data?.summary
  const matches = review.data?.matches ?? []
  const vps = review.data?.viewpoints ?? []
  const bulkable = vps.filter((v) => (v.counts?.proposed ?? 0) > 0 && !(v.counts?.needs_review))
  return (
    <div className="space-y-5">
      <section className="card" aria-labelledby="vp-title">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-surface-700/60 px-5 py-4">
          <h2 id="vp-title" className="card-title">Viewpoint mapping review</h2>
          {s && (
            <p className="text-xs text-ink-muted" aria-label="Mapping summary">
              {s.viewpoints} viewpoints · {s.members.approved} approved · {s.members.needs_review} need review · {s.members.proposed} proposed ·{' '}
              {s.members.rejected} rejected · {s.unlinked_records} unlinked · revision {review.data.revision}
            </p>
          )}
        </div>
        <div className="grid gap-3 p-5 sm:grid-cols-3" role="group" aria-label="Filters">
          <label className="flex flex-col gap-1 text-xs text-ink-faint">Location
            <select value={location} onChange={(e) => { setLocation(e.target.value); setShown(PAGE) }} className={field}>
              <option value="">All locations</option>
              {locations.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-ink-faint">Week
            <select value={week} onChange={(e) => { setWeek(e.target.value); setShown(PAGE) }} className={field}>
              <option value="">All weeks</option>
              {weeks.map((w) => <option key={w} value={w}>Week {w}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-ink-faint">Status
            <select value={status} onChange={(e) => { setStatus(e.target.value); setShown(PAGE) }} className={field}>
              <option value="needs_review">Needs review</option>
              <option value="proposed">Proposed</option>
              <option value="approved">Approved</option>
              <option value="">All</option>
            </select>
          </label>
        </div>
        <p className="px-5 pb-4 text-xs text-ink-faint">
          The generator only proposes matches. Approve two photographs only when they show the same physical view; the AI compares approved
          pairs only. Identical files are repeats (not retaken) and are never analysed.
        </p>
      </section>

      {message && (
        <p role={message.ok ? 'status' : 'alert'} className={`rounded-lg border px-3 py-2 text-sm ${message.ok ? 'border-ok/40 text-ok' : 'border-bad/40 bg-bad/10 text-ink'}`}>
          {message.text}
        </p>
      )}

      {bulkable.length > 0 && status !== 'approved' && (
        <section className="card flex flex-wrap items-center gap-2 p-4 text-sm" aria-label="Bulk approval">
          <span className="text-ink-muted">Viewpoints whose remaining photographs are all unflagged proposals:</span>
          {bulkable.slice(0, 8).map((v) => (
            <button key={v.viewpoint_id} type="button" disabled={busy} onClick={() => decide({ action: 'approve_all_proposed', viewpoint_id: v.viewpoint_id })}
              className={`${btn} border border-accent/50 text-ink hover:border-accent`}>
              <CheckCheck size={15} aria-hidden="true" /> Approve {v.display_name}
            </button>
          ))}
        </section>
      )}

      {review.loading ? (
        <Skeleton className="h-64 w-full rounded-xl" />
      ) : review.error ? (
        <section className="card"><ErrorState error={review.error} title="The viewpoint mapping could not be loaded" onRetry={review.reload} /></section>
      ) : matches.length === 0 ? (
        <section className="card"><EmptyState title="Nothing to review for this filter" description="Try another status, location or week." /></section>
      ) : (
        <div className="space-y-4" aria-busy={busy}>
          <p className="text-xs text-ink-muted">{matches.length} match{matches.length === 1 ? '' : 'es'} (consecutive photographs of one viewpoint)</p>
          {matches.slice(0, shown).map((m) => (
            <MatchCard key={`${m.earlier.record_id}|${m.later.record_id}`} match={m} others={others} flagText={review.data.flag_text} busy={busy} onDecide={decide} />
          ))}
          {shown < matches.length && (
            <div className="flex justify-center">
              <button type="button" onClick={() => setShown((n) => n + PAGE)} className={`${btn} border border-surface-600 text-ink`}>Show more</button>
            </div>
          )}
          {busy && <p role="status" className="flex items-center gap-2 text-sm text-ink-muted"><Loader2 size={15} className="animate-spin" aria-hidden="true" /> Saving…</p>}
        </div>
      )}

      <section className="card" aria-labelledby="vp-history">
        <h2 id="vp-history" className="card-title flex items-center gap-2 border-b border-surface-700/60 px-5 py-4"><History size={16} aria-hidden="true" /> Decision history</h2>
        <HistoryList revision={review.data?.revision} />
      </section>
    </div>
  )
}
