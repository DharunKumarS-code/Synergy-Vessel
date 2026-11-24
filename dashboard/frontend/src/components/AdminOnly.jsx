import { ShieldAlert } from 'lucide-react'
import { ROLE_LABELS, useAuth } from '../context/AuthContext'
import { LoadingState } from './ui/StateViews'

/**
 * Shows an administration page only to a signed-in administrator. This is a UI convenience only: every
 * administrative action is still checked by the server (role + session) and recorded in the audit log.
 */
export default function AdminOnly({ children }) {
  const { user, checking, isAdmin, requireSignIn } = useAuth()
  if (checking) return <LoadingState label="Checking access…" rows={2} />
  if (isAdmin) return children
  return (
    <section className="card mx-auto max-w-lg p-8 text-center" aria-labelledby="admin-only-title">
      <ShieldAlert size={28} className="mx-auto text-warn" aria-hidden="true" />
      <h2 id="admin-only-title" className="mt-3 text-base font-semibold text-ink">Administrator access required</h2>
      <p className="mt-1 text-sm text-ink-muted">
        {user ? `You are signed in as ${user.display_name} (${ROLE_LABELS[user.role] ?? user.role}). This page is limited to administrators.`
          : 'Sign in with an administrator account to open this page.'}
      </p>
      {!user && (
        <button type="button" onClick={() => requireSignIn('Sign in with an administrator account.')}
          className="focus-ring mt-4 inline-flex h-9 items-center rounded-lg bg-accent px-4 text-sm font-semibold text-surface-950 hover:bg-accent-soft">
          Sign in
        </button>
      )}
    </section>
  )
}
