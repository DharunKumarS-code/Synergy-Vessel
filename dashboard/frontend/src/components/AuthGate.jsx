import { Loader2, LockKeyhole } from 'lucide-react'
import { Fragment, useEffect } from 'react'
import { useAuth } from '../context/AuthContext'
import { useHealth } from '../hooks/useHealth'
import ConnectionStatus from './ConnectionStatus'
import Logo from './Logo'
import SignInForm from './SignInForm'

/**
 * SV-02 authentication gate. Nothing below it is mounted until a user is signed in, so no vessel, inspection,
 * assessment, report or photo request is ever sent while signed out. When a session ends, the signed-in tree is
 * unmounted (its requests are cancelled) and the sign-in screen returns. The signed-in tree is keyed by the user,
 * so a different account never sees data loaded for the previous one.
 */
export default function AuthGate({ children }) {
  const { user, checking } = useAuth()
  if (checking) {
    return (
      <div className="grid min-h-screen place-items-center" role="status" aria-label="Checking your session">
        <Loader2 size={22} className="animate-spin text-accent" aria-hidden="true" />
      </div>
    )
  }
  if (!user) return <SignInScreen />
  return <Fragment key={user.user_id}>{children}</Fragment>
}

function SignInScreen() {
  const { signIn, notice } = useAuth()
  const health = useHealth()   // public, minimal: { status } only before sign-in

  useEffect(() => {
    document.title = 'Sign in · Synergy Vessel'
  }, [])

  return (
    <main className="grid min-h-screen place-items-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-3">
          <Logo />
          <div className="min-w-0">
            <p className="truncate text-[17px] font-semibold tracking-tight text-ink">Synergy Vessel</p>
            <p className="truncate text-xs text-ink-faint">Inspection Monitoring</p>
          </div>
        </div>
        <section className="card bg-surface-850 p-0" aria-labelledby="sign-in-title">
          <div className="card-header">
            <h1 id="sign-in-title" className="card-title flex items-center gap-2">
              <LockKeyhole size={16} className="text-accent" aria-hidden="true" /> Sign in
            </h1>
          </div>
          <div className="space-y-3 px-5 pb-5">
            {notice && (
              <p role="status" aria-label="Session notice" className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
                {notice}
              </p>
            )}
            <SignInForm onSignIn={signIn} />
          </div>
        </section>
        <div className="mt-4 px-1">
          <ConnectionStatus status={health.status} />
        </div>
      </div>
    </main>
  )
}
