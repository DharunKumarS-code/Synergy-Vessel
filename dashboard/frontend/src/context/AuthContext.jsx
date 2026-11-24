import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import SignInDialog from '../components/SignInDialog'
import { SESSION_EXPIRED_EVENT, getSession, login, logout } from '../services/api'

/**
 * Authentication state. SV-02: every page needs a signed-in account -- <AuthGate> shows the sign-in screen until
 * `user` is set and mounts the application only afterwards. `requireSignIn()` still opens the sign-in dialog.
 * The server is the authority: it re-checks the session and role on every call (hiding a button is never the
 * only protection).
 */
const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [checking, setChecking] = useState(true)
  const [dialog, setDialog] = useState(null) // null | { reason }
  const [notice, setNotice] = useState(null)
  const userRef = useRef(null)
  userRef.current = user

  useEffect(() => {
    let alive = true
    getSession()
      .then((s) => alive && setUser(s.user))
      .catch(() => alive && setUser(null))
      .finally(() => alive && setChecking(false))
    const expired = () => {
      // Only a session that was in use "expires"; a stale token found at start-up just shows the sign-in screen.
      if (userRef.current) setNotice('Your session has expired or was ended. Sign in again to continue.')
      setUser(null)
      logout()   // also asks the server to clear the photo cookie (the session itself is already gone)
    }
    window.addEventListener(SESSION_EXPIRED_EVENT, expired)
    return () => {
      alive = false
      window.removeEventListener(SESSION_EXPIRED_EVENT, expired)
    }
  }, [])

  const signIn = useCallback(async (username, password) => {
    const u = await login(username, password)
    setUser(u)
    setNotice(null)
    setDialog(null)
    return u
  }, [])

  const signOut = useCallback(async () => {
    try {
      await logout()
    } finally {
      setUser(null)
      setNotice(null)
    }
  }, [])

  const requireSignIn = useCallback((reason) => setDialog({ reason: reason || null }), [])

  const value = useMemo(() => ({
    user, checking, notice, signIn, signOut, requireSignIn,
    isAdmin: user?.role === 'admin',
    canReview: REVIEW_ROLES.includes(user?.role),   // SV-02: run analyses and record review decisions
    dismissNotice: () => setNotice(null),
  }), [user, checking, notice, signIn, signOut, requireSignIn])

  return (
    <AuthContext.Provider value={value}>
      {children}
      {dialog && <SignInDialog reason={dialog.reason} onSignIn={signIn} onClose={() => setDialog(null)} />}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}

/** Labels for the three roles (SV-02 added the read-only viewer). */
export const ROLE_LABELS = { viewer: 'Viewer', reviewer: 'Reviewer', admin: 'Administrator' }
/** Roles that may run AI analyses and record review decisions (the server enforces the same rule). */
export const REVIEW_ROLES = ['reviewer', 'admin']
