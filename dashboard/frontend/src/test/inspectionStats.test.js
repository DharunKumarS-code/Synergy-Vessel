import { describe, expect, it } from 'vitest'
import { formatWeekLabel, parseIsoDate } from '../utils/format'
import { activityByDate, summariseInspections, summariseLocations } from '../utils/inspectionStats'
import { inspections, locations } from './fixtures'

describe('summariseInspections', () => {
  const s = summariseInspections(inspections.items)

  it('counts appearances, unique photographs and labelled records', () => {
    expect(s.appearances).toBe(4)
    expect(s.uniquePhotographs).toBe(3)
    expect(s.resolvedImages).toBe(4)
    expect(s.labelled).toBe(3)
    expect(s.unlabelled).toBe(1)
  })

  it('groups weeks in week-number order with dates and date sources', () => {
    expect(s.weeks.map((w) => w.week)).toEqual(['Week 30', 'week 34'])
    expect(s.weeks[1].dates).toEqual(['2026-08-30'])
    expect(s.weeks[1].dateSources).toContain('worksheet_name')
    expect(s.inspectionDates).toEqual(['2026-08-02', '2026-08-30'])
  })

  it('reports unique photographs as unavailable when a hash is missing', () => {
    const items = [...inspections.items, { week: 'Week 30', extracted_photograph: null }]
    expect(summariseInspections(items).uniquePhotographs).toBeNull()
  })

  it('returns null for non-array input', () => {
    expect(summariseInspections(undefined)).toBeNull()
  })
})

describe('summariseLocations', () => {
  it('separates the placeholder from named locations', () => {
    const l = summariseLocations(locations)
    expect(l.namedCount).toBe(2)
    expect(l.named[0].location).toBe('Deck One')
    expect(l.placeholderCount).toBe(1)
    expect(l.placeholderLabel).toBe('Not Labelled')
  })
})

describe('activityByDate', () => {
  it('splits per date by labelled status', () => {
    expect(activityByDate(inspections.items)).toEqual([
      { date: '2026-08-02', week: 'Week 30', labelled: 1, unlabelled: 1 },
      { date: '2026-08-30', week: 'week 34', labelled: 2, unlabelled: 0 },
    ])
  })
})

describe('format', () => {
  it('parses ISO dates without timezone shift', () => {
    const d = parseIsoDate('2026-08-02')
    expect([d.getFullYear(), d.getMonth(), d.getDate()]).toEqual([2026, 7, 2])
    expect(parseIsoDate('8/2/2026')).toBeNull()
  })
  it('normalises week labels for display', () => {
    expect(formatWeekLabel('week 31')).toBe('Week 31')
  })
})
