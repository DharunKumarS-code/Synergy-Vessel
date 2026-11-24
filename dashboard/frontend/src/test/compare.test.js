import { describe, expect, it } from 'vitest'
import { chronologicalGroups, clampZoom, daysBetween, defaultDates, describeInterval, resolveDates, shortSha } from '../utils/compare'
import { searchResult } from './fixtures'

const dates = ['2026-08-02', '2026-08-30', '2026-09-06']

describe('compare utils', () => {
  it('groups chronologically and drops undated records', () => {
    const g = chronologicalGroups([...searchResult.items, { record_id: 'u', inspection_date: null }])
    expect(g.map((x) => x.date)).toEqual(dates)
  })

  it('defaults to the two most recent dates', () => {
    expect(defaultDates(dates)).toEqual({ earlier: '2026-08-30', later: '2026-09-06' })
    expect(defaultDates(['2026-08-02'])).toEqual({ earlier: null, later: null })
  })

  it('keeps valid pairs and repairs invalid ones (never the same date)', () => {
    expect(resolveDates(dates, '2026-08-02', '2026-09-06')).toEqual({ earlier: '2026-08-02', later: '2026-09-06' })
    expect(resolveDates(dates, '2026-08-30', '2026-08-30')).toEqual({ earlier: '2026-08-30', later: '2026-09-06' })
    expect(resolveDates(dates, '2026-09-06', '2026-08-02')).toEqual({ earlier: '2026-08-30', later: '2026-09-06' })
    expect(resolveDates(dates, '', '2026-08-30')).toEqual({ earlier: '2026-08-02', later: '2026-08-30' })
    expect(resolveDates(dates, 'bogus', 'x')).toEqual(defaultDates(dates))
  })

  it('measures the interval between inspections', () => {
    expect(daysBetween('2026-08-30', '2026-09-06')).toBe(7)
    expect(describeInterval(7)).toBe('7 days (1 week)')
    expect(describeInterval(37)).toBe('37 days (5 weeks 2 days)')
    expect(describeInterval(3)).toBe('3 days')
    expect(daysBetween('x', '2026-09-06')).toBeNull()
  })

  it('clamps zoom and shortens hashes', () => {
    expect(clampZoom(0.5)).toBe(1)
    expect(clampZoom(9)).toBe(4)
    expect(clampZoom(1.6)).toBe(1.5)
    expect(shortSha('a'.repeat(64))).toBe(`${'a'.repeat(12)}…aaaa`)
  })
})
