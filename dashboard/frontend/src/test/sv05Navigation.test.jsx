/* SV-05 -- Reviews result filter (Phase 1) and clickable Dashboard condition cards (Phase 2).
   Mocked API responses only. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { RESULT_GROUPS, resultGroup } from '../components/compare/InspectionOutcome'
import { conditionSummary } from '../pages/DashboardPage'
import * as api from '../services/api'
import * as fx from './fixtures'

vi.mock('../services/api', async (orig) => {
  const actual = await orig()
  return {
    ...actual,
    getHealth: vi.fn(),
    getVessels: vi.fn(),
    getLocations: vi.fn(),
    getAllInspections: vi.fn(),
    getAllAnalyses: vi.fn(),
    getAnalysis: vi.fn(),
    reviewAnalysis: vi.fn(),
    getSession: vi.fn(),
  }
})

let currentUrl = ''
let goBack = () => {}
let goForward = () => {}
function Probe() {
  const l = useLocation()
  const nav = useNavigate()
  currentUrl = l.pathname + l.search
  goBack = () => nav(-1)
  goForward = () => nav(1)
  return null
}
const renderAt = (path) => render(
  <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
    <App />
    <Probe />
  </MemoryRouter>,
)

const saved = (id, classification, status = 'PENDING_REVIEW', extra = {}) => ({
  analysis_id: id, classification, simulated: false, review: { status, history: [] }, created_at: `2026-11-2${id.at(-1)}T08:00:00Z`,
  vessel_name: 'Test Vessel', location: `Deck ${id.at(-1)}`, model_classification: classification,
  earlier: { record_id: 'A-1', image_id: 'A-1', inspection_date: '2026-08-02' },
  later: { record_id: 'B-1', image_id: 'B-1', inspection_date: '2026-08-30' }, ai_result: { classification }, ...extra,
})
// improved 1 · neutral 2 · deteriorated 1 · needs review 5 (uncertain, reassessment, rejected, test result, pending uncertain)
const ITEMS = [
  saved('CA-20261123-0000000001', 'AMELIORATED', 'APPROVED'),
  saved('CA-20261123-0000000002', 'STABLE'),
  saved('CA-20261123-0000000003', 'STABLE', 'APPROVED'),
  saved('CA-20261123-0000000004', 'DETERIORATED'),
  saved('CA-20261123-0000000005', 'NEEDS_REVIEW', 'APPROVED'),
  saved('CA-20261123-0000000006', 'DETERIORATED', 'REJECTED'),
  saved('CA-20261123-0000000007', 'STABLE', 'NEEDS_REASSESSMENT'),
  saved('CA-20261123-0000000008', 'AMELIORATED', 'PENDING_REVIEW', { simulated: true }),
  saved('CA-20261123-0000000009', 'NEEDS_REVIEW'),
]
const EXPECTED = { improved: 1, neutral: 2, deteriorated: 1, needs_review: 5 }

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: fx.inspections.total, items: fx.inspections.items })
  api.getAllAnalyses.mockResolvedValue({ total: ITEMS.length, items: ITEMS })
  api.getAnalysis.mockImplementation((id) => Promise.resolve(ITEMS.find((a) => a.analysis_id === id)))
  api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
  Element.prototype.scrollIntoView = vi.fn(() => Promise.resolve())
})

const rows = async () => within(await screen.findByRole('list', { name: 'Assessments' })).getAllByRole('listitem')
const filters = () => screen.findByRole('group', { name: 'Filter by result' })

// =========================================================================== Phase 1: Reviews result filter
describe('Reviews result filter', () => {
  it('one shared grouping: the Dashboard counts and the filter use the same mapping', () => {
    const counts = conditionSummary(ITEMS)
    for (const g of RESULT_GROUPS) {
      expect(ITEMS.filter((a) => resultGroup(a) === g.key)).toHaveLength(EXPECTED[g.key])
      expect(counts[g.result]).toBe(EXPECTED[g.key])
    }
  })

  it.each(Object.entries(EXPECTED))('?result=%s shows exactly the assessments of that group', async (key, n) => {
    renderAt(`/reviews?show=all&result=${key}`)
    expect(await rows()).toHaveLength(n)
    const group = RESULT_GROUPS.find((g) => g.key === key)
    expect(within(await filters()).getByRole('button', { name: new RegExp(group.label) })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('tab', { name: `All assessments (${n})` })).toHaveAttribute('aria-selected', 'true')
  })

  it('"Needs review" includes uncertain, reassessment, rejected and test results, awaiting review first', async () => {
    renderAt('/reviews?show=all&result=needs_review')
    const r = await rows()
    expect(r.map((x) => x.textContent.match(/Deck \d/)[0])).toEqual(['Deck 9', 'Deck 8', 'Deck 7', 'Deck 5', 'Deck 6'])
    expect(r[0]).toHaveTextContent('Awaiting review')
    expect(r[1]).toHaveTextContent('Awaiting review')
    expect(r[2]).toHaveTextContent('Reassessment requested')
  })

  it('the filter also applies to the Awaiting review tab', async () => {
    renderAt('/reviews?result=neutral')
    const r = await rows()
    expect(r).toHaveLength(1)
    expect(r[0]).toHaveTextContent('Deck 2')
    expect(screen.getByRole('tab', { name: 'Awaiting review (1)' })).toHaveAttribute('aria-selected', 'true')
  })

  it('choosing filters and tabs adds history entries: browser back and forward work', async () => {
    renderAt('/reviews?show=all')
    expect(await rows()).toHaveLength(9)
    fireEvent.click(within(await filters()).getByRole('button', { name: /Improved/ }))
    await waitFor(() => expect(currentUrl).toBe('/reviews?show=all&result=improved'))
    expect(await rows()).toHaveLength(1)
    fireEvent.click(within(await filters()).getByRole('button', { name: /Deteriorated/ }))
    await waitFor(() => expect(currentUrl).toBe('/reviews?show=all&result=deteriorated'))
    goBack()
    await waitFor(() => expect(currentUrl).toBe('/reviews?show=all&result=improved'))
    expect(await rows()).toHaveLength(1)
    goBack()
    await waitFor(() => expect(currentUrl).toBe('/reviews?show=all'))
    expect(await rows()).toHaveLength(9)
    goForward()
    await waitFor(() => expect(currentUrl).toBe('/reviews?show=all&result=improved'))
  })

  it('shows an empty state for a filter with no matches, with a way back to all results', async () => {
    api.getAllAnalyses.mockResolvedValue({ total: 1, items: [ITEMS[1]] })
    renderAt('/reviews?show=all&result=deteriorated')
    const empty = await screen.findByRole('status', { name: 'No matching assessments' })
    expect(empty).toHaveTextContent('No assessments with the result “Deteriorated” for this vessel.')
    fireEvent.click(within(empty).getByRole('button', { name: 'Show all results' }))
    await waitFor(() => expect(currentUrl).toBe('/reviews?show=all'))
    expect(await rows()).toHaveLength(1)
  })

  it('an unknown result value is ignored (all results shown)', async () => {
    renderAt('/reviews?show=all&result=broken')
    expect(await rows()).toHaveLength(9)
    expect(within(await filters()).getByRole('button', { name: 'All results' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('review actions are unchanged inside a filtered list (reviewer on an awaiting assessment)', async () => {
    renderAt('/reviews?show=all&result=neutral')
    fireEvent.click(within(await screen.findByRole('list', { name: 'Assessments' })).getByRole('button', { name: /Open assessment: Deck 2/ }))
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    expect(await within(detail).findByRole('button', { name: /Approve/ })).toBeInTheDocument()
    expect(currentUrl).toContain('result=neutral')
  })
})

// =========================================================================== Phase 2: clickable Dashboard cards
describe('Dashboard condition cards', () => {
  const card = async (label) => within(await screen.findByRole('list', { name: 'Condition results' }))
    .getByRole('link', { name: new RegExp(`^Show ${label} assessments`) })

  it.each([['improved', 'improved'], ['neutral', 'neutral'], ['deteriorated', 'deteriorated'], ['needs review', 'needs_review']])(
    'the %s card opens Reviews filtered to it, showing exactly its count', async (label, key) => {
      renderAt('/dashboard')
      const link = await card(label)
      expect(link).toHaveAttribute('href', `/reviews?show=all&result=${key}`)
      expect(link).toHaveAccessibleName(`Show ${label} assessments (${EXPECTED[key]})`)
      fireEvent.click(link)
      await waitFor(() => expect(currentUrl).toBe(`/reviews?show=all&result=${key}`))
      expect(await rows()).toHaveLength(EXPECTED[key])
      expect(screen.getByRole('tab', { name: `All assessments (${EXPECTED[key]})` })).toBeInTheDocument()
    })

  it('cards look clickable (pointer cursor, hover feedback, focus ring) and keep their counts', async () => {
    renderAt('/dashboard')
    const link = await card('neutral')
    expect(link.className).toMatch(/cursor-pointer/)
    expect(link.className).toMatch(/hover:/)
    expect(link.className).toMatch(/focus-ring/)
    expect(within(link).getByText('2')).toBeInTheDocument()
  })

  it('browser back returns to the Dashboard; forward reopens the filtered list; the vessel stays selected', async () => {
    renderAt('/dashboard')
    fireEvent.click(await card('deteriorated'))
    await waitFor(() => expect(currentUrl).toBe('/reviews?show=all&result=deteriorated'))
    goBack()
    await waitFor(() => expect(currentUrl).toBe('/dashboard'))
    expect(await card('deteriorated')).toBeInTheDocument()
    goForward()
    await waitFor(() => expect(currentUrl).toBe('/reviews?show=all&result=deteriorated'))
    expect(await rows()).toHaveLength(1)
    // Both pages read the same selected vessel.
    expect(api.getAllAnalyses.mock.calls.every(([q]) => q.vessel === 'test-vessel')).toBe(true)
  })

  it('a card with no assessments opens the empty state', async () => {
    api.getAllAnalyses.mockResolvedValue({ total: 1, items: [ITEMS[1]] })
    renderAt('/dashboard')
    const link = await card('improved')
    expect(link).toHaveAccessibleName('Show improved assessments (0)')
    fireEvent.click(link)
    expect(await screen.findByRole('status', { name: 'No matching assessments' })).toBeInTheDocument()
  })

  it('a viewer can follow the cards and sees the list, but no review buttons (SV-02)', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.viewerUser })
    renderAt('/dashboard')
    fireEvent.click(await card('neutral'))
    fireEvent.click(within(await screen.findByRole('list', { name: 'Assessments' })).getByRole('button', { name: /Open assessment: Deck 2/ }))
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    await within(detail).findByLabelText('Review status')
    expect(within(detail).queryByRole('button', { name: /Approve|Reject|Reassessment/ })).toBeNull()
  })
})
