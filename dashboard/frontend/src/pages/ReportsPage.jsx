import { CheckCircle2, Download, FileSpreadsheet, FileText, Loader2, AlertTriangle } from 'lucide-react'
import { useCallback, useState } from 'react'
import { ErrorState, Skeleton } from '../components/ui/StateViews'
import { useAuth } from '../context/AuthContext'
import { useVessel } from '../context/VesselContext'
import { useApi } from '../hooks/useApi'
import { useUrlParams } from '../hooks/useUrlParams'
import { downloadReport, getReportPreview, saveBlob } from '../services/api'
import { formatDate, formatNumber } from '../utils/format'

/*
 * Reports for a manager: a date range and two downloads (inspection PDF, inspection spreadsheet). Uses the existing
 * report endpoints unchanged. The AI assessments spreadsheet stays available to administrators.
 */
const field =
  'focus-ring h-10 w-full rounded-lg border border-surface-600 bg-surface-800 px-3 text-sm text-ink transition-colors hover:border-accent/50'
const label = 'text-xs font-medium text-ink-muted'
const btn = 'focus-ring inline-flex h-11 items-center justify-center gap-2 rounded-xl px-5 text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40'

function useRange() {
  const [params, mutate] = useUrlParams()
  const range = { from: params.get('from') ?? '', to: params.get('to') ?? '' }
  const update = useCallback((patch) => mutate((next) => {
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v)
      else next.delete(k)
    }
  }), [mutate])
  return [range, update]
}

export default function ReportsPage() {
  const [range, update] = useRange()
  const { selectedVessel } = useVessel()
  const { isAdmin } = useAuth()
  const vessel = selectedVessel?.vessel_id
  const badRange = Boolean(range.from && range.to && range.from > range.to)
  const filters = { vessel, date_from: range.from || undefined, date_to: range.to || undefined }
  const preview = useApi((s) => getReportPreview(filters, s), [JSON.stringify(filters)], { enabled: Boolean(vessel) && !badRange })
  const s = preview.data?.summary
  const empty = Boolean(s) && !s.appearances

  const [busy, setBusy] = useState(null)
  const [message, setMessage] = useState(null)
  const download = async (kind) => {
    if (busy) return
    setBusy(kind)
    setMessage(null)
    try {
      const file = await downloadReport(kind, filters)
      saveBlob(file)
      setMessage({ ok: true, text: `Downloaded ${file.filename}` })
    } catch (err) {
      setMessage({ ok: false, text: err.message })
    } finally {
      setBusy(null)
    }
  }
  const disabled = !vessel || badRange || Boolean(busy) || empty
  const icon = (kind, Icon) => (busy === kind ? <Loader2 size={17} className="animate-spin" aria-hidden="true" /> : <Icon size={17} aria-hidden="true" />)

  return (
    <div className="space-y-6">
      <section className="card p-6" aria-labelledby="reports-title">
        <h2 id="reports-title" className="text-xl font-semibold tracking-tight text-ink">Inspection Reports</h2>
        <p className="mt-1 text-sm text-ink-muted">
          {selectedVessel ? selectedVessel.vessel_name : 'No vessel selected'} · Choose a date range, or leave it empty for all inspections.
        </p>

        <form className="mt-5 grid max-w-xl gap-4 sm:grid-cols-2" aria-label="Report date range" onSubmit={(e) => e.preventDefault()}>
          <label className="flex flex-col gap-1">
            <span className={label}>Start date</span>
            <input type="date" value={range.from} onChange={(e) => update({ from: e.target.value })} className={field} aria-label="Start date" />
          </label>
          <label className="flex flex-col gap-1">
            <span className={label}>End date</span>
            <input type="date" value={range.to} onChange={(e) => update({ to: e.target.value })} className={field} aria-label="End date" />
          </label>
        </form>
        {(range.from || range.to) && (
          <button type="button" onClick={() => update({ from: null, to: null })} className="focus-ring mt-2 rounded-md px-1 text-xs text-ink-muted hover:text-ink">
            Clear dates (all inspections)
          </button>
        )}

        <div className="mt-4 min-h-[1.5rem] text-sm" aria-live="polite">
          {badRange ? (
            <p role="alert" className="text-bad">The start date must be on or before the end date.</p>
          ) : preview.loading ? (
            <Skeleton className="h-5 w-80" />
          ) : preview.error ? (
            <ErrorState error={preview.error} title="The report could not be prepared" onRetry={preview.reload} compact />
          ) : empty ? (
            <p className="text-warn">No inspection photos fall within these dates. Widen the date range.</p>
          ) : s ? (
            <p className="text-ink-muted" aria-label="Report contents">
              The report will include {formatNumber(s.appearances)} photos from {formatNumber(s.inspection_dates)} {s.inspection_dates === 1 ? 'inspection' : 'inspections'}
              {preview.data.period?.from && ` (${formatDate(preview.data.period.from)} – ${formatDate(preview.data.period.to)})`}.
            </p>
          ) : null}
        </div>

        <div className="mt-5 flex flex-wrap gap-3" aria-label="Downloads">
          <button type="button" onClick={() => download('pdf')} disabled={disabled} className={`${btn} bg-accent text-surface-950 hover:bg-accent-soft`}>
            {icon('pdf', FileText)} Download Inspection PDF
          </button>
          <button type="button" onClick={() => download('inspectionsCsv')} disabled={disabled}
            className={`${btn} border border-surface-600 bg-surface-800 text-ink hover:border-accent/50`}>
            {icon('inspectionsCsv', FileSpreadsheet)} Download Spreadsheet
          </button>
        </div>
        <p className="mt-2 text-xs text-ink-faint">The spreadsheet is a CSV file and opens in Excel. AI assessments not yet approved are marked as unverified in the PDF.</p>

        {message && (
          <p role={message.ok ? 'status' : 'alert'} className={`mt-4 flex items-center gap-2 text-sm ${message.ok ? 'text-ok' : 'text-bad'}`}>
            {message.ok ? <CheckCircle2 size={15} aria-hidden="true" /> : <AlertTriangle size={15} aria-hidden="true" />} {message.text}
          </p>
        )}
      </section>

      {isAdmin && (
        <section className="card p-6" aria-labelledby="admin-downloads-title">
          <h3 id="admin-downloads-title" className="text-sm font-semibold text-ink">Administrator downloads</h3>
          <p className="mt-1 text-xs text-ink-muted">Every saved AI assessment with its evidence and review status, for the same date range.</p>
          <button type="button" onClick={() => download('analysesCsv')} disabled={!vessel || badRange || Boolean(busy)}
            className={`${btn} mt-3 border border-surface-600 bg-surface-800 text-ink hover:border-accent/50`}>
            {busy === 'analysesCsv' ? <Loader2 size={17} className="animate-spin" aria-hidden="true" /> : <Download size={17} aria-hidden="true" />} Download AI assessments (CSV)
          </button>
        </section>
      )}
    </div>
  )
}
