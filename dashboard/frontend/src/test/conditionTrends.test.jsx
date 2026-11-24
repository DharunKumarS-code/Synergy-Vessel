/* Condition Trends: saved AI condition analyses over time. Mocked API responses and synthetic analyses only;
   nothing here runs, requests or reviews an analysis. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import * as api from '../services/api'
import {
  analysisLocation, byLaterWeek, byLocation, filterAnalyses, isConfirmedChange, summarise,
} from '../utils/conditionTrends'
import * as fx from './fixtures'

vi.mock('../services/api', async (orig) => {
  const actual = await orig()
  return {
    ...actual,
    getHealth: vi.fn(), getVessels: vi.fn(), getLocations: vi.fn(), getAllInspections: vi.fn(), searchInspections: vi.fn(),
    getSession: vi.fn(), getAllAnalyses: vi.fn(), getAnalysisHistory: vi.fn(), getAnalysis: vi.fn(), getAiStatus: vi.fn(),
    compareImages: vi.fn(), reviewAnalysis: vi.fn(),
  }
})

const VESSELS = [...fx.vessels, { ...fx.vessels[0], vessel_id: 'second-vessel', vessel_name: 'Second Vessel' }]
let seq = 0
function analysis({ id, vessel = 'test-vessel', vesselName, location = 'MAIN DECK', laterLocation, earlier = '2026-08-02', later = '2026-08-30',
  laterWeek = 'Week 34', classification = 'STABLE', status = 'PENDING_REVIEW', reviewer, simulated = false, created } = {}) {
  seq += 1
  const n = String(seq).padStart(10, '0').replace(/[^0-9a-f]/g, '0')
  return {
    analysis_id: id ?? `CA-20261120-${n}`,
    created_at: created ?? `2026-11-${String(10 + seq).padStart(2, '0')}T10:00:00Z`,
    vessel_id: vessel,
    vessel_name: vesselName ?? (vessel === 'test-vessel' ? 'Test Vessel' : 'Second Vessel'),
    location: null,
    earlier: { record_id: `E${seq}`, image_id: `E${seq}`, inspection_date: earlier, week: 'Week 30', official_location: location },
    later: { record_id: `L${seq}`, image_id: `L${seq}`, inspection_date: later, week: laterWeek, official_location: laterLocation ?? location },
    interval_days: 28, provider: simulated ? 'mock' : 'groq', model: 'qwen/qwen3.8-27b', prompt_version: 'condition-compare-v1', simulated,
    ai_result: { explanation: `AI explanation ${seq}: visible coating condition compared.` },
    classification, model_classification: classification, confidence: 'MEDIUM', comparison_reliability: 'HIGH',
    human_review_required: true,
    review: { status, history: status === 'PENDING_REVIEW' ? [] : [{ status, reviewer: reviewer ?? 'Chief Officer', reviewer_role: 'admin', at: '2026-11-19T09:00:00Z' }] },
  }
}

function dataset() {
  seq = 0
  return [
    analysis({ id: 'CA-20261120-000000000a', classification: 'DETERIORATED', status: 'APPROVED', location: 'MAIN DECK' }),
    analysis({ id: 'CA-20261120-000000000b', classification: 'DETERIORATED', status: 'PENDING_REVIEW', location: 'MAIN DECK' }),
    analysis({ id: 'CA-20261120-000000000c', classification: 'AMELIORATED', status: 'REJECTED', location: 'GANGWAY ENTRANCE',
      earlier: '2026-10-18', later: '2026-10-25', laterWeek: 'Week 42' }),
    analysis({ id: 'CA-20261120-000000000d', classification: 'STABLE', status: 'NEEDS_REASSESSMENT', location: 'GANGWAY ENTRANCE',
      earlier: '2026-09-06', later: '2026-09-13', laterWeek: 'Week 37' }),
    analysis({ id: 'CA-20261120-000000000e', classification: 'AMELIORATED', status: 'APPROVED', vessel: 'second-vessel', location: 'BOW' }),
    analysis({ id: 'CA-20261120-000000000f', classification: 'DETERIORATED', status: 'APPROVED', simulated: true, location: 'MAIN DECK' }),
  ]
}

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname}{loc.search}</div>
}
const renderAt = (path) =>
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
      <Routes><Route path="*" element={<LocationProbe />} /></Routes>
    </MemoryRouter>,
  )
const card = async (name) => (await screen.findByRole('heading', { name, level: 3 })).closest('section')
const metric = (label) => within(screen.getByRole('group', { name: label })).getByTestId('metric-value').textContent
const timeline = () => within(screen.getByRole('list', { name: 'Analysis timeline' })).queryAllByRole('listitem', { name: /^Analysis CA-/ })

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(VESSELS)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: fx.inspections.total, items: fx.inspections.items })
  api.searchInspections.mockResolvedValue(fx.searchResult)
  api.getSession.mockResolvedValue({ authenticated: true, user: fx.viewerUser })   // SV-02: signed in to view
  const items = dataset()
  api.getAllAnalyses.mockResolvedValue({ total: items.length, items })
})

// ------------------------------------------------------------------------------------------- summaries
describe('trend summaries (stored data only)', () => {
  it('counts review statuses and treats only APPROVED changes as human-confirmed', () => {
    const items = dataset().filter((a) => !a.simulated)
    expect(summarise(items)).toEqual({
      total: 5, pending: 1, reviewed: 3, approved: 2, rejected: 1, reassessment: 1,
      confirmedChanges: 2, confirmedDeteriorated: 1, confirmedAmeliorated: 1,
    })
    const pendingDeterioration = items.find((a) => a.analysis_id.endsWith('b'))
    expect(isConfirmedChange(pendingDeterioration)).toBe(false)                // AI said deteriorated; nobody approved it
  })

  it('groups by Official Location and by later inspection week', () => {
    const items = dataset().filter((a) => !a.simulated)
    const rows = byLocation(items)
    expect(rows.map((r) => [r.location, r.total, r.pending, r.reviewed])).toEqual([
      ['GANGWAY ENTRANCE', 2, 0, 1], ['MAIN DECK', 2, 1, 1], ['BOW', 1, 0, 1],
    ])
    expect(rows.find((r) => r.location === 'MAIN DECK').human).toEqual({ DETERIORATED: 1, AMELIORATED: 0, STABLE: 0, NEEDS_REVIEW: 0 })
    expect(byLaterWeek(items).map((w) => w.week)).toEqual(['Week 34', 'Week 37', 'Week 42'])
    expect(analysisLocation({ earlier: { official_location: 'A' }, later: { official_location: 'B' } })).toBe('A → B')
    expect(analysisLocation({ earlier: {}, later: {} })).toBe('No Official Location')
  })

  it('combines filters and hides simulated results unless asked', () => {
    const items = dataset()
    expect(filterAnalyses(items).length).toBe(5)
    expect(filterAnalyses(items, { includeSimulated: true }).length).toBe(6)
    expect(filterAnalyses(items, { vessel: 'test-vessel', location: 'MAIN DECK', review: 'APPROVED' }).map((a) => a.analysis_id))
      .toEqual(['CA-20261120-000000000a'])
    expect(filterAnalyses(items, { from: '2026-10-01' }).map((a) => a.analysis_id)).toEqual(['CA-20261120-000000000c'])
    expect(filterAnalyses(items, { from: '2026-09-01', to: '2026-09-30' }).map((a) => a.analysis_id)).toEqual(['CA-20261120-000000000d'])
    expect(filterAnalyses(items, { q: 'gangway' }).length).toBe(2)
  })
})

// ------------------------------------------------------------------------------------------- page
describe('Condition Trends page', () => {
  it('is no longer in the menu but still renders from saved analyses only when opened directly', async () => {
    renderAt('/trends')
    expect(within(await screen.findByRole('navigation', { name: 'Main navigation' })).queryByRole('link', { name: /Condition Trends/ })).toBeNull()
    expect(await screen.findByRole('heading', { name: 'Condition Trends', level: 2 })).toBeInTheDocument()
    for (const name of ['Review Status', 'Condition Classification', 'Classifications Over Time', 'By Official Location', 'Analysis Timeline']) {
      expect(await card(name)).toBeInTheDocument()
    }
    expect(api.getAllAnalyses).toHaveBeenCalledTimes(1)
    expect(api.compareImages).not.toHaveBeenCalled()                                  // never runs an analysis
    expect(api.reviewAnalysis).not.toHaveBeenCalled()
  })

  it('shows summary metrics (simulated results excluded and counted)', async () => {
    renderAt('/trends')
    await screen.findByRole('group', { name: 'AI analyses' })
    expect(metric('AI analyses')).toBe('5')
    expect(metric('Pending human review')).toBe('1')
    expect(metric('Reviewed')).toBe('3')
    expect(metric('Needs reassessment')).toBe('1')
    expect(metric('Human-approved changes')).toBe('2')
    expect(screen.getByRole('status', { name: 'Result count' })).toHaveTextContent('5 of 5 analyses · 1 simulated (mock) result hidden')
    fireEvent.click(screen.getByLabelText('Include simulated (mock) results'))
    await waitFor(() => expect(metric('AI analyses')).toBe('6'))
    expect(timeline().some((li) => within(li).queryByText('Simulated'))).toBe(true)
  })

  it('lists analyses newest first with AI and human parts clearly separated', async () => {
    renderAt('/trends')
    await card('Analysis Timeline')
    const entries = timeline()
    expect(entries.map((li) => li.getAttribute('aria-label'))).toEqual([
      'Analysis CA-20261120-000000000e', 'Analysis CA-20261120-000000000d', 'Analysis CA-20261120-000000000c',
      'Analysis CA-20261120-000000000b', 'Analysis CA-20261120-000000000a',
    ])
    const pending = entries[3]
    expect(within(pending).getByTestId('ai-part')).toHaveTextContent('AI-generated')
    expect(within(pending).getByTestId('ai-part')).toHaveTextContent('Deteriorated')
    expect(within(pending).getByTestId('human-part')).toHaveTextContent('Pending review')
    expect(within(pending).getByTestId('human-part')).toHaveTextContent('not yet reviewed')
    expect(pending).toHaveTextContent('AI explanation')
    expect(pending).toHaveTextContent('MAIN DECK')
    expect(pending).toHaveTextContent('02 Aug 2026 → 30 Aug 2026')
    expect(within(pending).getByRole('img', { name: /^Earlier photograph E2/ })).toHaveAttribute('src', expect.stringContaining('/api/images/E2'))
    const approved = entries[4]
    expect(within(approved).getByTestId('human-part')).toHaveTextContent('Approved')
    expect(within(approved).getByTestId('human-part')).toHaveTextContent('by Chief Officer (admin)')
  })

  it('separates AI-generated classifications from human-approved ones', async () => {
    renderAt('/trends')
    const c = await card('Condition Classification')
    expect(within(c).getByRole('list', { name: 'AI-generated classifications' })).toHaveTextContent('Deteriorated2(40%)')
    const human = within(c).getByRole('list', { name: 'Human-approved classifications' })
    expect(within(human).getByText('Deteriorated').closest('li')).toHaveTextContent('1')
    expect(within(human).getByText('Ameliorated').closest('li')).toHaveTextContent('1')
    expect(within(human).getByText('Stable').closest('li')).toHaveTextContent('0')
    expect(c).toHaveTextContent('not severity levels')
  })

  it('shows the review-status distribution with the backend statuses', async () => {
    renderAt('/trends')
    const c = await card('Review Status')
    const list = within(c).getByRole('list', { name: 'Review status distribution' })
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Pending review1(20%)', 'Approved2(40%)', 'Rejected1(20%)', 'Needs reassessment1(20%)',
    ])
    expect(within(c).getByRole('img')).toHaveAccessibleName(/Pending review 1, Approved 2, Rejected 1, Needs reassessment 1/)
  })

  it('filters by Official Location (select or table) and shows per-location findings', async () => {
    renderAt('/trends')
    const c = await card('By Official Location')
    const row = within(c).getByRole('button', { name: 'MAIN DECK' }).closest('tr')
    expect(row).toHaveTextContent('Deteriorated 2')
    expect(row).toHaveTextContent('Deteriorated 1')                                    // human-approved column
    fireEvent.click(within(c).getByRole('button', { name: 'MAIN DECK' }))
    await waitFor(() => expect(timeline()).toHaveLength(2))
    expect(screen.getByLabelText('Official Location')).toHaveValue('MAIN DECK')
    fireEvent.change(screen.getByLabelText('Official Location'), { target: { value: 'GANGWAY ENTRANCE' } })
    await waitFor(() => expect(timeline().map((li) => li.textContent)).toEqual([expect.stringContaining('GANGWAY'), expect.stringContaining('GANGWAY')]))
  })

  it('filters by vessel', async () => {
    renderAt('/trends')
    await card('Analysis Timeline')
    fireEvent.change(screen.getByLabelText('Vessel'), { target: { value: 'second-vessel' } })
    await waitFor(() => expect(timeline()).toHaveLength(1))
    expect(timeline()[0]).toHaveTextContent('Second Vessel')
    expect(screen.getByRole('status', { name: 'Result count' })).toHaveTextContent('1 of 5 analyses · Second Vessel')
    expect([...screen.getByLabelText('Official Location').options].map((o) => o.value)).toEqual(['', 'BOW'])
  })

  it('filters by date range (either inspection date in range)', async () => {
    renderAt('/trends')
    await card('Analysis Timeline')
    fireEvent.change(screen.getByLabelText('From date'), { target: { value: '2026-09-01' } })
    fireEvent.change(screen.getByLabelText('To date'), { target: { value: '2026-09-30' } })
    await waitFor(() => expect(timeline()).toHaveLength(1))
    expect(timeline()[0]).toHaveAttribute('aria-label', 'Analysis CA-20261120-000000000d')
  })

  it('combines filters, searches, shows a filtered empty state and clears filters', async () => {
    renderAt('/trends')
    await card('Analysis Timeline')
    fireEvent.change(screen.getByLabelText('Vessel'), { target: { value: 'test-vessel' } })
    fireEvent.change(screen.getByLabelText('Review status'), { target: { value: 'APPROVED' } })
    await waitFor(() => expect(timeline()).toHaveLength(1))
    expect(metric('AI analyses')).toBe('1')                                             // summary follows the filters
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search analyses' }), { target: { value: 'gangway' } })
    expect(await screen.findByText('No analyses match these filters')).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: /Clear filters/ })[0])
    await waitFor(() => expect(timeline()).toHaveLength(5))
    expect(screen.getByLabelText('Review status')).toHaveValue('')
  })

  it('opens the assessment on the Reviews page', async () => {
    renderAt('/trends')
    await card('Analysis Timeline')
    api.getAnalysisHistory.mockResolvedValue({ total: 0, count: 0, offset: 0, limit: 25, items: [] })
    api.getAnalysis.mockReturnValue(new Promise(() => {}))
    fireEvent.click(screen.getByRole('link', { name: 'View details for CA-20261120-000000000a' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/reviews?id=CA-20261120-000000000a'))
    expect(await screen.findByRole('region', { name: 'Assessment details' })).toBeInTheDocument()
    expect(api.getAnalysis).toHaveBeenCalledWith('CA-20261120-000000000a', expect.anything())
  })

  it('keeps filters in the URL', async () => {
    renderAt('/trends?vessel=second-vessel&review=APPROVED')
    await card('Analysis Timeline')
    expect(timeline()).toHaveLength(1)
    expect(screen.getByLabelText('Vessel')).toHaveValue('second-vessel')
  })

  it('explains how to create the first analysis when none exist', async () => {
    api.getAllAnalyses.mockResolvedValue({ total: 0, items: [] })
    renderAt('/trends')
    expect(await screen.findByText('No AI condition analyses yet')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Go to Image Comparison/ })).toHaveAttribute('href', '/compare')
    expect(screen.queryByRole('group', { name: 'AI analyses' })).toBeNull()
  })

  it('shows a loading state', async () => {
    api.getAllAnalyses.mockReturnValue(new Promise(() => {}))
    renderAt('/trends')
    expect(await screen.findByText('Loading condition analyses…')).toBeInTheDocument()
  })

  it('shows an error state and Refresh/Retry reloads', async () => {
    api.getAllAnalyses.mockRejectedValueOnce(new api.ApiError('offline', 'The inspection service is currently unavailable.', 503))
    renderAt('/trends')
    expect(await screen.findByText('Condition analyses could not be loaded')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await card('Analysis Timeline')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(api.getAllAnalyses).toHaveBeenCalledTimes(3))
  })

  it('is viewable without signing in and offers no review actions (review stays in the existing workflow)', async () => {
    renderAt('/trends')
    await card('Analysis Timeline')
    expect(api.getSession).toHaveBeenCalled()
    for (const name of [/approve/i, /reject/i, /analyse images/i, /reassess/i]) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
  })

  it('paginates a long timeline', async () => {
    seq = 0
    const many = Array.from({ length: 20 }, () => analysis())
    api.getAllAnalyses.mockResolvedValue({ total: 20, items: many })
    renderAt('/trends')
    await card('Analysis Timeline')
    expect(timeline()).toHaveLength(15)
    fireEvent.click(screen.getByRole('button', { name: 'Show more (5 remaining)' }))
    expect(timeline()).toHaveLength(20)
  })
})

// ------------------------------------------------------------------------------------------- dashboard card
describe('Dashboard', () => {
  it('no longer duplicates Condition Trends (the condition summary replaces the card)', async () => {
    const items = dataset().filter((a) => a.vessel_id === 'test-vessel')
    api.getAllAnalyses.mockResolvedValue({ total: items.length, items })
    renderAt('/dashboard')
    expect(await screen.findByRole('list', { name: 'Condition results' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Condition Trends' })).toBeNull()
    expect(screen.queryByRole('link', { name: /Open Condition Trends/ })).toBeNull()
  })
})
