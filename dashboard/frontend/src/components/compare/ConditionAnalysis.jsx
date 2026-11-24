import { AlertTriangle, BrainCircuit, CheckCircle2, ClipboardCheck, FlaskConical, Loader2, MinusCircle, RefreshCw, ShieldAlert, TrendingDown, TrendingUp } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { useApi } from '../../hooks/useApi'
import { ROLE_LABELS, useAuth } from '../../context/AuthContext'
import { compareImages, getAiStatus, getAnalysisHistory, reviewAnalysis, validateImagePair } from '../../services/api'
import { formatDate } from '../../utils/format'
import { ErrorState, Skeleton } from '../ui/StateViews'
import PairValidation, { pairState } from './PairValidation'

export const CLASSIFICATIONS = {
  DETERIORATED: { label: 'Deteriorated', icon: TrendingDown, tone: 'border-bad/50 bg-bad/10 text-bad' },
  AMELIORATED: { label: 'Ameliorated', icon: TrendingUp, tone: 'border-ok/50 bg-ok/10 text-ok' },
  STABLE: { label: 'Stable', icon: MinusCircle, tone: 'border-accent/50 bg-accent/10 text-accent' },
  NEEDS_REVIEW: { label: 'Needs Review', icon: AlertTriangle, tone: 'border-warn/50 bg-warn/10 text-warn' },
}
export const REVIEW_STATUSES = {
  PENDING_REVIEW: 'Pending review',
  APPROVED: 'Approved',
  REJECTED: 'Rejected',
  NEEDS_REASSESSMENT: 'Needs reassessment',
}
const LEVELS = { LOW: 1, MEDIUM: 2, HIGH: 3 }
export const PROVIDER_LABELS = { gemini: 'Gemini', groq: 'Groq', mock: 'Mock provider', disabled: 'Disabled' }
const REANALYSABLE = ['REJECTED', 'NEEDS_REASSESSMENT']
const title = (s) => (s ? s.charAt(0) + s.slice(1).toLowerCase().replaceAll('_', ' ') : '')

export function ClassificationBadge({ value, size = 'md' }) {
  const c = CLASSIFICATIONS[value]
  if (!c) return null
  const Icon = c.icon
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-lg border font-semibold ${c.tone} ${size === 'lg' ? 'px-3 py-1.5 text-base' : 'px-2 py-0.5 text-xs'}`}>
      <Icon size={size === 'lg' ? 18 : 13} aria-hidden="true" /> {c.label}
    </span>
  )
}

export function LevelMeter({ label, value }) {
  const n = LEVELS[value] ?? 0
  return (
    <div className="flex items-center gap-2" aria-label={`${label}: ${title(value)}`} role="img">
      <span className="w-24 text-xs text-ink-faint">{label}</span>
      <span className="flex gap-0.5" aria-hidden="true">
        {[1, 2, 3].map((i) => (
          <span key={i} className={`h-2 w-5 rounded-sm ${i <= n ? 'bg-accent' : 'bg-surface-700'}`} />
        ))}
      </span>
      <span className="text-xs font-medium text-ink">{title(value)}</span>
    </div>
  )
}

function List({ title: heading, items, empty = 'None reported', render = (x) => x }) {
  return (
    <div>
      <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-ink-faint">{heading}</h4>
      {items?.length ? (
        <ul className="space-y-1.5 text-sm text-ink-muted">
          {items.map((x, i) => (
            <li key={i} className="flex gap-2"><span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-surface-500" aria-hidden="true" /><span className="min-w-0">{render(x)}</span></li>
          ))}
        </ul>
      ) : (
        <p className="text-sm italic text-ink-faint">{empty}</p>
      )}
    </div>
  )
}

const evidence = (e) => (
  <>
    <span className="font-medium text-ink">{e.region}:</span> {e.change}
    <span className="mt-0.5 block text-xs text-ink-faint">Earlier: {e.earlier_appearance} · Later: {e.later_appearance}</span>
  </>
)

function ReviewPanel({ analysis, onSaved }) {
  const id = useId()
  const [status, setStatus] = useState(analysis.review.status === 'PENDING_REVIEW' ? 'APPROVED' : analysis.review.status)
  const { user, requireSignIn, canReview } = useAuth()
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const submit = async (e) => {
    e.preventDefault()
    if (!user) return requireSignIn('Sign in to record a review decision.')
    setSaving(true)
    setError(null)
    try {
      // The reviewer is identified by the signed-in session on the server; no name is sent.
      onSaved(await reviewAnalysis(analysis.analysis_id, { status, note: note.trim() || null }))
      setNote('')
    } catch (err) {
      setError(err)
    } finally {
      setSaving(false)
    }
  }
  const history = analysis.review.history
  return (
    <section aria-label="Human review" className="rounded-lg border border-surface-700 bg-surface-900 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h4 className="flex items-center gap-2 text-sm font-semibold text-ink"><ClipboardCheck size={15} className="text-accent" aria-hidden="true" /> Human review</h4>
        <span className="chip" aria-label="Review status">{REVIEW_STATUSES[analysis.review.status]}</span>
      </div>
      {user && !canReview ? (
        <p className="rounded-lg border border-surface-700 bg-surface-850 px-3 py-2.5 text-xs text-ink-muted">You have view-only access. A reviewer records review decisions.</p>
      ) : !user ? (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-surface-700 bg-surface-850 px-3 py-2.5 text-xs text-ink-muted">
          <span>Sign in as a reviewer to approve or reject this assessment.</span>
          <button type="button" onClick={() => requireSignIn('Sign in to record a review decision.')}
            className="focus-ring rounded-lg border border-accent/40 px-3 py-1.5 font-semibold text-accent hover:bg-accent/10">Sign in to review</button>
        </div>
      ) : (
      <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[10rem_1fr]">
        <label htmlFor={`${id}-status`} className="text-xs text-ink-faint sm:pt-2.5">Review decision</label>
        <select id={`${id}-status`} value={status} onChange={(e) => setStatus(e.target.value)}
          className="focus-ring h-9 rounded-lg border border-surface-600 bg-surface-800 px-2 text-sm text-ink">
          {Object.entries(REVIEW_STATUSES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <span className="text-xs text-ink-faint sm:pt-0.5">Reviewer</span>
        <p className="text-sm text-ink" aria-label="Reviewer identity">
          {user.display_name} <span className="text-xs text-ink-faint">({ROLE_LABELS[user.role] || user.role} · {user.username})</span>
        </p>
        <label htmlFor={`${id}-note`} className="text-xs text-ink-faint sm:pt-2.5">Note (optional)</label>
        <textarea id={`${id}-note`} value={note} maxLength={1000} rows={2} onChange={(e) => setNote(e.target.value)}
          className="focus-ring rounded-lg border border-surface-600 bg-surface-800 px-3 py-2 text-sm text-ink" />
        <div className="flex items-center gap-3 sm:col-start-2">
          <button type="submit" disabled={saving}
            className="focus-ring inline-flex h-9 items-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-surface-950 hover:bg-accent-soft disabled:opacity-50">
            {saving && <Loader2 size={14} className="animate-spin" aria-hidden="true" />} Save review
          </button>
          {error && <span role="alert" className="text-xs text-bad">{error.message}</span>}
        </div>
      </form>
      )}
      <p className="mt-3 text-[11px] text-ink-faint">A review records a decision about the AI result; the original AI result is kept unchanged.</p>
      {history.length > 0 && (
        <ol className="mt-3 space-y-1 border-t border-surface-700/60 pt-3 text-xs text-ink-muted" aria-label="Review history">
          {[...history].reverse().map((h, i) => (
            <li key={i}>
              <span className="font-medium text-ink">{REVIEW_STATUSES[h.status]}</span> by {h.reviewer}
              {h.reviewer_username ? ` (${h.reviewer_username})` : h.reviewer_role ? ` (${ROLE_LABELS[h.reviewer_role] || h.reviewer_role})` : ' (name entered before sign-in was required)'} · {new Date(h.at).toLocaleString('en-GB')}
              {h.note && <span className="block text-ink-faint">“{h.note}”</span>}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

export function AnalysisResult({ analysis, onReviewed }) {
  const r = analysis.ai_result
  return (
    <div className="space-y-5">
      {analysis.simulated && (
        <p role="note" className="flex items-start gap-2 rounded-lg border border-warn/50 bg-warn/10 px-3 py-2 text-xs text-warn">
          <FlaskConical size={14} className="mt-px shrink-0" aria-hidden="true" />
          Simulated result from the development mock provider. It is not an assessment of these photographs.
        </p>
      )}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-2">
          <p className="text-[11px] uppercase tracking-[0.12em] text-ink-faint">AI classification (unverified)</p>
          <ClassificationBadge value={analysis.classification} size="lg" />
          {analysis.model_classification !== analysis.classification && (
            <p className="text-xs text-ink-muted">Model answered <ClassificationBadge value={analysis.model_classification} />; adjusted by validation rules.</p>
          )}
        </div>
        <div className="space-y-1.5">
          <LevelMeter label="Confidence" value={analysis.confidence} />
          <LevelMeter label="Reliability" value={analysis.comparison_reliability} />
          <p className="text-xs text-ink-faint">Same area: <span className="text-ink-muted">{title(r.same_area)}</span> · Image quality: <span className="text-ink-muted">{title(r.earlier_image_quality)} / {title(r.later_image_quality)}</span></p>
        </div>
      </div>

      {analysis.policy_adjustments.length > 0 && (
        <ul className="space-y-1 rounded-lg border border-warn/40 bg-warn/5 px-3 py-2 text-xs text-warn" aria-label="Validation adjustments">
          {analysis.policy_adjustments.map((a, i) => <li key={i}>{a}</li>)}
        </ul>
      )}

      <div>
        <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-ink-faint">Explanation</h4>
        <p className="text-sm leading-relaxed text-ink">{r.explanation}</p>
      </div>

      <div className="grid gap-5 md:grid-cols-2">
        <List title="Visual observations" items={r.visual_observations} />
        <List title="Comparison limitations" items={r.limitations} />
        <List title="Deterioration evidence" items={r.deterioration_evidence} render={evidence} />
        <List title="Improvement evidence" items={r.improvement_evidence} render={evidence} />
        <List title="Differences not treated as condition changes" items={r.non_condition_differences} />
        <div className="flex items-start gap-2 rounded-lg border border-surface-700 bg-surface-900 p-3 text-xs text-ink-muted">
          <ShieldAlert size={14} className={`mt-px shrink-0 ${analysis.human_review_required ? 'text-warn' : 'text-ink-faint'}`} aria-hidden="true" />
          <div>
            <p className="font-medium text-ink">{analysis.human_review_required ? 'Human review required' : 'Human review recommended'}</p>
            {analysis.review_reasons.length > 0 && <p className="mt-0.5">{analysis.review_reasons.join('; ')}.</p>}
          </div>
        </div>
      </div>

      <ReviewPanel analysis={analysis} onSaved={onReviewed} />

      <p className="text-[11px] text-ink-faint">
        {analysis.analysis_id} · {analysis.provider} / {analysis.model} · prompt {analysis.prompt_version} · {new Date(analysis.created_at).toLocaleString('en-GB')}
        {analysis.supersedes && ` · supersedes ${analysis.supersedes}`}
      </p>
    </div>
  )
}

/**
 * `validation` and `history` may be passed in (useApi results) when the parent already fetched them for the same pair;
 * otherwise they are fetched here. `onAnalysis` is told about a new analysis or review so the parent can show it.
 */
export default function ConditionAnalysis({ earlier, later, location, validation: givenValidation, history, onAnalysis }) {
  const status = useApi((s) => getAiStatus(s), [])
  const ownHistory = useApi(
    (s) => getAnalysisHistory({ earlier_record_id: earlier.record_id, later_record_id: later.record_id, limit: 1 }, s),
    [earlier.record_id, later.record_id],
    { enabled: !history },
  )
  const existing = history ?? ownHistory
  // Phase 5A: read-only eligibility check of exactly the selected pair (never calls the AI).
  const ownValidation = useApi(
    (s) => validateImagePair(earlier.record_id, later.record_id, s),
    [earlier.record_id, later.record_id],
    { enabled: !givenValidation },
  )
  const validation = givenValidation ?? ownValidation
  const [acknowledged, setAcknowledged] = useState(false)
  const [local, setLocalState] = useState(null)
  const setLocal = (a) => {
    setLocalState(a)
    onAnalysis?.(a)
  }
  const [running, setRunning] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [error, setError] = useState(null)
  const { user, requireSignIn } = useAuth()

  useEffect(() => {
    if (!running) return undefined
    setElapsed(0)
    const t = setInterval(() => setElapsed((s) => s + 1), 1000)
    return () => clearInterval(t)
  }, [running])

  const analysis = local ?? existing.data?.items?.[0] ?? null
  const available = Boolean(status.data?.available)
  const canReanalyse = analysis && REANALYSABLE.includes(analysis.review.status)
  const check = pairState(validation.data)
  const pairBlocked = validation.loading || Boolean(validation.error) || check === 'blocked' || check === 'unknown'
    || (check === 'warnings' && !acknowledged)
  const blocked = running || !available || (analysis && !canReanalyse) || pairBlocked

  const run = async () => {
    if (!user) return requireSignIn('Sign in to run an AI condition analysis.')
    setRunning(true)
    setError(null)
    try {
      const res = await compareImages({
        earlier_record_id: earlier.record_id,
        later_record_id: later.record_id,
        location: location || null,
        earlier_inspection_date: earlier.inspection_date,
        later_inspection_date: later.inspection_date,
        reanalyse: Boolean(canReanalyse),
        acknowledge_warnings: check === 'warnings' && acknowledged,
      })
      setLocal(res.analysis)
    } catch (err) {
      setError(err)
      if (err.kind === 'analysis_in_progress' || err.kind === 'already_analysed') existing.reload()
    } finally {
      setRunning(false)
    }
  }

  return (
    <section className="card" aria-labelledby="ai-analysis-title">
      <div className="card-header flex-wrap">
        <div className="flex items-start gap-3">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-accent/10 text-accent ring-1 ring-accent/20">
            <BrainCircuit size={17} aria-hidden="true" />
          </span>
          <div>
            <h2 id="ai-analysis-title" className="card-title">AI Condition Analysis</h2>
            <p className="card-subtitle">
              {formatDate(earlier.inspection_date)} → {formatDate(later.inspection_date)} · AI-generated, requires human review
            </p>
          </div>
        </div>
        <div className="flex flex-col items-end gap-1">
          <button
            type="button"
            onClick={run}
            disabled={blocked}
            className="focus-ring inline-flex h-9 items-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-surface-950 transition-colors hover:bg-accent-soft disabled:cursor-not-allowed disabled:opacity-40"
          >
            {running ? <Loader2 size={15} className="animate-spin" aria-hidden="true" /> : canReanalyse ? <RefreshCw size={15} aria-hidden="true" /> : <BrainCircuit size={15} aria-hidden="true" />}
            {canReanalyse ? 'Re-analyse images' : 'Analyse images'}
          </button>
          {!user && available && <span className="text-[11px] text-ink-faint">Sign-in required to run an analysis</span>}
          {status.data && available && !running && pairBlocked && !(analysis && !canReanalyse) && (
            <span className="text-[11px] text-ink-faint">
              {check === 'warnings' ? 'Acknowledge the warnings below to continue' : check === 'blocked' ? 'Blocked by the image-pair check' : 'Checking the image pair…'}
            </span>
          )}
          {status.data && (
            <span className="text-[11px] text-ink-faint" aria-label="AI provider">
              {available
                ? `${PROVIDER_LABELS[status.data.provider] ?? status.data.provider} · ${status.data.model} · ${status.data.simulated ? 'simulated (mock)' : status.data.live ? 'live' : 'offline'} · one request per pair`
                : 'AI service not enabled'}
            </span>
          )}
        </div>
      </div>

      <div className="space-y-4 p-5">
        {status.error ? (
          <p role="alert" className="text-sm text-ink-muted">The AI service status could not be loaded. Analysis is unavailable right now.</p>
        ) : status.data && !available ? (
          <p role="note" className="flex items-start gap-2 rounded-lg border border-surface-600 bg-surface-900 px-3 py-2 text-xs text-ink-muted">
            <AlertTriangle size={14} className="mt-px shrink-0 text-warn" aria-hidden="true" />
            {status.data.message}
          </p>
        ) : null}

        <PairValidation validation={validation} acknowledged={acknowledged} onAcknowledge={setAcknowledged} />

        {running && (
          <div role="status" aria-label="Analysing photographs" className="rounded-lg border border-accent/30 bg-accent/5 px-4 py-3">
            <p className="flex items-center gap-2 text-sm text-ink">
              <Loader2 size={15} className="animate-spin text-accent" aria-hidden="true" />
              Analysing the earlier and later photographs… <span className="tabular-nums text-ink-faint">{elapsed}s</span>
            </p>
            <div className="mt-2 h-1 overflow-hidden rounded bg-surface-700"><div className="h-full w-1/3 animate-pulse rounded bg-accent" /></div>
          </div>
        )}

        {error && (
          <p role="alert" className="flex items-start gap-2 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-ink">
            <AlertTriangle size={15} className="mt-0.5 shrink-0 text-bad" aria-hidden="true" /> {error.message}
          </p>
        )}

        {existing.loading && !local ? (
          <Skeleton className="h-24 w-full rounded-lg" />
        ) : existing.error && !local ? (
          <ErrorState error={existing.error} title="Previous analyses could not be loaded" onRetry={existing.reload} compact />
        ) : analysis ? (
          <AnalysisResult analysis={analysis} onReviewed={setLocal} />
        ) : (
          !running && (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <CheckCircle2 size={22} className="text-ink-faint" aria-hidden="true" />
              <p className="text-sm font-medium text-ink">No AI condition assessment available.</p>
              <p className="max-w-md text-xs text-ink-muted">
                Analysis runs only when requested and compares exactly the two photographs selected above, earlier against later.
              </p>
            </div>
          )
        )}
      </div>
    </section>
  )
}
