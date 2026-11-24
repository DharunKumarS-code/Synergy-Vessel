import { describe, expect, it } from 'vitest'
import { searchInspections, http } from '../services/api'
import {
  UNDATED,
  adjacentDates,
  filterItems,
  groupByDate,
  matchSource,
  summariseSearch,
  visibleItems,
} from '../utils/searchResults'
import { searchResult } from './fixtures'
import { vi } from 'vitest'

const items = searchResult.items

describe('matchSource', () => {
  it('distinguishes official, AI and both', () => {
    expect(items.map(matchSource)).toEqual(['ai', 'official', 'both', 'official'])
    expect(matchSource({})).toBe('none')
  })
})

describe('summariseSearch', () => {
  it('counts appearances, unique photos, dates, weeks and sources', () => {
    const s = summariseSearch(items)
    expect(s.appearances).toBe(4)
    expect(s.uniquePhotographs).toBe(3) // W30-0002 and W34-0001 share a photo
    expect(s.dates).toEqual(['2026-08-02', '2026-08-30', '2026-09-06'])
    expect(s.weeks).toEqual(['Week 30', 'week 34', 'week 35'])
    expect(s.bySource).toEqual({ official: 2, ai: 1, both: 1 })
    expect(s.missingImages).toBe(1)
  })
})

describe('filterItems', () => {
  it('filters by source (official/ai include records matching both)', () => {
    const ids = (src) => filterItems(items, { source: src }).map((r) => r.record_id)
    expect(ids('official')).toEqual(['W30-0002', 'W34-0001', 'week_35-json-0000'])
    expect(ids('ai')).toEqual(['W30-0001', 'W34-0001'])
    expect(ids('both')).toEqual(['W34-0001'])
    expect(ids('all')).toHaveLength(4)
  })
  it('filters by week', () => {
    expect(filterItems(items, { week: 'Week 30' }).map((r) => r.record_id)).toEqual(['W30-0001', 'W30-0002'])
  })
})

describe('groupByDate', () => {
  it('sorts groups newest or oldest first and keeps every appearance', () => {
    const newest = groupByDate(items, 'newest')
    expect(newest.map((g) => g.date)).toEqual(['2026-09-06', '2026-08-30', '2026-08-02'])
    expect(newest[2].items.map((r) => r.record_id)).toEqual(['W30-0001', 'W30-0002'])
    expect(groupByDate(items, 'oldest').map((g) => g.date)).toEqual(['2026-08-02', '2026-08-30', '2026-09-06'])
    expect(newest.reduce((n, g) => n + g.count, 0)).toBe(4)
  })
  it('flags worksheet-derived dates and unavailable photos', () => {
    const g = groupByDate(items).find((x) => x.date === '2026-08-30')
    expect(g.derived).toBe(true)
    expect(g.reportDates).toEqual(['AMBIGUOUS/PENDING'])
    expect(groupByDate(items).find((x) => x.date === '2026-09-06').unavailable).toBe(1)
  })
  it('puts undated records last', () => {
    const g = groupByDate([...items, { record_id: 'x', inspection_date: null }], 'oldest')
    expect(g.at(-1).date).toBe(UNDATED)
  })
})

describe('visibleItems / adjacentDates', () => {
  const groups = groupByDate(items, 'newest')
  it('returns one date or all dates in group order', () => {
    expect(visibleItems(groups, '2026-08-02')).toHaveLength(2)
    expect(visibleItems(groups, '').map((r) => r.record_id)[0]).toBe('week_35-json-0000')
  })
  it('finds chronological neighbours regardless of sort', () => {
    expect(adjacentDates(groups, '2026-08-30')).toEqual({ older: '2026-08-02', newer: '2026-09-06' })
    expect(adjacentDates(groups, '2026-08-02')).toEqual({ older: null, newer: '2026-08-30' })
  })
})

describe('searchInspections', () => {
  it('requests a trimmed, partial search across official and AI locations', async () => {
    const spy = vi.spyOn(http, 'get').mockResolvedValue({ data: searchResult })
    await searchInspections({ location: '  Main Deck ', vessel: 'v1' })
    expect(spy).toHaveBeenCalledWith('/api/inspections/search', {
      params: { location: 'Main Deck', vessel: 'v1', match: 'contains', include_ai_location: 'true' },
      signal: undefined,
    })
    spy.mockRestore()
  })
})
