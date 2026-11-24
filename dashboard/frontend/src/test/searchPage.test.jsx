import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import * as api from '../services/api'
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

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: 4, items: fx.inspections.items })
  api.searchInspections.mockResolvedValue(fx.searchResult)
})

const gallery = () => screen.getByRole('list', { name: /inspection photographs/i })
const cardIds = () =>
  within(gallery())
    .getAllByRole('button', { name: /open photograph/i })
    .map((b) => b.getAttribute('aria-label').match(/photograph (\S+),/)[1])

describe('Inspection search page', () => {
  it('shows a loading state, then the summary for the searched location', async () => {
    let resolve
    api.searchInspections.mockReturnValue(new Promise((r) => (resolve = r)))
    renderAt('/search?location=main%20deck')
    expect(await screen.findByRole('status', { name: /searching inspection records/i })).toBeInTheDocument()
    await act(async () => resolve(fx.searchResult))
    const summary = await screen.findByRole('region', { name: /search summary/i })
    expect(within(summary).getByText('“main deck”')).toBeInTheDocument()
    expect(within(summary).getByText('4')).toBeInTheDocument() // appearances
    expect(within(summary).getAllByText('3')).toHaveLength(3) // unique photos, dates, weeks
    expect(api.searchInspections).toHaveBeenCalledWith({ location: 'main deck', vessel: 'test-vessel' }, expect.anything())
  })

  it('labels each result with the field that matched', async () => {
    renderAt('/search?location=main%20deck')
    await screen.findByRole('region', { name: /search summary/i })
    const g = gallery()
    expect(within(g).getAllByText('Official')).toHaveLength(2)
    expect(within(g).getAllByText('AI')).toHaveLength(1)
    expect(within(g).getAllByText('Official + AI')).toHaveLength(1)
    // Official Location is never replaced by the AI location
    expect(within(g).getByText('No Official Location')).toBeInTheDocument()
  })

  it('groups by date newest first, toggles to oldest, and keeps duplicate appearances', async () => {
    renderAt('/search?location=main%20deck')
    await screen.findByRole('region', { name: /search summary/i })
    expect(cardIds()).toEqual(['week_35-json-0000', 'W34-0001', 'W30-0001', 'W30-0002'])
    fireEvent.click(screen.getByRole('button', { name: /change sort order/i }))
    await waitFor(() => expect(cardIds()).toEqual(['W30-0001', 'W30-0002', 'W34-0001', 'week_35-json-0000']))
    expect(currentUrl).toContain('sort=oldest')
    expect(screen.getAllByText(/same photo in 2 records/i)).toHaveLength(2)
  })

  it('selecting a timeline date shows only that date and the derived-date indicator', async () => {
    renderAt('/search?location=main%20deck')
    await screen.findByRole('region', { name: /search summary/i })
    const timeline = screen.getByRole('list', { name: /inspection dates/i })
    fireEvent.click(within(timeline).getByRole('button', { name: /30 august 2026/i }))
    await waitFor(() => expect(cardIds()).toEqual(['W34-0001']))
    expect(currentUrl).toContain('date=2026-08-30')
    expect(screen.getAllByText('Date derived from worksheet').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: /previous \(older\) inspection date/i }))
    await waitFor(() => expect(cardIds()).toEqual(['W30-0001', 'W30-0002']))
  })

  it('filters by location source and week, and clears filters', async () => {
    renderAt('/search?location=main%20deck')
    await screen.findByRole('region', { name: /search summary/i })
    fireEvent.click(screen.getByRole('radio', { name: /^AI/ }))
    await waitFor(() => expect(cardIds()).toEqual(['W34-0001', 'W30-0001']))
    fireEvent.change(screen.getByLabelText(/inspection week/i), { target: { value: 'Week 30' } })
    await waitFor(() => expect(cardIds()).toEqual(['W30-0001']))
    fireEvent.click(screen.getByRole('button', { name: /clear filters/i }))
    await waitFor(() => expect(cardIds()).toHaveLength(4))
    expect(currentUrl).toBe('/search?location=main+deck')
  })

  it('opens the viewer with metadata and navigates with buttons and keys', async () => {
    renderAt('/search?location=main%20deck&sort=oldest')
    await screen.findByRole('region', { name: /search summary/i })
    fireEvent.click(within(gallery()).getByRole('button', { name: /open photograph W30-0001/i }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: 'W30-0001' })).toBeInTheDocument()
    expect(within(dialog).getByText('Main Deck')).toBeInTheDocument() // AI location
    expect(within(dialog).getByText(/^Not labelled/)).toBeInTheDocument()
    expect(within(dialog).getByText('High')).toBeInTheDocument() // confidence
    expect(within(dialog).getByText('No activity observed')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: /previous photograph/i })).toBeDisabled()
    expect(currentUrl).toContain('image=W30-0001')

    fireEvent.click(within(dialog).getByRole('button', { name: /next photograph/i }))
    expect(await within(dialog).findByRole('heading', { name: 'W30-0002' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(await within(dialog).findByRole('heading', { name: 'W34-0001' })).toBeInTheDocument()
    expect(within(dialog).getByText(/original report date: AMBIGUOUS\/PENDING/i)).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'ArrowLeft' })
    expect(await within(dialog).findByRole('heading', { name: 'W30-0002' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(currentUrl).not.toContain('image=')
    expect(currentUrl).toContain('sort=oldest') // search state preserved
  })

  it('opens directly from a URL with an image selected', async () => {
    renderAt('/search?location=main%20deck&image=W34-0001')
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: 'W34-0001' })).toBeInTheDocument()
  })

  it('handles missing and failing photographs safely', async () => {
    renderAt('/search?location=main%20deck')
    await screen.findByRole('region', { name: /search summary/i })
    expect(within(gallery()).getByRole('img', { name: 'Photograph unavailable' })).toBeInTheDocument()
    const img = within(gallery()).getByAltText('Inspection photograph W30-0001')
    expect(img).toHaveAttribute('src', '/api/images/W30-0001')
    expect(img).toHaveAttribute('loading', 'lazy')
    fireEvent.error(img)
    expect(await within(gallery()).findByRole('img', { name: /could not be loaded/i })).toBeInTheDocument()   // after the SV-02 session re-check
  })

  it('shows an empty state when nothing matches', async () => {
    api.searchInspections.mockResolvedValue({ ...fx.searchResult, count: 0, items: [] })
    renderAt('/search?location=nowhere')
    expect(await screen.findByText('No inspection records match “nowhere”')).toBeInTheDocument()
  })

  it('shows a friendly error with retry', async () => {
    api.searchInspections.mockRejectedValue(new api.ApiError('offline', 'Unable to reach the inspection service.'))
    renderAt('/search?location=main%20deck')
    expect(await screen.findByText('Search unavailable')).toBeInTheDocument()
    api.searchInspections.mockResolvedValue(fx.searchResult)
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(await screen.findByRole('region', { name: /search summary/i })).toBeInTheDocument()
  })

  it('without a location, offers locations to browse and searches on click', async () => {
    renderAt('/search')
    fireEvent.click(await screen.findByRole('button', { name: /Deck One/ }))
    await waitFor(() => expect(currentUrl).toBe('/search?location=Deck+One'))
    expect(api.searchInspections).toHaveBeenCalledWith({ location: 'Deck One', vessel: 'test-vessel' }, expect.anything())
  })

  it('progressively renders large result sets', async () => {
    const many = Array.from({ length: 30 }, (_, i) => ({
      ...fx.searchResult.items[1],
      record_id: `R-${i}`,
      image_id: `R-${i}`,
      image_url: `/api/images/R-${i}`,
    }))
    api.searchInspections.mockResolvedValue({ ...fx.searchResult, count: 30, items: many })
    renderAt('/search?location=main%20deck')
    await screen.findByRole('region', { name: /search summary/i })
    expect(cardIds()).toHaveLength(24)
    expect(screen.getByText('Showing 24 of 30 photographs')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /load more/i }))
    await waitFor(() => expect(cardIds()).toHaveLength(30))
  })
})
