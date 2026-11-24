/* Image Comparison improvements (post-audit Phase 2): exact vs contains location matching, picker visibility beyond
   12 photographs, and selection clarity. (The zoom/pan workspace was removed from the page on 2026-11-21.) All API calls are mocked. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { compareLink } from '../pages/ReviewsPage'
import * as api from '../services/api'
import { differentLocations, locationKey, officialLocations } from '../utils/compare'
import * as fx from './fixtures'

vi.mock('../services/api', async (orig) => {
  const actual = await orig()
  return {
    ...actual,
    // SV-02: the application is only shown to a signed-in user; these tests view as a read-only viewer.
    getSession: vi.fn(async () => ({ authenticated: true, user: { user_id: 'USR-000000000003', username: 'v.smith', display_name: 'V. Smith', role: 'viewer' } })),
    getHealth: vi.fn(),
    getVessels: vi.fn(),
    getLocations: vi.fn(),
    getAllInspections: vi.fn(),
    searchInspections: vi.fn(),
    getAiStatus: vi.fn(),
    getAnalysisHistory: vi.fn(),
    compareImages: vi.fn(),
    validateImagePair: vi.fn(),
    reviewAnalysis: vi.fn(),
  }
})

let currentUrl = ''
function UrlProbe() {
  const loc = useLocation()
  currentUrl = loc.pathname + loc.search
  return null
}
const renderAt = (path) =>
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
      <UrlProbe />
    </MemoryRouter>,
  )

// One labelled template record; photo(id, date, official) clones it with the given identity.
const T = fx.searchResult.items.find((r) => r.record_id === 'W30-0002')
const photo = (id, date, official = 'Main Deck', week = 'Week 30') => ({
  ...T, record_id: id, image_id: id, inspection_date: date, week, official_location: official,
  official_location_labelled: true, ai_suggested_location: null, matched_on: ['official_location'],
  extracted_photograph: { ...T.extracted_photograph, sha256: `sha-${id}` }, same_photo_appearances: 1,
})
const result = (items, match = 'exact') => ({ location: 'x', match, include_ai_location: match !== 'exact', count: items.length, items })

// Exact-mode data: two dates of one Official Location; 20 photographs on the later date.
const EARLY = Array.from({ length: 3 }, (_, i) => photo(`E-${i + 1}`, '2026-08-02'))
const LATE = Array.from({ length: 20 }, (_, i) => photo(`L-${String(i + 1).padStart(2, '0')}`, '2026-08-30', 'Main Deck', 'week 34'))
// Contains-mode data: three Official Locations mixed.
const MIXED = [
  photo('M-1', '2026-08-02', 'Main Deck'), photo('M-2', '2026-08-30', 'Main Deck', 'week 34'),
  photo('M-3', '2026-08-02', 'Main Deck Fwd'), photo('M-4', '2026-08-30', 'Main Deck Fwd', 'week 34'),
  photo('M-5', '2026-08-30', 'Top View Main Deck', 'week 34'),
]

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: 4, items: fx.inspections.items })
  api.searchInspections.mockResolvedValue(result([...EARLY, ...LATE]))
  api.getAiStatus.mockResolvedValue({ available: false, message: 'AI condition analysis is not enabled on this server.', provider: 'disabled', model: 'none' })
  api.validateImagePair.mockResolvedValue(fx.pairPassed)
  api.getAnalysisHistory.mockResolvedValue({ total: 0, count: 0, offset: 0, limit: 1, items: [] })
})

const picker = (name) => screen.getByRole('region', { name: `${name} photographs` })
const choices = (name) => within(picker(name)).getAllByRole('button', { name: /^Select photograph/ })
const shownImgs = () => [screen.getByAltText(/^Earlier image /), screen.getByAltText(/^Later image /)]
const params = () => new URLSearchParams(currentUrl.split('?')[1] ?? '')
// The photo grid is folded away in the simplified view.
const openPickers = async () => fireEvent.click(await screen.findByRole('button', { name: 'Choose from all photos' }))

// ============================================================================ location matching
describe('exact vs contains location matching', () => {
  it('defaults to exact Official Location matching and asks the API for exactly that', async () => {
    renderAt('/compare?location=Main%20Deck')
    await screen.findByLabelText('Earlier inspection')
    expect(api.searchInspections).toHaveBeenCalledWith({ location: 'Main Deck', vessel: 'test-vessel', match: 'exact', includeAi: false }, expect.anything())
    expect(screen.queryByRole('radio', { name: /^AI/ })).not.toBeInTheDocument()            // no matching/source controls on the page
    expect(screen.queryByRole('note', { name: 'Multiple Official Locations' })).not.toBeInTheDocument()
    expect(params().get('match')).toBeNull()                                                 // default kept out of the URL
  })

  it('date counts and photo grids use the same filtered records', async () => {
    renderAt('/compare?location=Main%20Deck')
    const earlier = await screen.findByLabelText('Earlier inspection')
    await openPickers()
    expect(within(earlier).getByRole('option', { name: /3 photos/ })).toHaveValue('2026-08-02')
    expect(within(screen.getByLabelText('Later inspection')).getByRole('option', { name: /20 photos/ })).toHaveValue('2026-08-30')
    expect(choices('Earlier inspection')).toHaveLength(3)
    expect(within(picker('Later inspection')).getByRole('button', { name: /show all 20 photographs/i })).toBeInTheDocument()
  })

  it('contains mode keeps the broad search and says plainly that locations are mixed', async () => {
    api.searchInspections.mockResolvedValue(result(MIXED, 'contains'))
    renderAt('/compare?location=main%20deck&match=contains')
    await screen.findByLabelText('Earlier inspection')
    await openPickers()
    expect(api.searchInspections).toHaveBeenCalledWith({ location: 'main deck', vessel: 'test-vessel', match: 'contains', includeAi: true }, expect.anything())
    const note = screen.getByRole('note', { name: 'Multiple Official Locations' })
    expect(note).toHaveTextContent('3 different Official Locations')
    expect(within(note).getAllByRole('button').map((b) => b.getAttribute('aria-label')))
      .toEqual(['Compare only Main Deck', 'Compare only Main Deck Fwd', 'Compare only Top View Main Deck'])
    // every photograph shows its own Official Location in the picker
    expect(within(picker('Later inspection')).getAllByTitle('Official Location').map((p) => p.textContent))
      .toEqual(['Main Deck', 'Main Deck Fwd', 'Top View Main Deck'])
  })

  it('picking one location from the mixed results switches to an exact comparison of it', async () => {
    api.searchInspections.mockResolvedValue(result(MIXED, 'contains'))
    renderAt('/compare?location=main%20deck&match=contains')
    fireEvent.click(await screen.findByRole('button', { name: 'Compare only Main Deck Fwd' }))
    await waitFor(() => expect(params().get('location')).toBe('Main Deck Fwd'))
    expect(params().get('match')).toBeNull()
    await waitFor(() => expect(api.searchInspections).toHaveBeenLastCalledWith(
      { location: 'Main Deck Fwd', vessel: 'test-vessel', match: 'exact', includeAi: false }, expect.anything()))
  })

  it('choosing another location clears the selection and returns to exact matching', async () => {
    api.searchInspections.mockResolvedValue(result(MIXED, 'contains'))
    renderAt('/compare?location=main%20deck&match=contains&earlier=2026-08-02&later=2026-08-30&a=M-1&b=M-2')
    const select = await screen.findByLabelText('Location')
    await within(select).findByRole('option', { name: 'Deck One' })
    fireEvent.change(select, { target: { value: 'Deck One' } })
    await waitFor(() => expect(params().get('location')).toBe('Deck One'))
    expect(['match', 'a', 'b', 'earlier', 'later'].map((k) => params().get(k))).toEqual([null, null, null, null, null])
  })

  it('exact mode with no match offers the Official Locations containing the text, or a broader search', async () => {
    api.searchInspections.mockResolvedValue(result([]))
    renderAt('/compare?location=deck')
    expect(await screen.findByText('No inspection records match “deck”')).toBeInTheDocument()
    expect(screen.getByText('Exact location matching looks for this Official Location name only.')).toBeInTheDocument()
    const chip = await screen.findByRole('button', { name: 'Compare only Deck One' })
    expect(screen.queryByRole('button', { name: 'Compare only Hold Two' })).not.toBeInTheDocument()  // does not contain "deck"
    fireEvent.click(screen.getByRole('button', { name: /search all locations containing “deck” instead/i }))
    await waitFor(() => expect(params().get('match')).toBe('contains'))
    expect(params().get('location')).toBe('deck')
    expect(chip).toBeDefined()
  })

  it('contains mode with no results keeps the original guidance', async () => {
    api.searchInspections.mockResolvedValue(result([], 'contains'))
    renderAt('/compare?location=nowhere&match=contains')
    expect(await screen.findByText('Try a shorter part of the location name.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /search all locations containing/i })).not.toBeInTheDocument()
  })
})

// ============================================================================ picker visibility
describe('photo picker visibility', () => {
  it('shows the first 12 photographs and can show the rest', async () => {
    renderAt('/compare?location=Main%20Deck')
    await screen.findByLabelText('Earlier inspection')
    await openPickers()
    expect(choices('Later inspection')).toHaveLength(12)
    fireEvent.click(within(picker('Later inspection')).getByRole('button', { name: /show all 20 photographs/i }))
    expect(choices('Later inspection')).toHaveLength(20)
  })

  it('a selection beyond photograph 12 (e.g. from the URL) is always shown and marked', async () => {
    renderAt('/compare?location=Main%20Deck&earlier=2026-08-02&later=2026-08-30&a=E-1&b=L-15')
    await screen.findByAltText(/^Later image /)
    await openPickers()
    expect(choices('Later inspection')).toHaveLength(20)
    const sel = within(picker('Later inspection')).getByRole('button', { name: /select photograph L-15 /i })
    expect(sel).toHaveAttribute('aria-pressed', 'true')
    expect(picker('Later inspection')).toHaveTextContent('Selected L-15 · 15 of 20')
    expect(within(picker('Later inspection')).queryByRole('button', { name: /show fewer/i })).not.toBeInTheDocument()
    expect(shownImgs()[1]).toHaveAttribute('src', '/api/images/L-15')                    // grid and shown image agree
  })

  it('stepping with the arrows past photograph 12 expands the grid and keeps selection and image in sync', async () => {
    renderAt('/compare?location=Main%20Deck&earlier=2026-08-02&later=2026-08-30&a=E-1&b=L-12')
    await screen.findByAltText(/^Later image /)
    await openPickers()
    expect(choices('Later inspection')).toHaveLength(12)
    fireEvent.click(screen.getByRole('button', { name: 'Next later photograph' }))
    await waitFor(() => expect(params().get('b')).toBe('L-13'))
    expect(choices('Later inspection')).toHaveLength(20)
    expect(within(picker('Later inspection')).getByRole('button', { name: /select photograph L-13 /i })).toHaveAttribute('aria-pressed', 'true')
    expect(picker('Later inspection')).toHaveTextContent('Selected L-13 · 13 of 20')
    expect(shownImgs()[1]).toHaveAttribute('src', '/api/images/L-13')
  })

  it('scrolls only the grid to the selected photograph', async () => {
    renderAt('/compare?location=Main%20Deck&earlier=2026-08-02&later=2026-08-30&a=E-1&b=L-12')
    await screen.findByAltText(/^Later image /)
    await openPickers()
    const list = within(picker('Later inspection')).getByRole('list')
    // simulate layout: the grid shows 0-400px, the newly selected photograph sits at 900-1000px
    list.getBoundingClientRect = () => ({ top: 0, bottom: 400 })
    const real = Element.prototype.getBoundingClientRect
    Element.prototype.getBoundingClientRect = function () { return this.tagName === 'LI' ? { top: 900, bottom: 1000 } : real.call(this) }
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Next later photograph' }))
      await waitFor(() => expect(list.scrollTop).toBe(600))
    } finally {
      Element.prototype.getBoundingClientRect = real
    }
  })
})

// ============================================================================ selection clarity
describe('selection clarity', () => {
  it('shows each photograph’s Official Location under it', async () => {
    renderAt('/compare?location=Main%20Deck&earlier=2026-08-02&later=2026-08-30&a=E-1&b=L-01')
    await screen.findByAltText(/^Later image /)
    expect(screen.getByLabelText('Earlier image location')).toHaveTextContent('Main Deck')
    expect(screen.getByLabelText('Later image location')).toHaveTextContent('Main Deck')
    expect(screen.queryByRole('note', { name: 'Different locations' })).not.toBeInTheDocument()
  })

  it('warns when the two selected photographs have different Official Locations', async () => {
    api.searchInspections.mockResolvedValue(result(MIXED, 'contains'))
    renderAt('/compare?location=main%20deck&match=contains&earlier=2026-08-02&later=2026-08-30&a=M-1&b=M-4')
    await screen.findByAltText(/^Later image /)
    await openPickers()
    expect(screen.getByRole('note', { name: 'Different locations' })).toHaveTextContent('different locations')
    expect(screen.getByLabelText('Later image location')).toHaveTextContent('Main Deck Fwd')
    fireEvent.click(within(picker('Later inspection')).getByRole('button', { name: /select photograph M-2 /i }))
    await waitFor(() => expect(screen.queryByRole('note', { name: 'Different locations' })).not.toBeInTheDocument())
  })
})

// ============================================================================ helpers + deep links
describe('helpers and links', () => {
  it('groups Official Locations and compares names ignoring case and spacing', () => {
    expect(officialLocations(MIXED).map((l) => [l.location, l.count])).toEqual([['Main Deck', 2], ['Main Deck Fwd', 2], ['Top View Main Deck', 1]])
    expect(officialLocations([{ ...MIXED[0], official_location_labelled: false }])).toEqual([{ location: null, count: 1 }])
    expect(locationKey('  MAIN   deck ')).toBe('main deck')
    expect(differentLocations(MIXED[0], { ...MIXED[1], official_location: 'main  DECK' })).toBe(false)
    expect(differentLocations(MIXED[0], MIXED[2])).toBe(true)
  })

  it('analysis links use exact matching only when both saved photographs have that Official Location', () => {
    const side = (id, loc) => ({ record_id: id, inspection_date: '2026-08-02', official_location: loc })
    expect(compareLink({ location: 'Main Deck', earlier: side('A', 'Main Deck'), later: side('B', 'MAIN deck') })).not.toContain('match=')
    expect(compareLink({ location: 'Main Deck', earlier: side('A', null), later: side('B', 'Main Deck') })).toContain('match=contains')
    expect(compareLink({ location: 'deck', earlier: side('A', 'Main Deck'), later: side('B', 'Main Deck') })).toContain('match=contains')
  })
})
