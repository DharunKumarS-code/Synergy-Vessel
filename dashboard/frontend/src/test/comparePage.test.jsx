import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: 4, items: fx.inspections.items })
  api.searchInspections.mockResolvedValue(fx.searchResult)
  api.getAiStatus.mockResolvedValue({ available: false, message: 'AI condition analysis is not enabled on this server.', provider: 'disabled', model: 'none' })
  api.validateImagePair.mockResolvedValue(fx.pairPassed)
  api.getAnalysisHistory.mockResolvedValue({ total: 0, count: 0, offset: 0, limit: 1, items: [] })
})

const BASE = '/compare?location=main%20deck'
const earlierSelect = () => screen.getByLabelText('Earlier inspection')
const laterSelect = () => screen.getByLabelText('Later inspection')
const picker = (name) => screen.getByRole('region', { name: `${name} photographs` })
const ready = () => screen.findByLabelText('Earlier inspection')
const openPickers = async () => fireEvent.click(await screen.findByRole('button', { name: 'Choose from all photos' }))

describe('Image comparison page', () => {
  it('has the page header and is reachable from the sidebar', async () => {
    renderAt('/')                                                                         // the home address opens Compare Photos
    expect(await screen.findByRole('heading', { level: 1, name: 'Compare Photos' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Compare Photos' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByText('Choose a vessel and a location to compare its earlier and later inspection photographs.')).toBeInTheDocument()
  })

  it('shows a loading state while searching', async () => {
    api.searchInspections.mockReturnValue(new Promise(() => {}))
    renderAt(BASE)
    expect(await screen.findByRole('status', { name: /loading comparison data/i })).toBeInTheDocument()
  })

  it('selects a location from the location dropdown', async () => {
    renderAt('/compare')
    const select = await screen.findByLabelText('Location')
    await within(select).findByRole('option', { name: 'Deck One' })
    expect(within(select).queryByRole('option', { name: 'Not Labelled' })).toBeNull()      // placeholders are not offered
    fireEvent.change(select, { target: { value: 'Deck One' } })
    await waitFor(() => expect(currentUrl).toBe('/compare?location=Deck+One'))
    expect(api.searchInspections).toHaveBeenCalledWith({ location: 'Deck One', vessel: 'test-vessel', match: 'exact', includeAi: false }, expect.anything())
  })

  it('defaults to the two most recent dates and prevents the same date on both sides', async () => {
    renderAt(BASE)
    await ready()
    expect(earlierSelect()).toHaveValue('2026-08-30')
    expect(laterSelect()).toHaveValue('2026-09-06')
    const opt = (sel, v) => within(sel).getAllByRole('option').find((o) => o.value === v)
    expect(opt(earlierSelect(), '2026-09-06')).toBeDisabled()
    expect(opt(earlierSelect(), '2026-08-02')).not.toBeDisabled()
    expect(opt(laterSelect(), '2026-08-30')).toBeDisabled()
    expect(opt(laterSelect(), '2026-08-02')).toBeDisabled()
    // Week 34 date is flagged as derived next to the selector
    expect(screen.getAllByText('Date derived from worksheet').length).toBeGreaterThan(0)
  })

  it('changes the earlier and later dates independently', async () => {
    renderAt(BASE)
    await ready()
    fireEvent.change(earlierSelect(), { target: { value: '2026-08-02' } })
    await waitFor(() => expect(within(screen.getByRole('region', { name: 'Earlier image' })).getByText('Photo 1 of 2')).toBeInTheDocument())
    expect(currentUrl).toContain('earlier=2026-08-02')
    fireEvent.change(laterSelect(), { target: { value: '2026-08-30' } })
    await waitFor(() => expect(currentUrl).toContain('later=2026-08-30'))
    expect(screen.getByAltText('Later image W34-0001')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Later image' })).queryByText(/^Photo \d/)).toBeNull()   // only one photo
  })

  it('shows the first photograph of each date and lets the user choose any other', async () => {
    renderAt(`${BASE}&earlier=2026-08-02&later=2026-08-30`)
    await ready()
    expect(screen.getByAltText('Earlier image W30-0001')).toBeInTheDocument()
    expect(screen.getByAltText('Later image W34-0001')).toBeInTheDocument()
    expect(currentUrl).not.toMatch(/[?&][ab]=/)                                               // shown, not written to the URL
    expect(screen.queryByRole('region', { name: 'Earlier inspection photographs' })).toBeNull()  // grid folded away
    await openPickers()
    fireEvent.click(screen.getByRole('button', { name: /select photograph W30-0002 for the earlier/i }))
    await waitFor(() => expect(currentUrl).toContain('a=W30-0002'))
    expect(screen.getByAltText('Earlier image W30-0002')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /select photograph W30-0002 for the earlier/i })).toHaveAttribute('aria-pressed', 'true')
  })

  const COMPARED = `${BASE}&earlier=2026-08-02&later=2026-08-30&a=W30-0001&b=W34-0001`

  it('shows the selected pair with no comparison workspace, modes or zoom', async () => {
    renderAt(COMPARED)
    expect(await screen.findByAltText('Earlier image W30-0001')).toHaveAttribute('src', '/api/images/W30-0001')
    expect(screen.getByAltText('Later image W34-0001')).toHaveAttribute('src', '/api/images/W34-0001')
    expect(await screen.findByLabelText('Inspection result')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Comparison Workspace' })).toBeNull()
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
    expect(screen.queryByRole('button', { name: /zoom|fit to frame|earlier full size|later full size/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /detailed comparison/i })).toBeNull()
    expect(document.body.textContent).not.toMatch(/geometrically aligned|SHA-256/i)
  })

  it('steps through a date’s photographs with the arrows', async () => {
    renderAt(COMPARED)
    fireEvent.click(await screen.findByRole('button', { name: 'Next earlier photograph' }))
    await waitFor(() => expect(currentUrl).toContain('a=W30-0002'))
    expect(screen.getByAltText('Earlier image W30-0002')).toBeInTheDocument()
  })

  it('enlarges a photograph by clicking it', async () => {
    renderAt(COMPARED)
    fireEvent.click(await screen.findByAltText('Later image W34-0001'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: 'W34-0001' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'ArrowLeft' })
    expect(await within(dialog).findByRole('heading', { name: 'W30-0001' })).toBeInTheDocument()
  })

  it('warns when both selections are the same photograph file', async () => {
    renderAt(`${BASE}&earlier=2026-08-02&later=2026-08-30&a=W30-0002&b=W34-0001`)
    expect(await screen.findByRole('alert', { name: 'Duplicate image' })).toHaveTextContent('🔴 DUPLICATE IMAGE')
  })

  it('handles a missing photograph safely', async () => {
    renderAt(`${BASE}&a=W34-0001&b=week_35-json-0000`)
    const later = await screen.findByRole('region', { name: 'Later image' })
    expect(within(later).getByRole('img', { name: 'Photograph unavailable' })).toBeInTheDocument()
    fireEvent.error(screen.getByAltText('Earlier image W34-0001'))
    // SV-02: the session is re-checked once before the fallback is shown
    expect(await within(screen.getByRole('region', { name: 'Earlier image' })).findByRole('img', { name: 'Photograph could not be loaded' })).toBeInTheDocument()
  })

  it('applies a location source from a search link and explains when fewer than two dates remain', async () => {
    renderAt(`${BASE}&match=contains&source=ai`) // the Official/AI source filter belongs to the broader Contains mode
    await ready()
    expect(earlierSelect()).toHaveValue('2026-08-02')
    expect(laterSelect()).toHaveValue('2026-08-30')
  })

  it('explains when fewer than two dates remain for the source', async () => {
    renderAt(`${BASE}&match=contains&source=both`)
    expect(await screen.findByText('At least two inspection dates are needed to compare')).toBeInTheDocument()
  })

  it('shows an empty state and a recoverable error', async () => {
    api.searchInspections.mockResolvedValueOnce({ ...fx.searchResult, count: 0, items: [] })
    const { unmount } = renderAt('/compare?location=nowhere')
    expect(await screen.findByText('No inspection records match “nowhere”')).toBeInTheDocument()
    unmount()
    api.searchInspections.mockRejectedValueOnce(new api.ApiError('offline', 'Unable to reach the inspection service.'))
    renderAt(BASE)
    expect(await screen.findByText('Comparison data unavailable')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(await screen.findByLabelText('Earlier inspection')).toBeInTheDocument()
  })

  it('uses responsive layouts (stacked on small screens, paired on larger ones)', async () => {
    renderAt(COMPARED)
    const pair = (await screen.findByRole('region', { name: 'Earlier image' })).parentElement
    expect(pair.className).toContain('grid-cols-1')
    expect(pair.className).toContain('md:grid-cols-2')
  })

  it('Phase 3 search links to the comparison for the same location', async () => {
    renderAt('/search?location=main%20deck')
    fireEvent.click(await screen.findByRole('link', { name: /compare inspection dates/i }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Compare Photos' })).toBeInTheDocument()
    expect(currentUrl).toBe('/compare?location=main+deck&match=contains') // mixed results: the broad search is kept (Phase 4B)
  })
})

describe('URL state', () => {
  it('keeps both selections when they are made in quick succession', async () => {
    renderAt('/compare?location=main%20deck&earlier=2026-08-02&later=2026-08-30')
    await screen.findByLabelText('Earlier inspection')
    await openPickers()
    const a = screen.getByRole('button', { name: /select photograph W30-0001 for the earlier/i })
    const b = screen.getByRole('button', { name: /select photograph W34-0001 for the later/i })
    // both clicks inside one act(): the second update happens before any re-render
    const { act } = await import('@testing-library/react')
    act(() => {
      a.click()
      b.click()
    })
    await waitFor(() => expect(currentUrl).toContain('a=W30-0001'))
    expect(currentUrl).toContain('b=W34-0001')
  })
})
