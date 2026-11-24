import { LockKeyhole, X } from 'lucide-react'
import { useEffect, useId } from 'react'
import SignInForm from './SignInForm'

/** Modal sign-in form (e.g. "Sign in" from a session notice). The form itself is shared with the sign-in screen. */
export default function SignInDialog({ reason, onSignIn, onClose }) {
  const id = useId()

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-[60] grid place-items-center bg-surface-950/70 p-4 backdrop-blur-sm">
      <div role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} className="card w-full max-w-sm bg-surface-850 p-0">
        <div className="card-header">
          <h2 id={`${id}-title`} className="card-title flex items-center gap-2">
            <LockKeyhole size={16} className="text-accent" aria-hidden="true" /> Sign in
          </h2>
          <button type="button" onClick={onClose} aria-label="Close sign-in" className="focus-ring rounded-md p-1 text-ink-muted hover:text-ink">
            <X size={16} />
          </button>
        </div>
        <div className="px-5 pb-5">
          <SignInForm reason={reason} onSignIn={onSignIn} />
        </div>
      </div>
    </div>
  )
}
