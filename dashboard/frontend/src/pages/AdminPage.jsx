import { CheckCircle2, Loader2, ShieldAlert, ShieldCheck, UserPlus, XCircle } from 'lucide-react'
import { useCallback, useEffect, useId, useState } from 'react'
import { EmptyState, ErrorState, Skeleton } from '../components/ui/StateViews'
import { ROLE_LABELS, useAuth } from '../context/AuthContext'
import { createUser, getAuditEntries, listUsers, updateUser, verifyAudit } from '../services/api'

/* Phase 8.2 -- administration: accounts, roles and the audit log. The server enforces every rule
   (admin only, last-admin protection, no self-demotion); this page only presents them. */

const field = 'focus-ring h-9 w-full rounded-lg border border-surface-600 bg-surface-800 px-3 text-sm text-ink'
const btn = 'focus-ring inline-flex h-9 items-center gap-2 rounded-lg px-3 text-sm font-semibold disabled:opacity-50'
const when = (iso) => (iso ? new Date(iso).toLocaleString('en-GB') : '—')

function CreateUser({ onCreated }) {
  const id = useId()
  const empty = { username: '', display_name: '', role: 'reviewer', password: '' }
  const [form, setForm] = useState(empty)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))
  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setMessage(null)
    try {
      const u = await createUser({ ...form, display_name: form.display_name.trim() || null })
      setForm(empty)
      setMessage({ ok: true, text: `Account '${u.username}' created.` })
      onCreated()
    } catch (err) {
      setMessage({ ok: false, text: err.message })
    } finally {
      setBusy(false)
    }
  }
  return (
    <form onSubmit={submit} aria-label="Create account" className="grid gap-3 p-5 sm:grid-cols-2 lg:grid-cols-5 lg:items-end">
      <div><label htmlFor={`${id}-u`} className="mb-1 block text-xs text-ink-faint">Username</label>
        <input id={`${id}-u`} value={form.username} onChange={set('username')} maxLength={32} autoComplete="off" className={field} /></div>
      <div><label htmlFor={`${id}-d`} className="mb-1 block text-xs text-ink-faint">Display name</label>
        <input id={`${id}-d`} value={form.display_name} onChange={set('display_name')} maxLength={80} className={field} /></div>
      <div><label htmlFor={`${id}-r`} className="mb-1 block text-xs text-ink-faint">Role</label>
        <select id={`${id}-r`} value={form.role} onChange={set('role')} className={field}>
          <option value="viewer">Viewer (view only)</option><option value="reviewer">Reviewer</option><option value="admin">Administrator</option>
        </select></div>
      <div><label htmlFor={`${id}-p`} className="mb-1 block text-xs text-ink-faint">Initial password (12+ characters)</label>
        <input id={`${id}-p`} type="password" value={form.password} onChange={set('password')} maxLength={256} autoComplete="new-password" className={field} /></div>
      <button type="submit" disabled={busy || !form.username || !form.password} className={`${btn} bg-accent text-surface-950 hover:bg-accent-soft`}>
        {busy ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <UserPlus size={14} aria-hidden="true" />} Create account
      </button>
      {message && <p role={message.ok ? 'status' : 'alert'} className={`text-xs sm:col-span-2 lg:col-span-5 ${message.ok ? 'text-ok' : 'text-bad'}`}>{message.text}</p>}
    </form>
  )
}

function UserRow({ u, self, onChanged, onError }) {
  const [busy, setBusy] = useState(false)
  const change = async (body, confirmText) => {
    if (confirmText && !window.confirm(confirmText)) return
    setBusy(true)
    try {
      await updateUser(u.user_id, body)
      onChanged()
    } catch (err) {
      onError(err.message)
    } finally {
      setBusy(false)
    }
  }
  const reset = () => {
    const pw = window.prompt(`New password for ${u.username} (12+ characters). They will be signed out.`)
    if (pw) change({ password: pw })
  }
  return (
    <tr className="border-t border-surface-700/60">
      <td className="px-3 py-2"><span className="font-medium text-ink">{u.display_name}</span><span className="block text-xs text-ink-faint">{u.username}{self && ' · you'}</span></td>
      <td className="px-3 py-2">{ROLE_LABELS[u.role]}</td>
      <td className="px-3 py-2">{u.active ? <span className="text-ok">Active</span> : <span className="text-bad">Disabled</span>}</td>
      <td className="px-3 py-2 text-xs text-ink-faint">{when(u.created_at)}</td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap gap-2">
          {/* SV-02: three roles; an administrator cannot change their own role (the server enforces this too). */}
          <select aria-label={`Role for ${u.username}`} value={u.role} disabled={busy || self}
            onChange={(e) => change({ role: e.target.value })}
            className="focus-ring h-9 rounded-lg border border-surface-600 bg-surface-800 px-2 text-sm text-ink disabled:opacity-50">
            <option value="viewer">Viewer</option><option value="reviewer">Reviewer</option><option value="admin">Administrator</option>
          </select>
          <button type="button" disabled={busy || self} className={`${btn} border border-surface-600 text-ink-muted hover:text-ink`}
            onClick={() => change({ active: !u.active }, u.active ? `Disable ${u.username}? They will be signed out immediately.` : null)}>
            {u.active ? 'Disable' : 'Enable'}
          </button>
          <button type="button" disabled={busy} className={`${btn} border border-surface-600 text-ink-muted hover:text-ink`} onClick={reset}>Reset password</button>
        </div>
      </td>
    </tr>
  )
}

function Users() {
  const { user } = useAuth()
  const [state, setState] = useState({ loading: true, data: null, error: null })
  const [problem, setProblem] = useState(null)
  const load = useCallback(() => {
    setState((s) => ({ ...s, loading: true }))
    listUsers().then((data) => setState({ loading: false, data, error: null }), (error) => setState({ loading: false, data: null, error }))
  }, [])
  useEffect(load, [load])
  return (
    <section className="card" aria-labelledby="users-title">
      <div className="card-header"><div>
        <h2 id="users-title" className="card-title">User accounts</h2>
        <p className="card-subtitle">Reviewers can run analyses and record review decisions. Administrators can also manage accounts and imports.</p>
      </div></div>
      <CreateUser onCreated={load} />
      {problem && <p role="alert" className="mx-5 mb-3 rounded-lg border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad">{problem}</p>}
      <div className="overflow-x-auto px-2 pb-4">
        {state.loading && !state.data ? <Skeleton className="m-3 h-20 rounded-lg" />
          : state.error ? <ErrorState error={state.error} title="Accounts could not be loaded" onRetry={load} compact />
            : (
              <table className="w-full min-w-[640px] text-left text-sm text-ink-muted" aria-label="User accounts">
                <thead><tr className="text-xs uppercase tracking-wide text-ink-faint">
                  {['Account', 'Role', 'Status', 'Created', 'Actions'].map((h) => <th key={h} className="px-3 py-2 font-medium">{h}</th>)}
                </tr></thead>
                <tbody>
                  {state.data.users.map((u) => (
                    <UserRow key={u.user_id} u={u} self={u.user_id === user.user_id} onChanged={() => { setProblem(null); load() }} onError={setProblem} />
                  ))}
                </tbody>
              </table>
            )}
      </div>
    </section>
  )
}

function Audit() {
  const [check, setCheck] = useState(null)
  const [entries, setEntries] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      const [v, e] = await Promise.all([verifyAudit(), getAuditEntries(25)])
      setCheck(v)
      setEntries(e.entries)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }
  useEffect(() => { run() }, [])
  return (
    <section className="card" aria-labelledby="audit-title">
      <div className="card-header flex-wrap">
        <div>
          <h2 id="audit-title" className="card-title">Audit log</h2>
          <p className="card-subtitle">Append-only and hash-chained: any edited, removed or reordered entry is detected.</p>
        </div>
        <button type="button" onClick={run} disabled={busy} className={`${btn} border border-surface-600 text-ink-muted hover:text-ink`}>
          {busy && <Loader2 size={14} className="animate-spin" aria-hidden="true" />} Verify integrity
        </button>
      </div>
      <div className="space-y-3 p-5">
        {error && <ErrorState error={error} title="The audit log could not be checked" compact />}
        {check && (
          <p role="status" aria-label="Audit integrity" className={`flex items-start gap-2 text-sm ${check.valid ? 'text-ok' : 'text-bad'}`}>
            {check.valid ? <CheckCircle2 size={16} className="mt-0.5 shrink-0" aria-hidden="true" /> : <XCircle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />}
            <span>{check.valid ? `Verified: ${check.entries} entries, chain intact.` : `Integrity problems found in ${check.entries} entries:`}
              {!check.valid && <span className="block text-xs">{check.problems.join(' · ')}</span>}</span>
          </p>
        )}
        {entries && (entries.length === 0 ? <EmptyState title="No audit entries yet" /> : (
          <ol className="divide-y divide-surface-700/60 text-xs text-ink-muted" aria-label="Recent audit entries">
            {entries.map((e) => (
              <li key={e.seq} className="flex flex-wrap gap-x-3 py-1.5">
                <span className="w-10 text-ink-faint">#{e.seq}</span>
                <span className="w-40 text-ink-faint">{when(e.at)}</span>
                <span className="font-medium text-ink">{e.event.replaceAll('_', ' ')}</span>
                <span>{e.actor?.username || 'anonymous'}</span>
                {e.target && <span className="text-ink-faint">→ {e.target}</span>}
              </li>
            ))}
          </ol>
        ))}
      </div>
    </section>
  )
}

export default function AdminPage() {
  const { user, isAdmin, checking, requireSignIn } = useAuth()
  if (checking) return <Skeleton className="h-40 w-full rounded-xl" />
  if (!isAdmin) {
    return (
      <section className="card p-8 text-center" aria-labelledby="admin-locked">
        <ShieldAlert size={28} className="mx-auto text-warn" aria-hidden="true" />
        <h2 id="admin-locked" className="mt-3 text-base font-semibold text-ink">Administrator access required</h2>
        <p className="mt-1 text-sm text-ink-muted">
          {user ? `You are signed in as ${user.display_name} (${ROLE_LABELS[user.role]}). This page is limited to administrators.`
            : 'Sign in with an administrator account to manage users and review the audit log.'}
        </p>
        {!user && (
          <button type="button" onClick={() => requireSignIn('Sign in with an administrator account.')}
            className={`${btn} mt-4 bg-accent text-surface-950 hover:bg-accent-soft`}>Sign in</button>
        )}
      </section>
    )
  }
  return (
    <div className="space-y-6">
      <p className="flex items-center gap-2 text-sm text-ink-muted"><ShieldCheck size={16} className="text-accent" aria-hidden="true" /> Signed in as {user.display_name} (Administrator)</p>
      <Users />
      <Audit />
    </div>
  )
}
