import { CheckCircle2, Loader2, RotateCcw, XCircle } from 'lucide-react'
import { useId, useRef, useState } from 'react'
import { REVIEW_ROLES, useAuth } from '../../context/AuthContext'
import { reviewAnalysis } from '../../services/api'

/*
 * Approve / Reject / Request Reassessment for one saved AI assessment.
 * The only review UI in the application: used inline on Compare Photos and on the Reviews page. It calls the existing
 * POST /api/ai-analysis/{id}/review, so the server still checks the session and role, records the reviewer from the
 * session, keeps the original AI result unchanged and writes the audit log entry. Nothing is decided automatically.
 *
 * SV-03: the server enforces the review rules (app/ai/review_policy.py); this component only avoids offering what the
 * server would refuse. An assessment awaiting review can be decided once. A decision is then final for reviewers; an
 * administrator can correct it with a reason. A reviewer cannot approve an analysis they ran themselves.
 */

export const REVIEW_LABELS = {
  PENDING_REVIEW: 'Awaiting review',
  APPROVED: 'Approved',
  REJECTED: 'Rejected',
  NEEDS_REASSESSMENT: 'Reassessment requested',
}

const DECISIONS = [
  { status: 'APPROVED', label: 'Approve', icon: CheckCircle2, done: 'Assessment approved.',
    question: 'Approve this assessment? It will be recorded as confirmed by you.',
    cls: 'bg-accent text-surface-950 hover:bg-accent-soft' },
  { status: 'REJECTED', label: 'Reject', icon: XCircle, done: 'Assessment rejected.',
    question: 'Reject this assessment? The AI result will not be treated as a condition result, and the photos can be analysed again.',
    cls: 'border border-bad/50 text-bad hover:bg-bad/10' },
  { status: 'NEEDS_REASSESSMENT', label: 'Request Reassessment', icon: RotateCcw, done: 'Reassessment requested.',
    question: 'Ask for these photos to be analysed again? The current result will be shown as needing review.',
    cls: 'border border-warn/50 text-warn hover:bg-warn/10' },
]
export const MIN_CORRECTION_REASON = 10 // matches the server (app/ai/review_policy.py)
const btn = 'focus-ring inline-flex h-10 items-center gap-2 rounded-lg px-4 text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40'

/**
 * `pendingOnly` (Compare Photos): offer decisions only while the assessment is awaiting review.
 * Otherwise (Reviews page) an administrator may also correct a decided assessment, giving a reason.
 * `offer={false}` shows no decisions (e.g. a test result), but keeps a just-saved confirmation message visible.
 */
export default function ReviewActions({ analysis, onReviewed, pendingOnly = false, offer = true }) {
  const id = useId()
  const { user, requireSignIn } = useAuth()
  const [pending, setPending] = useState(null) // the decision awaiting confirmation
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [done, setDone] = useState(null)
  const inFlight = useRef(false) // blocks a second request even before "saving" has re-rendered

  const current = analysis.review?.status
  const success = done && <p role="status" aria-label="Review saved" className="text-sm font-medium text-ok">{done}</p>
  if (!offer || (pendingOnly && current !== 'PENDING_REVIEW')) return success || null
  if (!user) {
    return (
      <div className="flex flex-wrap items-center justify-center gap-3 text-sm text-ink-muted">
        <span>Sign in as a reviewer to approve or reject this assessment.</span>
        <button type="button" onClick={() => requireSignIn('Sign in to review this assessment.')}
          className="focus-ring rounded-lg border border-accent/40 px-3 py-1.5 text-sm font-semibold text-accent hover:bg-accent/10">Sign in to review</button>
      </div>
    )
  }
  // SV-02: viewers see assessments but never decision controls (the server refuses them too).
  if (!REVIEW_ROLES.includes(user.role)) {
    return current === 'PENDING_REVIEW'
      ? <p className="text-center text-xs text-ink-muted" aria-label="View only">You have view-only access. A reviewer records the decision for this assessment.</p>
      : success || null
  }

  const isAdmin = user.role === 'admin'
  const decided = Boolean(current) && current !== 'PENDING_REVIEW'
  // SV-02: computed by the server for the signed-in user; account identifiers are not sent to reviewers.
  const ownAnalysis = analysis.requested_by_me === true
  const selfApprovalBlocked = !decided && ownAnalysis && !isAdmin
  const offered = !decided
    ? DECISIONS.filter((d) => !(d.status === 'APPROVED' && selfApprovalBlocked))
    : isAdmin ? DECISIONS.filter((d) => d.status !== current) : []   // corrections: administrators only
  const reasonMissing = decided && note.trim().length < MIN_CORRECTION_REASON
  const confirm = async () => {
    if (inFlight.current || !pending || reasonMissing) return
    inFlight.current = true
    setSaving(true)
    setError(null)
    try {
      const updated = await reviewAnalysis(analysis.analysis_id, { status: pending.status, note: note.trim() || null })
      setDone(pending.done)
      setPending(null)
      setNote('')
      onReviewed?.(updated)
    } catch (err) {
      setError(err)
    } finally {
      inFlight.current = false
      setSaving(false)
    }
  }

  return (
    <div className="flex w-full flex-col items-center gap-3" role="group" aria-label="Review this assessment">
      {success}
      {decided && !isAdmin && !pending && !done && (
        <p className="text-sm text-ink-muted">This assessment has been decided. Only an administrator can correct a decision.</p>
      )}
      {selfApprovalBlocked && !pending && (
        <p className="text-sm text-ink-muted">You ran this analysis, so another reviewer or an administrator must approve it.</p>
      )}
      {!pending ? (
        offered.length > 0 && (
        <div className="flex flex-wrap justify-center gap-2">
          {offered.map((d) => (
            <button key={d.status} type="button" className={`${btn} ${d.cls}`} disabled={saving}
              onClick={() => { setDone(null); setError(null); setNote(''); setPending(d) }}>
              <d.icon size={16} aria-hidden="true" /> {decided ? `Change to ${REVIEW_LABELS[d.status]}` : d.label}
            </button>
          ))}
        </div>
        )
      ) : (
        <div role="group" aria-label="Confirm review decision" className="w-full max-w-xl rounded-xl border border-surface-600 bg-surface-900 p-4 text-left">
          <p className="text-sm font-medium text-ink">
            {decided
              ? `Correct this decision from ${REVIEW_LABELS[current] ?? current} to ${REVIEW_LABELS[pending.status]}? The earlier decision stays in the review history.`
              : pending.question}
          </p>
          <label htmlFor={`${id}-note`} className="mt-3 block text-xs text-ink-faint">
            {decided ? `Reason for the correction (required, at least ${MIN_CORRECTION_REASON} characters)` : 'Note (optional)'}
          </label>
          <textarea id={`${id}-note`} value={note} maxLength={1000} rows={2} onChange={(e) => setNote(e.target.value)} disabled={saving}
            className="focus-ring mt-1 w-full rounded-lg border border-surface-600 bg-surface-800 px-3 py-2 text-sm text-ink" />
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" onClick={confirm} disabled={saving || reasonMissing} className={`${btn} ${pending.cls}`}>
              {saving ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : <pending.icon size={16} aria-hidden="true" />}
              {saving ? 'Saving…' : decided ? `Confirm: Change to ${REVIEW_LABELS[pending.status]}` : `Confirm: ${pending.label}`}
            </button>
            <button type="button" onClick={() => setPending(null)} disabled={saving}
              className={`${btn} border border-surface-600 text-ink-muted hover:text-ink`}>Cancel</button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-bad">{error.message}</p>}
    </div>
  )
}

/** Who decided what, newest first (read from the saved assessment; never edited here). */
export function ReviewHistory({ analysis }) {
  const history = analysis.review?.history ?? []
  if (!history.length) return null
  return (
    <ol aria-label="Review history" className="space-y-1 text-xs text-ink-muted">
      {[...history].reverse().map((h, i) => (
        <li key={i}>
          <span className="font-medium text-ink">{REVIEW_LABELS[h.status] ?? h.status}</span>{h.correction && ' (correction)'} by {h.reviewer}
          {' · '}{new Date(h.at).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' })}
          {h.note && <span className="block text-ink-faint">“{h.note}”</span>}
        </li>
      ))}
    </ol>
  )
}
