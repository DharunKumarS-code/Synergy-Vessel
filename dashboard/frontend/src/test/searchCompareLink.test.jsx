/* Post-audit Phase 4B: the Inspection Search -> Image Comparison link keeps the search's matching context.
   Mocked API responses and synthetic records only. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import * as api from '../services/api'
import { compareHref } from '../utils/compare'
import * as fx from './fixtures'

vi.mock('../services/api', async (orig) => {
  const actual = await orig()
  return {
    ...actual,
    // SV-02: the application is only shown to a signed-in user; these tests view as a read-only viewer.
    getSession: vi.fn(async () => ({ authenticated: true, user: { user_id: 'USR-000000000003', username: 'v.smith', display_name: 'V. Smith', role: 'viewer' } })),
    getHealth: vi.fn(), getVessels: vi.fn(), getLocations: vi.fn(), getAllInspections: vi.fn(), searchInspections: vi.fn(),
    getAiStatus: vi.fn(), getAnalysisHistory: vi.fn(), compareImages: vi.fn(), validateImagePair: vi.fn(), reviewAnalysis: vi.fn(),
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

const T = fx.searchResult.items.find((r) => r.record_id === 'W30-0002')
const rec = (id, date, official, { ai = null, labelled = true, on = ['official_location'] } = {}) => ({
  ...T, record_id: id, image_id: id, inspection_date: date, official_location: official, official_location_labelled: labelled,
  ai_suggested_location: ai, matched_on: on, extracted_photograph: { ...T.extracted_photograph, sha256: `sha-${id}` }, same_photo_appearances: 1,
})
const result = (q, items) => ({ location: q, match: 'contains', include_ai_location: true, count: items.length, items })
// "deck" matches several Official Locations
const BROAD = [rec('D-1', '2026-08-02', 'Main Deck'), rec('D-2', '2026-08-30', 'Main Deck'), rec('D-3', '2026-08-30', 'Poop Deck')]
// "Main Deck" names exactly one Official Location (spelling/case variants included)
const EXACT = [rec('X-1', '2026-08-02', 'Main Deck'), rec('X-2', '2026-08-30', 'MAIN  deck')]

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: 4, items: fx.inspections.items })
  api.getAiStatus.mockResolvedValue({ available: false, message: 'AI condition analysis is not enabled on this server.', provider: 'disabled', model: 'none' })
  api.validateImagePair.mockResolvedValue(fx.pairPassed)
  api.getAnalysisHistory.mockResolvedValue({ total: 0, count: 0, offset: 0, limit: 1, items: [] })
})

const link = () => screen.findByRole('link', { name: /compare inspection dates/i })
const params = () => new URLSearchParams(currentUrl.split('?')[1] ?? '')

describe('Search -> Compare keeps the matching context', () => {
  it('a broad/partial search opens the comparison in Contains mode with the same query', async () => {
    api.searchInspections.mockResolvedValue(result('deck', BROAD))
    renderAt('/search?location=deck')
    expect(await link()).toHaveAttribute('href', '/compare?location=deck&match=contains')
    fireEvent.click(await link())
    expect(await screen.findByRole('heading', { level: 1, name: 'Compare Photos' })).toBeInTheDocument()
    expect(params().get('match')).toBe('contains')
    expect(params().get('location')).toBe('deck')
    await waitFor(() => expect(api.searchInspections).toHaveBeenLastCalledWith(
      { location: 'deck', vessel: 'test-vessel', match: 'contains', includeAi: true }, expect.anything()))
    expect(within(screen.getByLabelText('Location')).getByRole('option', { name: 'Contains “deck”' }).selected).toBe(true)
    expect(await screen.findByRole('note', { name: 'Multiple Official Locations' })).toHaveTextContent('2 different Official Locations')
  })

  it('an exact Official Location search opens the comparison in Exact mode', async () => {
    api.searchInspections.mockResolvedValue(result('Main Deck', EXACT))
    renderAt('/search?location=Main%20Deck')
    expect(await link()).toHaveAttribute('href', '/compare?location=Main+Deck')
    fireEvent.click(await link())
    await screen.findByRole('heading', { level: 1, name: 'Compare Photos' })
    expect(params().get('match')).toBeNull() // Exact is the default
    await waitFor(() => expect(api.searchInspections).toHaveBeenLastCalledWith(
      { location: 'Main Deck', vessel: 'test-vessel', match: 'exact', includeAi: false }, expect.anything()))
    await waitFor(() => expect(screen.getByLabelText('Location')).toHaveValue('Main Deck'))
  })

  it('the Official/AI source filter is carried into Contains mode; week/date/sort stay on the search page', async () => {
    api.searchInspections.mockResolvedValue(result('deck', BROAD))
    renderAt('/search?location=deck&source=official&week=Week%2030&sort=oldest')
    expect(await link()).toHaveAttribute('href', '/compare?location=deck&match=contains&source=official')
    fireEvent.click(await link())
    await screen.findByRole('heading', { level: 1, name: 'Compare Photos' })
    expect(params().get('source')).toBe('official')                                          // still applied to the comparison
    expect(['week', 'sort', 'date'].map((k) => params().get(k))).toEqual([null, null, null])
  })

  it('results matched only through an AI location, or without an Official Location, keep Contains', async () => {
    api.searchInspections.mockResolvedValue(result('Main Deck', [
      ...EXACT, rec('A-1', '2026-08-30', 'Not Labelled', { ai: 'Main Deck', labelled: false, on: ['ai_suggested_location'] })]))
    renderAt('/search?location=Main%20Deck')
    expect(await link()).toHaveAttribute('href', '/compare?location=Main+Deck&match=contains')
  })

  it('the search page itself is unchanged (summary text and its own broad search call)', async () => {
    api.searchInspections.mockResolvedValue(result('deck', BROAD))
    renderAt('/search?location=deck')
    const summary = await screen.findByRole('region', { name: 'Search summary' })
    expect(within(summary).getByText('Official and AI locations · partial match')).toBeInTheDocument()
    expect(api.searchInspections).toHaveBeenCalledWith({ location: 'deck', vessel: 'test-vessel' }, expect.anything())
  })
})

describe('compareHref', () => {
  it('chooses the mode from the results, ignoring case and spacing', () => {
    expect(compareHref('main deck', EXACT)).toBe('/compare?location=main+deck')
    expect(compareHref('deck', BROAD)).toBe('/compare?location=deck&match=contains')
    expect(compareHref('Main Deck', EXACT, { source: 'official' })).toBe('/compare?location=Main+Deck') // exact has no source filter
    expect(compareHref('deck', BROAD, { source: 'all' })).toBe('/compare?location=deck&match=contains')
  })

  it('falls back to Contains when there is nothing to judge by, and encodes the query safely', () => {
    expect(compareHref('deck', [])).toBe('/compare?location=deck&match=contains')
    expect(compareHref('deck', undefined)).toBe('/compare?location=deck&match=contains')
    const href = compareHref('No. 2 hold & hatch', BROAD)
    expect(new URLSearchParams(href.split('?')[1]).get('location')).toBe('No. 2 hold & hatch')
  })
})
