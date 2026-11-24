import { AlertTriangle, CheckCircle2, FileSpreadsheet, History, Info, Loader2, Upload, X, XCircle } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import AiLocationDetails from '../components/AiLocationDetails'
import FileName from '../components/ui/FileName'
import { EmptyState, ErrorState, Skeleton } from '../components/ui/StateViews'
import { ROLE_LABELS, useAuth } from '../context/AuthContext'
import { useApi } from '../hooks/useApi'
import { useUrlParams } from '../hooks/useUrlParams'
import { confirmImport, decideLocation, getImport, getImportHistory, previewImport, reinstateImport, verifyImport, withdrawImport } from '../services/api'
import { formatDate, formatNumber } from '../utils/format'

/* Phase 7 -- Excel register import: choose -> preview (nothing saved) -> confirm -> history. */

export const ROW_STATUS = {
  ready: { label: 'Ready', cls: 'border-ok/40 bg-ok/10 text-ok' },
  imported: { label: 'Imported', cls: 'border-ok/40 bg-ok/10 text-ok' },
  withdrawn: { label: 'Withdrawn', cls: 'border-warn/40 bg-warn/10 text-warn' },
  duplicate: { label: 'Duplicate', cls: 'border-warn/50 bg-warn/10 text-warn' },
  skipped: { label: 'Skipped', cls: 'border-surface-600 bg-surface-800 text-ink-muted' },
  invalid: { label: 'Failed', cls: 'border-bad/40 bg-bad/10 text-bad' },
}
const FILTERS = [
  ['all', 'All'], ['ok', 'Ready / imported'], ['duplicate', 'Duplicates'], ['invalid', 'Failed'], ['skipped', 'Skipped'],
  ['location', 'Location review'],
]
/* Phase 9 -- location intelligence labels */
const reviewOf = (r) => r.effective_location?.review_status ?? r.location_intel?.review_status
const matches = (r, f) => f === 'all' || (f === 'ok' ? ['ready', 'imported'].includes(r.status)
  : f === 'location' ? ['ready', 'imported'].includes(r.status) && reviewOf(r) === 'pending' : r.status === f)
export const LOCATION_METHOD = {
  exact: 'Exact match', normalized: 'Standardised by rules', managed_synonym: 'Managed synonym', synonym: 'Known synonym', spelling: 'Spelling variant',
  partial: 'Partial match', ambiguous: 'Ambiguous', unknown: 'Unknown location', missing: 'No location',
  ai_suggestion: 'AI suggestion', approved_suggestion: 'Approved suggestion', manual: 'Entered manually', original: 'Original kept',
}
export const LOCATION_REVIEW = {
  not_required: { label: 'No review needed', cls: 'text-ink-faint' },
  auto_applied: { label: 'Applied automatically', cls: 'text-ok' },
  pending: { label: 'Needs review', cls: 'text-warn' },
  accepted: { label: 'Suggestion accepted', cls: 'text-ok' },
  rejected: { label: 'Suggestion rejected', cls: 'text-ink-muted' },
  kept_original: { label: 'Original kept', cls: 'text-ink-muted' },
  manual: { label: 'Set manually', cls: 'text-ok' },
}
const pct = (c) => (c == null ? null : `${Math.round(c * 100)}%`)
/* Phase 9.4: AI confidence is self-reported, so it is shown as a band capped at "medium", never as a percentage. */
const aiBand = (c) => (c == null ? null : c < 0.6 ? 'low' : 'medium')
/** The rule's own candidate (kept next to any AI suggestion) and whether the two disagree. */
export function locationOptions(li) {
  const rule = li.normalized_location && li.normalized_location !== li.original_location ? li.normalized_location : null
  const ai = li.ai_suggestion || null
  return { rule, ai, disagree: Boolean(rule && ai && rule !== ai) }
}

function LocationActions({ row, onDecide }) {
  const [manual, setManual] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const li = row.location_intel || {}
  const opts = locationOptions(li)
  const suggestion = opts.ai || opts.rule
  const run = async (action, location) => {
    setBusy(true)
    setError(null)
    try {
      await onDecide(row.row_number, action, location)
      setManual(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }
  const btn = 'focus-ring rounded border border-surface-600 px-1.5 py-0.5 text-[10px] text-ink-muted hover:text-ink disabled:opacity-40'
  return (
    <div className="mt-1 space-y-1" role="group" aria-label={`Location decision for row ${row.row_number}`}>
      <div className="flex flex-wrap gap-1">
        {suggestion && !opts.disagree && <button type="button" className={btn} disabled={busy} onClick={() => run('accept')}>Accept</button>}
        {opts.disagree && <button type="button" className={btn} disabled={busy} onClick={() => run('accept', opts.rule)}>Accept rule candidate</button>}
        {opts.disagree && <button type="button" className={btn} disabled={busy} onClick={() => run('accept', opts.ai)}>Accept AI suggestion</button>}
        {suggestion && <button type="button" className={btn} disabled={busy} onClick={() => run('reject')}>Reject</button>}
        <button type="button" className={btn} disabled={busy} onClick={() => run('keep_original')}>Keep original</button>
        <button type="button" className={btn} disabled={busy} onClick={() => setManual(manual === null ? '' : null)}>Enter location</button>
      </div>
      {manual !== null && (
        <div className="flex gap-1">
          <input aria-label={`Location for row ${row.row_number}`} value={manual} maxLength={120} onChange={(e) => setManual(e.target.value)}
            className="focus-ring h-6 min-w-0 flex-1 rounded border border-surface-600 bg-surface-800 px-1.5 text-[11px] text-ink" />
          <button type="button" className={btn} disabled={busy || manual.trim().length < 2} onClick={() => run('manual', manual)}>Save</button>
        </div>
      )}
      {error && <p role="alert" className="text-[10px] text-bad">{error}</p>}
    </div>
  )
}

/** Original Excel text, the location shown, how it was reached, AI suggestion + confidence, review status. */
function LocationCell({ row, onDecide }) {
  const li = row.location_intel
  if (!li) return row.official_location ?? <span className="text-ink-faint">Not labelled</span>
  const eff = row.effective_location
  const shown = eff ? eff.location : li.applied_location
  const method = (eff && eff.method) || li.normalization_method
  const review = LOCATION_REVIEW[reviewOf(row)] ?? LOCATION_REVIEW.pending
  const changed = shown && shown !== li.original_location
  // Rule result and confidence only; an AI suggestion is shown on its own line, never with the rule's percentage.
  const ruleMethod = method === 'ai_suggestion' ? li.rule_method : method
  const ruleConf = method === 'ai_suggestion' ? li.rule_confidence : li.confidence
  const opts = locationOptions(li)
  const pending = reviewOf(row) === 'pending'
  return (
    <div className="space-y-0.5" data-location-method={method}>
      <p>{li.original_location ?? <span className="text-ink-faint">Not labelled</span>}</p>
      {changed && <p className="text-ok" aria-label="Location used">→ {shown}</p>}
      <p className="text-[10px] text-ink-faint">
        {LOCATION_METHOD[ruleMethod] ?? ruleMethod}{ruleConf != null && ruleMethod !== 'missing' ? ` · ${pct(ruleConf)}` : ''}
      </p>
      {opts.rule && !changed && pending && (
        <p className="text-[10px] text-ink-muted" aria-label="Rule candidate">Rule candidate: {opts.rule}</p>
      )}
      {li.ai_suggestion && !changed && (
        <p className="text-[10px] text-accent" aria-label="AI suggestion">
          AI Suggestion — Requires Review: {li.ai_suggestion} ({li.ai_confidence_band ?? aiBand(li.ai_confidence)} AI confidence)
        </p>
      )}
      {!changed && <AiLocationDetails info={li} />}
      {opts.disagree && !changed && pending && (
        <p className="text-[10px] font-semibold text-warn" aria-label="Disagreement">AI disagrees with the rule candidate</p>
      )}
      {!li.ai_suggestion && li.candidates?.length > 0 && !changed && (
        <p className="text-[10px] text-ink-muted">Could be: {li.candidates.slice(0, 3).join(' · ')}</p>
      )}
      {li.review_reason && pending && <p className="text-[10px] text-ink-muted" aria-label="Review reason">{li.review_reason}</p>}
      <p className={`text-[10px] font-semibold ${review.cls}`}>{review.label}</p>
      {onDecide && row.status === 'imported' && <LocationActions row={row} onDecide={onDecide} />}
    </div>
  )
}

/** Preview-level counts and AI availability (never any key). */
export function LocationSummary({ info }) {
  if (!info) return null
  const c = info.counts
  const ai = info.ai
  return (
    <div role="note" aria-label="Location intelligence" className="flex items-start gap-2 text-xs text-ink-muted">
      <Info size={14} className="mt-px shrink-0" aria-hidden="true" />
      <span>
        Locations: {formatNumber(c.auto_applied)} standardised automatically, {formatNumber(c.ai_suggestions)} AI suggestion(s),{' '}
        {formatNumber(c.review_required)} need review after import ({formatNumber(c.missing)} without a location
        {c.ai_disagreements ? `, ${formatNumber(c.ai_disagreements)} where the AI disagrees with the rules` : ''}).{' '}
        AI suggestions are never applied without review.{' '}
        {ai.enabled ? `AI: ${ai.simulated ? 'simulated (mock)' : ai.provider}${ai.used === 'ok' ? '' : ` — ${ai.used}`}.`
          : 'AI is not enabled; rules and human review are used.'}{' '}
        Original Excel text is always kept.
      </span>
    </div>
  )
}

function Chip({ status }) {
  const s = ROW_STATUS[status] ?? ROW_STATUS.skipped
  return <span className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] font-semibold ${s.cls}`}>{s.label}</span>
}

function Stat({ label, value, tone = 'text-ink' }) {
  return (
    <div className="rounded-lg border border-surface-700 bg-surface-900 px-4 py-3">
      <p className="text-[11px] uppercase tracking-[0.1em] text-ink-faint">{label}</p>
      <p className={`mt-1 text-xl font-semibold tabular-nums ${tone}`}>{formatNumber(value ?? 0)}</p>
    </div>
  )
}

export function Summary({ summary, done = false }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-5" aria-label="Import summary">
      <Stat label="Records" value={summary.records} />
      <Stat label={done ? 'Imported' : 'Ready to import'} value={done ? summary.imported : summary.ready} tone="text-ok" />
      <Stat label="Duplicates" value={summary.duplicates} tone={summary.duplicates ? 'text-warn' : 'text-ink'} />
      <Stat label="Failed" value={summary.failed} tone={summary.failed ? 'text-bad' : 'text-ink'} />
      <Stat label="Blank rows" value={summary.blank_rows} />
    </div>
  )
}

/* Phase 10.4: a row linked from the location review queue (?row=N). */
function linkedRow(rows, param) {
  if (param == null || param === '') return null
  if (!/^\d+$/.test(param)) return { n: null, state: 'invalid', text: `“${param.slice(0, 20)}” is not a valid row number.` }
  const n = Number(param)
  const row = rows.find((r) => r.row_number === n)
  if (!row || row.status !== 'imported') return { n, state: 'missing', text: `Row ${n} is not an imported record of this import.` }
  if (!matches(row, 'location')) return { n, state: 'decided', text: `Row ${n} no longer needs a location review (it has been decided). Showing all rows.` }
  return { n, state: 'pending', text: `Row ${n} needs a location review. It is highlighted below.` }
}

export function RowTable({ rows, onDecide, focusRow }) {
  const focus = linkedRow(rows, focusRow)
  const [filter, setFilter] = useState(focus?.state === 'pending' ? 'location' : 'all')
  const tableRef = useRef(null)
  useEffect(() => {
    if (focus?.state === 'pending') setFilter('location')
    else if (focus?.state === 'decided') setFilter('all')
  }, [focusRow]) // eslint-disable-line react-hooks/exhaustive-deps -- re-run only when the link changes
  useEffect(() => {
    if (focus?.state !== 'pending' && focus?.state !== 'decided') return
    tableRef.current?.querySelector(`tr[data-row="${focus.n}"]`)?.scrollIntoView?.({ block: 'center' })
  }, [focusRow, filter]) // eslint-disable-line react-hooks/exhaustive-deps
  const shown = rows.filter((r) => matches(r, filter))
  const highlighted = focus && (focus.state === 'pending' || focus.state === 'decided') ? focus.n : null
  return (
    <div className="space-y-3">
      {focus && (
        <p role="status" aria-label="Linked row" className={`text-xs ${focus.state === 'pending' ? 'text-accent' : 'text-warn'}`}>{focus.text}</p>
      )}
      <div role="tablist" aria-label="Filter rows" className="flex flex-wrap gap-1.5">
        {FILTERS.map(([key, text]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={filter === key}
            onClick={() => setFilter(key)}
            className={`focus-ring rounded-full border px-3 py-1 text-xs transition-colors ${
              filter === key ? 'border-accent/60 bg-accent/10 text-accent' : 'border-surface-600 text-ink-muted hover:text-ink'
            }`}
          >
            {text} ({rows.filter((r) => matches(r, key)).length})
          </button>
        ))}
      </div>
      <div ref={tableRef} className="max-h-[28rem] overflow-auto rounded-lg border border-surface-700">
        <table className="w-full min-w-[720px] text-left text-xs">
          <thead className="sticky top-0 bg-surface-900 text-[11px] uppercase tracking-wide text-ink-faint">
            <tr>
              <th className="px-3 py-2">Row</th><th className="px-3 py-2">Serial</th><th className="px-3 py-2">Image file</th>
              <th className="px-3 py-2">Inspection date</th><th className="px-3 py-2">Official Location</th>
              <th className="px-3 py-2">Status</th><th className="px-3 py-2">Details</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.row_number} data-row={r.row_number} aria-current={r.row_number === highlighted ? 'true' : undefined}
                className={`border-t border-surface-700 align-top ${r.row_number === highlighted ? 'bg-accent/10 outline outline-1 -outline-offset-1 outline-accent/60' : ''}`}>
                <td className="px-3 py-2 tabular-nums text-ink-muted">
                  {r.row_number}
                  {r.row_number === highlighted && <span className="block text-[10px] font-semibold text-accent">Linked</span>}
                </td>
                <td className="px-3 py-2 tabular-nums">{r.serial_number ?? '—'}</td>
                <td className="max-w-[16rem] px-3 py-2"><FileName name={r.image_filename} /></td>
                <td className="px-3 py-2">
                  {r.inspection_date ? formatDate(r.inspection_date) : '—'}
                  {r.date_source === 'worksheet_name' && <span className="block text-[10px] text-ink-faint">from worksheet name</span>}
                </td>
                <td className="px-3 py-2"><LocationCell row={r} onDecide={onDecide} /></td>
                <td className="px-3 py-2"><Chip status={r.status} /></td>
                <td className="px-3 py-2">
                  <ul className="space-y-0.5">
                    {r.errors.map((m) => <li key={m} className="text-bad">{m}</li>)}
                    {r.warnings.map((m) => <li key={m} className="text-warn">{m}</li>)}
                    {r.notes.map((m) => <li key={m} className="text-ink-muted">{m}</li>)}
                  </ul>
                </td>
              </tr>
            ))}
            {!shown.length && (
              <tr><td colSpan={7} className="px-3 py-6 text-center text-ink-muted">No rows in this category.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Problem({ error, onDismiss }) {
  return (
    <div role="alert" className="flex items-start gap-2 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-ink">
      <XCircle size={15} className="mt-0.5 shrink-0 text-bad" aria-hidden="true" />
      <div className="flex-1">
        <p>{error.message}</p>
        {error.guidance && <p className="mt-0.5 text-xs text-ink-muted">{error.guidance}</p>}
      </div>
      {onDismiss && (
        <button type="button" onClick={onDismiss} aria-label="Dismiss" className="focus-ring rounded text-ink-faint hover:text-ink">
          <X size={14} />
        </button>
      )}
    </div>
  )
}

function useElapsed(active) {
  const [s, setS] = useState(0)
  useEffect(() => {
    if (!active) return undefined
    setS(0)
    const t = setInterval(() => setS((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [active])
  return s
}

function ImportPanel({ onImported }) {
  const input = useRef(null)
  const [file, setFile] = useState(null)
  const [phase, setPhase] = useState('idle') // idle | previewing | preview | importing | done
  const [preview, setPreview] = useState(null)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const elapsed = useElapsed(phase === 'previewing' || phase === 'importing')

  const reset = () => {
    setFile(null); setPreview(null); setResult(null); setError(null); setPhase('idle')
    if (input.current) input.current.value = ''
  }
  const choose = (e) => {
    const f = e.target.files?.[0] ?? null
    setPreview(null); setResult(null); setError(null); setPhase('idle')
    if (f && !f.name.toLowerCase().endsWith('.xlsx')) {
      setFile(null)
      setError({ message: 'Only Excel workbooks (.xlsx) can be imported.', guidance: 'Choose an Image Register saved as .xlsx.' })
      return
    }
    setFile(f)
  }
  const runPreview = async () => {
    setPhase('previewing'); setError(null)
    try {
      setPreview(await previewImport(file)); setPhase('preview')
    } catch (err) {
      setError(err); setPhase('idle')
    }
  }
  const runImport = async () => {
    setPhase('importing'); setError(null)
    try {
      const res = await confirmImport(file, preview.fingerprint)
      setResult(res); setPhase('done'); onImported(res.import_id)
    } catch (err) {
      setError(err); setPhase('preview')
    }
  }
  const busy = phase === 'previewing' || phase === 'importing'

  return (
    <section className="card" aria-labelledby="import-title">
      <div className="card-header flex-wrap">
        <div className="flex items-start gap-3">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-accent/10 text-accent ring-1 ring-accent/20">
            <FileSpreadsheet size={17} aria-hidden="true" />
          </span>
          <div>
            <h2 id="import-title" className="card-title">Import an Image Register</h2>
            <p className="card-subtitle">
              Workbook with an “Image Register” sheet and one embedded photograph per row. Previewing never saves anything.
            </p>
          </div>
        </div>
      </div>
      <div className="space-y-4 p-5">
        <div className="flex flex-wrap items-center gap-3">
          <label className="focus-within:ring-accent inline-flex cursor-pointer items-center gap-2 rounded-lg border border-surface-600 bg-surface-800 px-3 py-2 text-sm text-ink hover:border-accent/50">
            <Upload size={15} aria-hidden="true" />
            <span>{file ? 'Choose another file' : 'Choose workbook'}</span>
            <input
              ref={input}
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              className="sr-only"
              aria-label="Excel workbook"
              disabled={busy}
              onChange={choose}
            />
          </label>
          {file && <span className="text-sm text-ink-muted">{file.name} · {formatNumber(Math.round(file.size / 1024))} KB</span>}
          <button
            type="button"
            onClick={runPreview}
            disabled={!file || busy || phase === 'preview' || phase === 'done'}
            className="focus-ring inline-flex h-9 items-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-surface-950 hover:bg-accent-soft disabled:cursor-not-allowed disabled:opacity-40"
          >
            {phase === 'previewing' && <Loader2 size={15} className="animate-spin" aria-hidden="true" />}
            Preview import
          </button>
        </div>

        {busy && (
          <div role="status" aria-label={phase === 'previewing' ? 'Validating workbook' : 'Importing records'} className="rounded-lg border border-accent/30 bg-accent/5 px-4 py-3">
            <p className="flex items-center gap-2 text-sm text-ink">
              <Loader2 size={15} className="animate-spin text-accent" aria-hidden="true" />
              {phase === 'previewing' ? 'Validating the workbook and checking for duplicates…' : 'Importing records and photographs…'}
              <span className="tabular-nums text-ink-faint">{elapsed}s</span>
            </p>
            <div className="mt-2 h-1 overflow-hidden rounded bg-surface-700"><div className="h-full w-1/3 animate-pulse rounded bg-accent" /></div>
          </div>
        )}

        {error && <Problem error={error} onDismiss={() => setError(null)} />}

        {preview && phase !== 'done' && (
          <div className="space-y-4" aria-label="Import preview">
            <h3 className="text-sm font-semibold text-ink">Preview · <FileName name={preview.filename} /></h3>
            <Summary summary={preview.summary} />
            {preview.summary.repeated_appearances > 0 && (
              <p className="flex items-start gap-2 text-xs text-ink-muted">
                <Info size={14} className="mt-px shrink-0 text-accent" aria-hidden="true" />
                {preview.summary.repeated_appearances} photograph(s) also appear in other inspections. They are legitimate repeated
                appearances and will be imported as separate records.
              </p>
            )}
            <LocationSummary info={preview.location_intelligence} />
            {preview.source_format === 'deck_photos' && (
              <p role="note" aria-label="Detected format" className="flex items-start gap-2 text-xs text-ink-muted">
                <Info size={14} className="mt-px shrink-0" aria-hidden="true" />
                Deck Photos workbook: photographs are numbered in reading order, and each takes the location heading above it.
              </p>
            )}
            {preview.file_warnings.map((w) => (
              <p key={w} className="flex items-start gap-2 text-xs text-warn"><AlertTriangle size={14} className="mt-px shrink-0" aria-hidden="true" />{w}</p>
            ))}
            <RowTable rows={preview.rows} />
            <div className="flex flex-wrap items-center justify-end gap-2">
              {!preview.can_import && <span className="text-xs text-ink-muted">There are no new valid records to import.</span>}
              <button type="button" onClick={reset} disabled={busy} className="focus-ring h-9 rounded-lg border border-surface-600 px-4 text-sm text-ink hover:border-accent/50 disabled:opacity-40">
                Cancel
              </button>
              <button
                type="button"
                onClick={runImport}
                disabled={busy || !preview.can_import}
                className="focus-ring inline-flex h-9 items-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-surface-950 hover:bg-accent-soft disabled:cursor-not-allowed disabled:opacity-40"
              >
                {phase === 'importing' && <Loader2 size={15} className="animate-spin" aria-hidden="true" />}
                Import {formatNumber(preview.summary.ready)} record{preview.summary.ready === 1 ? '' : 's'}
              </button>
            </div>
          </div>
        )}

        {phase === 'done' && result && (
          <div role="status" aria-label="Import complete" className="space-y-3 rounded-lg border border-ok/40 bg-ok/10 px-4 py-3">
            <p className="flex items-center gap-2 text-sm font-semibold text-ink">
              <CheckCircle2 size={16} className="text-ok" aria-hidden="true" />
              Import {result.import_id} completed: {formatNumber(result.summary.imported)} record(s) imported.
            </p>
            <Summary summary={result.summary} done />
            <p className="text-xs text-ink-muted">The new records are now available in search, comparison and reports. Original files were not changed.</p>
            <button type="button" onClick={reset} className="focus-ring h-9 rounded-lg border border-surface-600 px-4 text-sm text-ink hover:border-accent/50">
              Import another workbook
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

/** Phase 8.4 -- admin-only integrity check and reversible withdrawal (rollback) of one import. */
function RollbackPanel({ data, onChanged }) {
  const { isAdmin } = useAuth()
  const withdrawn = data.status === 'withdrawn'
  const [reason, setReason] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)
  const [check, setCheck] = useState(null)
  if (!isAdmin) return null
  const last = data.state_events?.[data.state_events.length - 1]
  const act = async () => {
    setBusy(true)
    setMessage(null)
    try {
      const res = withdrawn ? await reinstateImport(data.import_id, reason) : await withdrawImport(data.import_id, reason)
      const n = res.event?.affected_analyses?.length || 0
      setMessage({ ok: true, text: withdrawn ? 'Import reinstated; its records are back in the dataset.'
        : `Import withdrawn; its records left the dataset. Files are kept and it can be reinstated.${n ? ` ${n} saved analysis(es) reference these records.` : ''}` })
      setReason('')
      setConfirm('')
      onChanged()
    } catch (err) {
      setMessage({ ok: false, text: `${err.message}${err.guidance ? ` ${err.guidance}` : ''}` })
    } finally {
      setBusy(false)
    }
  }
  const verify = async () => {
    setCheck({ loading: true })
    try { setCheck(await verifyImport(data.import_id)) } catch (err) { setCheck({ error: err.message }) }
  }
  const field = 'focus-ring h-9 w-full rounded-lg border border-surface-600 bg-surface-800 px-3 text-sm text-ink'
  return (
    <section aria-label="Import administration" className="space-y-3 rounded-lg border border-surface-700 bg-surface-900 p-4 text-sm">
      {withdrawn && last && (
        <p role="note" className="text-xs text-warn">Withdrawn {formatDate(last.at.slice(0, 10))} by {last.by?.username || 'unknown'}: “{last.reason}”</p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={verify} className="focus-ring rounded-lg border border-surface-600 px-3 py-1.5 text-xs text-ink-muted hover:text-ink">Check file integrity</button>
        {check?.loading && <Loader2 size={14} className="animate-spin" aria-label="Checking" />}
        {check && !check.loading && (
          <span role="status" aria-label="Integrity result" className={`text-xs ${check.valid ? 'text-ok' : 'text-bad'}`}>
            {check.error || (check.valid ? `Intact: workbook and ${check.photographs_checked} photograph(s) match their SHA-256.` : check.problems.join(' · '))}
          </span>
        )}
      </div>
      <div className="grid gap-2 sm:grid-cols-[1fr_14rem_auto] sm:items-end">
        <div><label htmlFor={`r-${data.import_id}`} className="mb-1 block text-xs text-ink-faint">{withdrawn ? 'Reason for reinstating' : 'Reason for withdrawing'}</label>
          <input id={`r-${data.import_id}`} value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} className={field} /></div>
        <div><label htmlFor={`c-${data.import_id}`} className="mb-1 block text-xs text-ink-faint">Type the import ID to confirm</label>
          <input id={`c-${data.import_id}`} value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" className={field} /></div>
        <button type="button" onClick={act} disabled={busy || confirm.trim() !== data.import_id || reason.trim().length < 5}
          className="focus-ring inline-flex h-9 items-center gap-2 rounded-lg border border-warn/50 px-3 text-xs font-semibold text-warn hover:bg-warn/10 disabled:opacity-40">
          {busy && <Loader2 size={14} className="animate-spin" aria-hidden="true" />} {withdrawn ? 'Reinstate import' : 'Withdraw import'}
        </button>
      </div>
      <p className="text-[11px] text-ink-faint">Withdrawal is reversible: nothing is deleted, and saved analyses are never changed.</p>
      {message && <p role={message.ok ? 'status' : 'alert'} className={`text-xs ${message.ok ? 'text-ok' : 'text-bad'}`}>{message.text}</p>}
    </section>
  )
}

function ImportDetail({ id, focusRow, onClose, onChanged }) {
  const detail = useApi((s) => getImport(id, s), [id])
  // Keep the last loaded details on screen while they refresh (e.g. after a withdrawal).
  const last = useRef(null)
  if (detail.data) last.current = detail.data
  else if (!detail.loading || last.current?.import_id !== id) last.current = null
  const data = detail.data ?? last.current
  const { isAdmin } = useAuth()
  const decide = async (rowNumber, action, location) => {
    await decideLocation(id, rowNumber, action, location)
    detail.reload()
  }
  return (
    <section className="card" aria-label="Import details">
      <div className="card-header">
        <div>
          <h2 className="card-title">Import {id}</h2>
          {data && <p className="card-subtitle"><FileName name={data.filename} /> · {formatDate(data.created_at.slice(0, 10))}</p>}
        </div>
        <button type="button" onClick={onClose} aria-label="Close details" className="focus-ring rounded-lg p-1.5 text-ink-faint hover:text-ink"><X size={16} /></button>
      </div>
      <div className="space-y-4 p-5">
        {!data && detail.loading ? <Skeleton className="h-40 w-full rounded-lg" />
          : detail.error ? <ErrorState error={detail.error} title="Import details could not be loaded" onRetry={detail.reload} compact />
            : (
              <>
                <Summary summary={data.summary} done />
                <RollbackPanel data={data} onChanged={() => { detail.reload(); onChanged?.() }} />
                <RowTable rows={data.rows} focusRow={focusRow} onDecide={isAdmin && data.status !== 'withdrawn' ? decide : undefined} />
              </>
            )}
      </div>
    </section>
  )
}

function HistoryTable({ history, selected, onSelect }) {
  const { imports, failed_attempts: failed } = history
  const rows = [
    ...imports.map((i) => ({ ...i, key: i.import_id, ok: true })),
    ...failed.map((a) => ({ ...a, key: a.attempt_id, ok: false })),
  ].sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
  if (!rows.length) return <EmptyState title="No imports yet" description="Imported registers will be listed here." icon={History} />
  return (
    <div className="overflow-x-auto rounded-lg border border-surface-700">
      <table className="w-full min-w-[720px] text-left text-sm" aria-label="Import history">
        <thead className="bg-surface-900 text-[11px] uppercase tracking-wide text-ink-faint">
          <tr>
            <th className="px-3 py-2">Import date</th><th className="px-3 py-2">File</th><th className="px-3 py-2 text-right">Total records</th>
            <th className="px-3 py-2 text-right">Imported</th><th className="px-3 py-2 text-right">Skipped</th>
            <th className="px-3 py-2 text-right">Failed</th><th className="px-3 py-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className={`border-t border-surface-700 ${selected === r.key ? 'bg-accent/5' : ''}`}>
              <td className="px-3 py-2 tabular-nums">{formatDate(r.created_at.slice(0, 10))}</td>
              <td className="px-3 py-2">
                {r.ok ? (
                  <button type="button" onClick={() => onSelect(r.import_id)} className="focus-ring text-left text-accent hover:underline">
                    <FileName name={r.filename} />
                  </button>
                ) : <FileName name={r.filename} />}
                {!r.ok && <span className="block text-xs text-ink-muted">{r.message}</span>}
              </td>
              <td className="px-3 py-2 text-right tabular-nums">{r.summary ? formatNumber(r.summary.records) : '—'}</td>
              <td className="px-3 py-2 text-right tabular-nums">{r.ok ? formatNumber(r.summary.imported) : 0}</td>
              <td className="px-3 py-2 text-right tabular-nums">{r.summary ? formatNumber(r.summary.skipped) : '—'}</td>
              <td className="px-3 py-2 text-right tabular-nums">{r.summary ? formatNumber(r.summary.failed) : '—'}</td>
              <td className="px-3 py-2">
                {r.ok ? <Chip status={r.status === 'withdrawn' ? 'withdrawn' : 'imported'} /> : <span className="inline-flex rounded-full border border-bad/40 bg-bad/10 px-2 py-0.5 text-[11px] font-semibold text-bad">Not imported</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Shown instead of the upload panel when the viewer is not an administrator (history stays visible). */
function ImportLocked() {
  const { user, requireSignIn } = useAuth()
  return (
    <section className="card" aria-labelledby="import-locked-title">
      <div className="card-header">
        <div>
          <h2 id="import-locked-title" className="card-title">Import an Image Register</h2>
          <p className="card-subtitle">Importing workbooks is limited to administrators. The import history below is open to everyone.</p>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-3 p-5 text-sm text-ink-muted">
        {user ? (
          <span>You are signed in as {user.display_name} ({ROLE_LABELS[user.role] || user.role}). Ask an administrator to import new workbooks.</span>
        ) : (
          <>
            <span>Sign in with an administrator account to import workbooks.</span>
            <button type="button" onClick={() => requireSignIn('Sign in with an administrator account to import workbooks.')}
              className="focus-ring rounded-lg border border-accent/40 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/10">Sign in</button>
          </>
        )}
      </div>
    </section>
  )
}

export default function ImportPage() {
  const [params, mutate] = useUrlParams()
  const selected = params.get('id')
  const [nonce, setNonce] = useState(0)
  const history = useApi((s) => getImportHistory(s), [nonce])
  const select = (id) => mutate((next) => {
    next.delete('row') // a linked row belongs to the import it was opened with
    if (id) next.set('id', id)
    else next.delete('id')
  })
  const { isAdmin } = useAuth()

  return (
    <div className="space-y-6">
      {isAdmin ? <ImportPanel onImported={() => setNonce((n) => n + 1)} /> : <ImportLocked />}
      <section className="card" aria-labelledby="history-title">
        <div className="card-header">
          <div>
            <h2 id="history-title" className="card-title">Import history</h2>
            <p className="card-subtitle">Every confirmed import, plus attempts that were refused. Existing inspection data is never changed.</p>
          </div>
        </div>
        <div className="space-y-3 p-5">
          {history.loading && !history.data ? <Skeleton className="h-24 w-full rounded-lg" />
            : history.error ? <ErrorState error={history.error} title="Import history could not be loaded" onRetry={history.reload} compact />
              : history.data && (
                <>
                  {history.data.interrupted_imports > 0 && (
                    <p role="note" className="flex items-start gap-2 text-xs text-warn">
                      <AlertTriangle size={14} className="mt-px shrink-0" aria-hidden="true" />
                      {history.data.interrupted_imports} interrupted import(s) were found. They were never added to the dataset and
                      will be cleaned up by the next import.
                    </p>
                  )}
                  {history.data.problems.map((p) => <p key={p} role="note" className="text-xs text-warn">{p}</p>)}
                  <HistoryTable history={history.data} selected={selected} onSelect={select} />
                </>
              )}
        </div>
      </section>
      {selected && <ImportDetail id={selected} focusRow={params.get('row')} onClose={() => select(null)} onChanged={() => setNonce((n) => n + 1)} />}
    </div>
  )
}
