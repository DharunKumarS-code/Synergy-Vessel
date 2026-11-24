import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * SV-05: automatic AI condition analysis of the selected photograph pair.
 *
 * The caller decides *whether* a pair is eligible (both photos, reviewer/admin, pair check passed, warnings
 * acknowledged, AI available, no saved assessment). This hook decides *when* and *how often*:
 *
 *  - debounce: the request starts only after the pair has stayed selected for `delayMs` (default 2 s), so clicking
 *    through photographs starts nothing;
 *  - at most one automatic attempt per pair and signed-in user in this browser tab (a module-level registry that
 *    survives the panel being re-created when the user moves away and back). A failed attempt is never retried
 *    automatically; `retry()` is the manual "Try again";
 *  - stale replies: `isCurrent()` is false once the panel for this pair has unmounted (another pair was chosen), so a
 *    late reply is never shown for photographs it does not belong to. The server still saves it; it appears as the
 *    saved result the next time that pair is opened.
 *
 * Duplicate assessments are also prevented by the server: an already analysed pair is returned as `reused` without
 * calling the AI, and a second concurrent request for the same pair is refused (409 analysis_in_progress).
 */
export const AUTO_ANALYSIS_DELAY_MS = 2000
/** Runtime settings (tests shorten the delay; the application always uses AUTO_ANALYSIS_DELAY_MS). */
export const AUTO_ANALYSIS = { delayMs: AUTO_ANALYSIS_DELAY_MS }

const registry = new Map() // `${userId}|${pairKey}` -> { status: 'running' | 'done' | 'failed', promise }

/*
 * SV-06: the reviewer's "Analyse automatically" switch is OFF by default. The choice lives only in this module's
 * memory: it holds while the user moves around the app in this tab, but a new tab, a page refresh or a code reload
 * always starts OFF again, so automatic AI requests never begin without a deliberate choice. Nothing is persisted and
 * nothing is shared between tabs. (SV-05 kept the choice in sessionStorage with ON as the default; that key is no
 * longer read and is removed.)
 */
let autoAnalysisOn = false
const listeners = new Set()
try { sessionStorage.removeItem('sv_auto_analysis') } catch { /* storage unavailable */ }

/** [on, setOn] for the "Analyse automatically" switch (OFF until the user turns it on in this tab). */
export function useAutoAnalysisSetting() {
  const [on, setOnState] = useState(autoAnalysisOn)
  useEffect(() => {
    listeners.add(setOnState)
    setOnState(autoAnalysisOn)
    return () => { listeners.delete(setOnState) }
  }, [])
  const setOn = useCallback((value) => {
    autoAnalysisOn = Boolean(value)
    listeners.forEach((l) => l(autoAnalysisOn))
  }, [])
  return [on, setOn]
}

/** Test helper: the state of a freshly opened tab (no attempts, switch OFF). */
export const resetAutoAnalysisRegistry = () => {
  registry.clear()
  autoAnalysisOn = false
}

/** Test helper: the user has already switched automatic analysis ON in this tab. */
export const enableAutoAnalysisForTab = () => { autoAnalysisOn = true }

export function useAutoAnalysis({ pairKey, userId, eligible, enabled = true, start, delayMs = AUTO_ANALYSIS.delayMs }) {
  const key = `${userId ?? 'anonymous'}|${pairKey}`
  const alive = useRef(true)
  const startRef = useRef(start)
  startRef.current = start
  const [entry, setEntry] = useState(() => registry.get(key) ?? null)
  const [waiting, setWaiting] = useState(false)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const isCurrent = useCallback(() => alive.current, [])

  const launch = useCallback(() => {
    const promise = Promise.resolve()
      .then(() => startRef.current())
      .then((ok) => {
        const done = { status: ok === false ? 'failed' : 'done', promise: null }
        registry.set(key, done)
        if (alive.current) setEntry(done)
        return ok
      })
    const running = { status: 'running', promise }
    registry.set(key, running)
    setEntry(running)
    return promise
  }, [key])

  // Debounced automatic start.
  useEffect(() => {
    if (!eligible || !enabled || !pairKey || registry.has(key)) {
      setWaiting(false)
      return undefined
    }
    setWaiting(true)
    const t = setTimeout(() => {
      setWaiting(false)
      if (!registry.has(key)) launch()
    }, delayMs)
    return () => { clearTimeout(t); setWaiting(false) }
  }, [eligible, enabled, pairKey, key, delayMs, launch])

  // Reopened while an earlier automatic request for this pair is still running: follow it to the end.
  const [followed, setFollowed] = useState(false)
  useEffect(() => {
    const e = registry.get(key)
    if (e?.status !== 'running' || !e.promise) return
    setFollowed(true)
    e.promise.finally(() => { if (alive.current) { setEntry(registry.get(key) ?? null); setFollowed(false) } })
  }, [key])

  return {
    /** Counting down the debounce before an automatic start. */
    waiting,
    /** 'running' | 'done' | 'failed' | null for this pair in this tab. */
    status: entry?.status ?? null,
    /** An automatic request started in an earlier visit to this pair is still running. */
    followingEarlierRequest: followed,
    /** Manual "Try again" (after a failure) -- never called automatically. */
    retry: launch,
    isCurrent,
  }
}
