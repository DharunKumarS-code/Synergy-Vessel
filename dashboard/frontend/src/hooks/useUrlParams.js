import { useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * useSearchParams with a merge-safe updater.
 *
 * React Router's functional setSearchParams receives the params from the last
 * *render*, so two updates issued before a re-render (e.g. quick successive clicks
 * while a transition is pending) overwrite each other. Here every update builds on
 * the latest params we produced, so consecutive patches compose.
 */
export function useUrlParams() {
  const [params, setParams] = useSearchParams()
  const latest = useRef(params)
  const rendered = useRef(params)
  if (rendered.current !== params) {
    rendered.current = params
    latest.current = params
  }
  const mutate = useCallback(
    (fn, { replace = true } = {}) => {
      const next = new URLSearchParams(latest.current)
      fn(next)
      latest.current = next
      setParams(next, { replace })
    },
    [setParams],
  )
  return [params, mutate]
}
