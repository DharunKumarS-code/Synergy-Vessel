import { useCallback, useEffect, useRef, useState } from 'react'
import { recheckPhotoAccess } from '../services/api'

/**
 * SV-02: photos are authorised by an HttpOnly session cookie, so a failed <img> may simply mean the cookie was
 * cleared (another tab signed out) or the session ended. On the first failure, re-check the session once:
 * a live session renews the cookie and the image element is re-created (`attempt` changes its React key -- the
 * URL itself never changes and never carries a token); an ended session returns the app to the sign-in screen.
 * Returns { attempt, onError } where onError resolves to true when a retry was started.
 */
export function usePhotoRetry(imageId) {
  const [attempt, setAttempt] = useState(0)
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  useEffect(() => setAttempt(0), [imageId])

  const onError = useCallback(async () => {
    if (attempt > 0) return false
    const ok = await recheckPhotoAccess()
    if (ok && alive.current) {
      setAttempt(1)
      return true
    }
    return false
  }, [attempt])

  return { attempt, onError }
}
