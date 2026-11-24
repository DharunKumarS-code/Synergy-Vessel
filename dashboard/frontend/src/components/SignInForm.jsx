import { Loader2 } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'

/**
 * Username + password form shared by the sign-in screen and the sign-in dialog. The password is only held in
 * component state until the request is sent, and is cleared after a failed attempt.
 */
export default function SignInForm({ reason, onSignIn, autoFocus = true }) {
  const id = useId()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const first = useRef(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    if (autoFocus) first.current?.focus()
    return () => { alive.current = false }
  }, [autoFocus])

  const submit = async (e) => {
    e.preventDefault()
    if (!username.trim() || !password) return setError('Enter your username and password.')
    setBusy(true)
    setError(null)
    try {
      await onSignIn(username.trim(), password)
    } catch (err) {
      if (!alive.current) return
      setError(err.message || 'Sign-in failed.')
      setPassword('')
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  const field = 'focus-ring h-10 w-full rounded-lg border border-surface-600 bg-surface-800 px-3 text-sm text-ink'
  return (
    <form onSubmit={submit} className="space-y-3" aria-label="Sign in">
      {reason && <p className="text-xs text-ink-muted">{reason}</p>}
      <div>
        <label htmlFor={`${id}-user`} className="mb-1 block text-xs text-ink-faint">Username</label>
        <input id={`${id}-user`} ref={first} value={username} autoComplete="username" maxLength={64}
          onChange={(e) => setUsername(e.target.value)} className={field} />
      </div>
      <div>
        <label htmlFor={`${id}-pass`} className="mb-1 block text-xs text-ink-faint">Password</label>
        <input id={`${id}-pass`} type="password" value={password} autoComplete="current-password" maxLength={256}
          onChange={(e) => setPassword(e.target.value)} className={field} />
      </div>
      {error && <p role="alert" className="rounded-lg border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad">{error}</p>}
      <button type="submit" disabled={busy}
        className="focus-ring inline-flex h-10 w-full items-center justify-center gap-2 rounded-lg bg-accent text-sm font-semibold text-surface-950 hover:bg-accent-soft disabled:opacity-50">
        {busy && <Loader2 size={15} className="animate-spin" aria-hidden="true" />} Sign in
      </button>
      <p className="text-[11px] text-ink-faint">An account is needed to view inspections, photos and reports. Accounts are created by an administrator.</p>
    </form>
  )
}
