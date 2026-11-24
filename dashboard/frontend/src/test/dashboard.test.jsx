/* Dashboard (manager simplification, 2026-11-23): pending reviews, condition results, latest inspection, overview.
   Mocked API responses only. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Camera } from 'lucide-react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import StatCard from '../components/StatCard'
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
    searchInspections: vi.fn(),
    getAllAnalyses: vi.fn(),
    getSession: vi.fn(),
  }
})

const renderApp = (path = '/dashboard') =>
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </MemoryRouter>,
  )

const saved = (id, classification, status = 'PENDING_REVIEW', extra = {}) => ({
  analysis_id: id, classification, simulated: false, review: { status, history: [] }, created_at: '2026-11-23T08:00:00Z',
  vessel_name: 'Test Vessel', location: 'Deck One', earlier: { record_id: 'A-1', image_id: 'A-1', inspection_date: '2026-08-02' },
  later: { record_id: 'B-1', image_id: 'B-1', inspection_date: '2026-08-30' }, ...extra,
})
// 3 awaiting review; results: 1 improved, 2 neutral, 1 deteriorated, 2 needs review (one model NEEDS_REVIEW, one rejected)
const ANALYSES = [
  saved('CA-20261123-0000000001', 'AMELIORATED'),
  saved('CA-20261123-0000000002', 'STABLE'),
  saved('CA-20261123-0000000003', 'STABLE', 'APPROVED'),
  saved('CA-20261123-0000000004', 'DETERIORATED', 'APPROVED'),
  saved('CA-20261123-0000000005', 'NEEDS_REVIEW'),
  saved('CA-20261123-0000000006', 'DETERIORATED', 'REJECTED'),
]

function mockOk() {
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: fx.inspections.total, items: fx.inspections.items })
  api.searchInspections.mockResolvedValue({ ...fx.searchResult, location: 'Deck One' })
  api.getAllAnalyses.mockResolvedValue({ total: ANALYSES.length, items: ANALYSES })
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getSession.mockResolvedValue({ authenticated: true, user: fx.viewerUser })   // SV-02: signed in to view
})

describe('StatCard', () => {
  it('shows an Unavailable state instead of a number when value is missing', () => {
    render(<StatCard label="Images" icon={Camera} value={null} loading={false} />)
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
  })
  it('shows a loading skeleton', () => {
    render(<StatCard label="Images" icon={Camera} value={5} loading />)
    expect(screen.getByRole('status', { name: /loading images/i })).toBeInTheDocument()
  })
})

describe('Dashboard', () => {
  it('shows the manager menu and an overview read from the API (vessels, weeks, photos)', async () => {
    mockOk()
    renderApp()
    const nav = await screen.findByRole('navigation', { name: /main navigation/i })   // after the sign-in gate
    expect(within(nav).getAllByRole('link').map((l) => l.textContent.trim())).toEqual(['Dashboard', 'Compare Photos', 'Reviews', 'Reports'])
    expect(await screen.findByRole('option', { name: 'Test Vessel' })).toBeInTheDocument()
    const overview = screen.getByRole('region', { name: 'Overview' })
    await waitFor(() => expect(within(overview).getByText('4')).toBeInTheDocument()) // inspection appearances
    // SV-06: appearances and unique photographs are separate cards (W30-0002 and W34-0001 are the same photo)
    const card = (label) => within(overview).getByText(label).closest('article')
    expect(card('Inspection appearances')).toHaveTextContent('4')
    expect(card('Inspection appearances')).toHaveTextContent('Every photo in every inspection week')
    expect(card('Unique photographs')).toHaveTextContent('3')
    expect(card('Unique photographs')).toHaveTextContent('Repeated photos counted once')
    expect(within(overview).getByText('2')).toBeInTheDocument() // inspection weeks
    expect(within(overview).getByText('1')).toBeInTheDocument() // vessels
    expect(within(overview).getByText('02 Aug 2026 – 30 Aug 2026')).toBeInTheDocument()
    expect(api.getAllInspections).toHaveBeenCalledWith({ vessel: 'test-vessel' }, expect.anything())
    expect(screen.getAllByRole('status', { name: /api connected/i }).length).toBeGreaterThan(0)
  })

  it('shows the latest inspection week and date', async () => {
    mockOk()
    renderApp()
    const latest = await screen.findByLabelText('Latest inspection details')
    expect(latest).toHaveTextContent('Week 34')
    expect(latest).toHaveTextContent('30 Aug 2026 · 2 photos')
  })

  it('makes pending reviews prominent with the real count; Review Assessments opens Reviews', async () => {
    mockOk()
    renderApp()
    expect(await screen.findByLabelText('Assessments awaiting review')).toHaveTextContent('3 Assessments Awaiting Review')
    expect(api.getAllAnalyses).toHaveBeenCalledWith({ vessel: 'test-vessel' }, expect.anything())
    fireEvent.click(screen.getByRole('link', { name: 'Review Assessments' }))
    expect(await screen.findByRole('heading', { name: 'Reviews', level: 2 })).toBeInTheDocument()
  })

  it('the Compare Photos button opens the comparison workflow', async () => {
    mockOk()
    renderApp()
    const pending = await screen.findByRole('region', { name: 'Pending reviews' })
    fireEvent.click(within(pending).getByRole('link', { name: 'Compare Photos' }))
    expect(await screen.findByRole('heading', { name: 'Compare Photos', level: 1 })).toBeInTheDocument()
  })

  it('summarises condition results with the existing names and mapping', async () => {
    mockOk()
    renderApp()
    const list = await screen.findByRole('list', { name: 'Condition results' })
    expect(within(list).getByLabelText('IMPROVED: 1')).toBeInTheDocument()
    expect(within(list).getByLabelText('NEUTRAL: 2')).toBeInTheDocument()
    expect(within(list).getByLabelText('DETERIORATED: 1')).toBeInTheDocument()
    expect(within(list).getByLabelText('NEEDS REVIEW: 2')).toBeInTheDocument()
    expect(screen.getByText('From 6 saved assessments')).toBeInTheDocument()
  })

  it('says so when nothing is awaiting review', async () => {
    mockOk()
    api.getAllAnalyses.mockResolvedValue({ total: 1, items: [saved('CA-20261123-0000000003', 'STABLE', 'APPROVED')] })
    renderApp()
    expect(await screen.findByLabelText('Assessments awaiting review')).toHaveTextContent('No assessments awaiting review')
  })

  it('no longer shows the search bar, location list, activity chart, trends card or technical details', async () => {
    mockOk()
    renderApp()
    await screen.findByLabelText('Assessments awaiting review')
    await screen.findByLabelText('Latest inspection details')
    expect(screen.queryByPlaceholderText('Search vessel inspection location...')).toBeNull()
    expect(screen.queryByRole('heading', { name: /Inspection Activity|Inspection Locations|Condition Trends/ })).toBeNull()
    // SV-06 shows "Inspection appearances" and "Unique photographs" by design; technical terms stay off the page.
    expect(document.body.textContent).not.toMatch(/SHA-256|sha256|hash/i)
  })

  it('shows friendly error states and the offline banner when the API is down', async () => {
    const offline = new api.ApiError('offline', 'Unable to reach the inspection service.')
    api.getHealth.mockRejectedValue(offline)
    api.getVessels.mockRejectedValue(offline)
    api.getLocations.mockRejectedValue(offline)
    api.getAllInspections.mockRejectedValue(offline)
    api.getAllAnalyses.mockRejectedValue(offline)
    renderApp()
    expect(await screen.findByText(/vessels unavailable/i)).toBeInTheDocument()
    expect(await screen.findAllByText('Unavailable')).toHaveLength(4)   // vessels, weeks, appearances, unique photographs
    expect(await screen.findByText(/inspection service is not reachable/i)).toBeInTheDocument()
  })

  it('shows an empty state when no vessels exist', async () => {
    api.getHealth.mockResolvedValue(fx.health)
    api.getVessels.mockResolvedValue([])
    renderApp()
    expect(await screen.findByText('No vessels available')).toBeInTheDocument()
    expect(api.getAllInspections).not.toHaveBeenCalled()
  })

  it('unknown pages show the not-found page, not a placeholder', async () => {
    mockOk()
    renderApp('/no-such-page')
    expect(await screen.findByText('This page does not exist.')).toBeInTheDocument()
    expect(screen.queryByText(/planned for a later phase/i)).toBeNull()
  })

  it('the home address opens Compare Photos', async () => {
    mockOk()
    renderApp('/')
    expect(await screen.findByRole('heading', { name: 'Compare Photos', level: 1 })).toBeInTheDocument()
  })
})

// =========================================================================== SV-06: appearances vs unique photographs
describe('Dashboard photo counts (SV-06)', () => {
  // The real data set's shape: 11 weeks x 98 photo appearances = 1,078, but only 609 distinct photographs because
  // later weeks repeat earlier weeks' photos (each record carries the backend's SHA-256 of its photo).
  const WEEKS = [30, 31, 34, 35, 36, 37, 38, 39, 40, 41, 42]
  const dataset = () => {
    const items = []
    let next = 0
    WEEKS.forEach((w, wi) => {
      for (let i = 1; i <= 98; i += 1) {
        // photos 1..609 are new the first time they appear; afterwards the same hash repeats in later weeks
        const n = wi * 98 + i <= 609 ? (next += 1) : ((wi * 98 + i) % 609) + 1
        items.push({ ...fx.inspections.items[0], record_id: `W${w}-${String(i).padStart(4, '0')}`, image_id: `W${w}-${String(i).padStart(4, '0')}`,
          week: `Week ${w}`, week_number: w, inspection_date: `2026-10-${String(wi + 1).padStart(2, '0')}`,
          extracted_photograph: { sha256: `sha-${n}` } })
      }
    })
    return items
  }
  const overviewCard = async (label) => {
    const overview = await screen.findByRole('region', { name: 'Overview' })
    await waitFor(() => expect(within(overview).getByText(label).closest('article')).not.toHaveTextContent(/^\s*$/))
    return within(overview).getByText(label).closest('article')
  }

  it('shows 1,078 inspection appearances and 609 unique photographs for the historical data set', async () => {
    mockOk()
    const items = dataset()
    expect(items).toHaveLength(1078)
    expect(new Set(items.map((i) => i.extracted_photograph.sha256)).size).toBe(609)
    api.getAllInspections.mockResolvedValue({ total: items.length, items })
    renderApp()
    await waitFor(async () => expect(await overviewCard('Inspection appearances')).toHaveTextContent('1,078'))
    expect(await overviewCard('Unique photographs')).toHaveTextContent('609')
    expect(await overviewCard('Inspection weeks')).toHaveTextContent('11')
  })

  it('a photograph repeated in several weeks is one unique photograph but several appearances', async () => {
    mockOk()
    const base = fx.inspections.items[0]
    const items = ['Week 30', 'Week 31', 'Week 34'].map((week, i) => ({ ...base, record_id: `R-${i}`, week, week_number: 30 + i,
      extracted_photograph: { sha256: 'same-photo' } }))
    api.getAllInspections.mockResolvedValue({ total: 3, items })
    renderApp()
    await waitFor(async () => expect(await overviewCard('Inspection appearances')).toHaveTextContent('3'))
    expect(await overviewCard('Unique photographs')).toHaveTextContent('1')
  })

  it('both counts come from the one inventory request (no image hashing in the browser)', async () => {
    mockOk()
    renderApp()
    await waitFor(async () => expect(await overviewCard('Unique photographs')).toHaveTextContent('3'))
    expect(api.getAllInspections).toHaveBeenCalledTimes(1)
    expect(api.getAllInspections).toHaveBeenCalledWith({ vessel: 'test-vessel' }, expect.anything())
  })

  it('says Unavailable instead of guessing when a record has no photograph hash', async () => {
    mockOk()
    const items = fx.inspections.items.map((r, i) => (i === 0 ? { ...r, extracted_photograph: null } : r))
    api.getAllInspections.mockResolvedValue({ total: items.length, items })
    renderApp()
    await waitFor(async () => expect(await overviewCard('Inspection appearances')).toHaveTextContent('4'))
    expect(await overviewCard('Unique photographs')).toHaveTextContent('Unavailable')
  })
})
