import { useEffect, useState } from 'react'
import { getHealth, toApiError } from '../services/api'

/**
 * Polls GET /api/health. status: 'checking' | 'online' | 'offline'.
 * Checks every `intervalMs` while online and every `offlineIntervalMs` while
 * offline, so recovery is noticed quickly.
 */
export function useHealth(intervalMs = 30000, offlineIntervalMs = 5000) {
  const [state, setState] = useState({ status: 'checking', health: null, checkedAt: null })

  useEffect(() => {
    let alive = true
    let timer
    let controller
    const check = async () => {
      controller = new AbortController()
      let online = false
      try {
        const health = await getHealth(controller.signal)
        online = health?.status === 'ok'
        if (alive) setState({ status: online ? 'online' : 'offline', health, checkedAt: new Date() })
      } catch (err) {
        if (!alive || toApiError(err).kind === 'cancelled') return
        setState({ status: 'offline', health: null, checkedAt: new Date() })
      }
      if (alive) timer = setTimeout(check, online ? intervalMs : offlineIntervalMs)
    }
    check()
    return () => {
      alive = false
      clearTimeout(timer)
      controller?.abort()
    }
  }, [intervalMs, offlineIntervalMs])

  return state
}
