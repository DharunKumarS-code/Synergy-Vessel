/* SV-05 -- useAutoAnalysis: the real 2 s debounce, one attempt per pair, no automatic retry, stale replies. */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AUTO_ANALYSIS, AUTO_ANALYSIS_DELAY_MS, resetAutoAnalysisRegistry, useAutoAnalysis, useAutoAnalysisSetting } from '../hooks/useAutoAnalysis'

const hook = (props) => renderHook((p) => useAutoAnalysis(p), { initialProps: { userId: 'U1', eligible: true, enabled: true, ...props } })

beforeEach(() => {
  vi.useFakeTimers()
  resetAutoAnalysisRegistry()
  AUTO_ANALYSIS.delayMs = AUTO_ANALYSIS_DELAY_MS
})
afterEach(() => vi.useRealTimers())

describe('useAutoAnalysis', () => {
  it('uses a 2 second debounce by default', () => {
    expect(AUTO_ANALYSIS_DELAY_MS).toBe(2000)
  })

  it('starts only after the pair has stayed selected for 2 s', async () => {
    const start = vi.fn().mockResolvedValue(true)
    const { result } = hook({ pairKey: 'A|B', start })
    expect(result.current.waiting).toBe(true)
    await act(async () => { vi.advanceTimersByTime(1999) })
    expect(start).not.toHaveBeenCalled()
    await act(async () => { vi.advanceTimersByTime(1) })
    expect(start).toHaveBeenCalledTimes(1)
    expect(result.current.status).toBe('done')
  })

  it('clicking quickly through photographs starts nothing for the pairs passed over', async () => {
    const start = vi.fn().mockResolvedValue(true)
    const { rerender } = hook({ pairKey: 'A|B', start })
    for (const k of ['A|C', 'A|D', 'A|E']) {
      await act(async () => { vi.advanceTimersByTime(800) })
      rerender({ userId: 'U1', eligible: true, enabled: true, pairKey: k, start })
    }
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(start).toHaveBeenCalledTimes(1)                      // only the pair that stayed selected: A|E
  })

  it('never starts while not eligible or switched off', async () => {
    const start = vi.fn().mockResolvedValue(true)
    hook({ pairKey: 'A|B', start, eligible: false })
    hook({ pairKey: 'A|C', start, enabled: false })
    await act(async () => { vi.advanceTimersByTime(10_000) })
    expect(start).not.toHaveBeenCalled()
  })

  it('one attempt per pair and user in this tab: reopening the pair does not analyse it again', async () => {
    const start = vi.fn().mockResolvedValue(true)
    const first = hook({ pairKey: 'A|B', start })
    await act(async () => { vi.advanceTimersByTime(2000) })
    first.unmount()
    hook({ pairKey: 'A|B', start })
    await act(async () => { vi.advanceTimersByTime(10_000) })
    expect(start).toHaveBeenCalledTimes(1)
  })

  it('a failure is never retried automatically; retry() is the manual Try Again', async () => {
    const start = vi.fn().mockResolvedValue(false)
    const { result } = hook({ pairKey: 'A|B', start })
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(result.current.status).toBe('failed')
    await act(async () => { vi.advanceTimersByTime(60_000) })
    expect(start).toHaveBeenCalledTimes(1)
    start.mockResolvedValue(true)
    await act(async () => { await result.current.retry() })
    expect(start).toHaveBeenCalledTimes(2)
    expect(result.current.status).toBe('done')
  })

  it('reopening a pair whose request is still running follows it instead of sending another', async () => {
    let finish
    const start = vi.fn(() => new Promise((r) => { finish = r }))
    const first = hook({ pairKey: 'A|B', start })
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(first.result.current.isCurrent()).toBe(true)
    first.unmount()
    expect(first.result.current.isCurrent()).toBe(false)       // a late reply is not shown for the old panel
    const second = hook({ pairKey: 'A|B', start })
    expect(second.result.current.followingEarlierRequest).toBe(true)
    await act(async () => { finish(true) })
    expect(second.result.current.followingEarlierRequest).toBe(false)
    expect(second.result.current.status).toBe('done')
    expect(start).toHaveBeenCalledTimes(1)
  })
})

describe('useAutoAnalysisSetting (SV-06)', () => {
  it('starts ON, can be switched OFF and back ON, is shared by every Compare panel, and is ON again when reopened', () => {
    const a = renderHook(() => useAutoAnalysisSetting())
    expect(a.result.current[0]).toBe(true)
    const b = renderHook(() => useAutoAnalysisSetting())
    act(() => a.result.current[1](false))                                             // manual switch OFF
    expect(a.result.current[0]).toBe(false)
    expect(b.result.current[0]).toBe(false)
    act(() => b.result.current[1](true))                                              // and back ON
    expect(a.result.current[0]).toBe(true)
    act(() => a.result.current[1](false))
    a.unmount(); b.unmount()
    expect(renderHook(() => useAutoAnalysisSetting()).result.current[0]).toBe(true)   // page opened again
    resetAutoAnalysisRegistry()                                                       // a new tab / refresh
    expect(renderHook(() => useAutoAnalysisSetting()).result.current[0]).toBe(true)
  })

  it('never reads a stored value: nothing in sessionStorage can switch it off', () => {
    sessionStorage.setItem('sv_auto_analysis', 'off')
    expect(renderHook(() => useAutoAnalysisSetting()).result.current[0]).toBe(true)
  })
})
