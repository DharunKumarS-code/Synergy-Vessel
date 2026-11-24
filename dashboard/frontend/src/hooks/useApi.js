import { useCallback, useEffect, useRef, useState } from 'react'
import { toApiError } from '../services/api'

/**
 * Runs `fetcher(signal)` whenever `deps` change. Aborts stale requests.
 * Returns { data, error, loading, reload }. `enabled=false` skips the call.
 *
 * `loading` is true until a result for the *current* deps has arrived, so callers
 * never see a stale or empty "loaded" frame between a deps change and the fetch.
 */
export function useApi(fetcher, deps = [], { enabled = true } = {}) {
  const [nonce, setNonce] = useState(0)
  const key = JSON.stringify([...deps, nonce])
  const [state, setState] = useState({ data: null, error: null, key: null })
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  useEffect(() => {
    if (!enabled) return undefined
    const controller = new AbortController()
    fetcherRef
      .current(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ data, error: null, key })
      })
      .catch((err) => {
        const e = toApiError(err)
        if (controller.signal.aborted || e.kind === 'cancelled') return
        setState({ data: null, error: e, key })
      })
    return () => controller.abort()
  }, [key, enabled])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  if (!enabled) return { data: null, error: null, loading: false, reload }
  const current = state.key === key
  return {
    data: current ? state.data : null,
    error: current ? state.error : null,
    loading: !current,
    reload,
  }
}
