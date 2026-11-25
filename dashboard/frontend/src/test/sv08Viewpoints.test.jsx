/* SV-08: Image Comparison by physical viewpoint, and the administrator review of the viewpoint mapping.
   All API calls are mocked; no AI is ever called. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { AUTO_ANALYSIS, enableAutoAnalysisForTab, resetAutoAnalysisRegistry } from '../hooks/useAutoAnalysis'
import * as api from '../services/api'
import { PAIR, nextNewPhotoWeek, pairStatus, resolveWeeks, viewpointOfRecord, weekOptionLabel } from '../utils/viewpoints'
import * as fx from './fixtures'

vi.mock('../services/api', async (orig) => {
  const actual = await orig()
  return {
    ...actual,
    getHealth: vi.fn(), getVessels: vi.fn(), getLocations: vi.fn(), getAllInspections: vi.fn(), searchInspections: vi.fn(),
    getAiStatus: vi.fn(), getAnalysisHistory: vi.fn(), compareImages: vi.fn(), validateImagePair: vi.fn(), getSession: vi.fn(),
    getViewpoints: vi.fn(), getViewpointGroups: vi.fn(), getViewpointReview: vi.fn(), getViewpointHistory: vi.fn(),
    decideViewpoint: vi.fn(), reviewAnalysis: vi.fn(), listUsers: vi.fn(), getAuditEntries: vi.fn(), verifyAudit: vi.fn(),
  }
})

const LOC = 'Top View Main Deck'
const photo = (id, week, sha, status = 'approved', extra = {}) => ({
  record_id: id, image_id: id, image_url: `/api/images/${id}`, image_status: 'resolved', week: `Week ${week}`, week_number: week,
  inspection_date: `2026-10-${String(week - 30).padStart(2, '0')}`, official_location: LOC, official_location_labelled: true,
  extracted_photograph: { sha256: sha },
  viewpoint_member: { status, method: 'same_serial_within_layout', flags: [], flag_messages: [], evidence: [], repeat_of_previous_week: false },
  ...extra,
})
const week = (n, photos) => ({ week: n, week_label: `Week ${n}`, inspection_date: `2026-10-${String(n - 30).padStart(2, '0')}`, photos, available: photos.length > 0 })
const VP_A = {
  viewpoint_id: 'VP-TOP-VIEW-MAIN-DECK-01', display_name: 'Top view main deck - view 1', official_location: LOC,
  counts: { proposed: 0, needs_review: 0, approved: 4, rejected: 0 },
  weeks: [week(37, [photo('W37-0005', 37, 's1')]), week(38, [photo('W38-0021', 38, 's2')]),
    week(39, [photo('W39-0021', 39, 's2')]), week(40, [photo('W40-0021', 40, 's3')])],
}
const VP_B = {
  viewpoint_id: 'VP-TOP-VIEW-MAIN-DECK-02', display_name: 'Top view main deck - view 2', official_location: LOC,
  counts: { proposed: 2, needs_review: 1, approved: 0, rejected: 0 },
  weeks: [week(37, [photo('W37-0006', 37, 't1', 'needs_review')]), week(38, []),
    week(39, [photo('W39-0022', 39, 't2', 'proposed'), photo('W39-0099', 39, 't3', 'proposed')]), week(40, [])],
}
const MAPPED = {
  location: LOC, mapping: { available: true, revision: 3, problem: null },
  weeks: [37, 38, 39, 40].map((n) => ({ week: n, week_label: `Week ${n}`, inspection_date: null })),
  viewpoints: [VP_A, VP_B], unlinked: [],
}
const NO_MAPPING = { location: LOC, mapping: { available: false, revision: null, problem: 'No viewpoint mapping has been generated yet.' }, weeks: [], viewpoints: [], unlinked: [] }

const at = (path) =>
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </MemoryRouter>,
  )
const compareAt = (q = '') => at(`/compare?location=${encodeURIComponent(LOC)}${q}`)
const panel = (side) => screen.getByRole('region', { name: `${side} image` })

beforeEach(() => {
  vi.clearAllMocks()
  resetAutoAnalysisRegistry()
  AUTO_ANALYSIS.delayMs = 10
  sessionStorage.clear()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue([...fx.locations,
    { location: LOC, variants: [LOC], is_placeholder: false, record_count: 6, date_count: 4, vessel_ids: ['test-vessel'] },
    { location: 'Main Deck', variants: ['Main Deck'], is_placeholder: false, record_count: 6, date_count: 4, vessel_ids: ['test-vessel'] }])
  api.getAllInspections.mockResolvedValue({ total: 4, items: fx.inspections.items })
  api.searchInspections.mockResolvedValue(fx.searchResult)
  api.getAiStatus.mockResolvedValue({ available: true, provider: 'mock', model: 'm' })
  api.getAnalysisHistory.mockResolvedValue({ total: 0, count: 0, offset: 0, limit: 1, items: [] })
  api.validateImagePair.mockResolvedValue(fx.pairPassed)
  api.compareImages.mockResolvedValue({ analysis: null })
  api.getViewpoints.mockResolvedValue(MAPPED)
  api.getViewpointGroups.mockResolvedValue({
    mapping: { available: true, revision: 3 },
    groups: [{ group_id: 'main-deck-areas', display_name: 'Main Deck Areas', basis: 'x',
      locations: [{ location: 'Main Deck', viewpoint_count: 2 }, { location: LOC, viewpoint_count: 2 }] }],
  })
  api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
})

/* ------------------------------------------------------------------ pure helpers */
describe('viewpoint helpers', () => {
  it('defaults to the last two weeks with a photograph and keeps valid requested weeks', () => {
    expect(resolveWeeks(VP_A, null, null)).toEqual({ earlier: 39, later: 40 })
    expect(resolveWeeks(VP_B, null, null)).toEqual({ earlier: 37, later: 39 })
    expect(resolveWeeks(VP_A, 37, 38)).toEqual({ earlier: 37, later: 38 })
    expect(resolveWeeks(VP_A, 40, 37)).toEqual({ earlier: 40, later: null })
    expect(resolveWeeks(VP_A, 38, null)).toEqual({ earlier: 38, later: 39 })
  })
  it('classifies pairs: unavailable, not retaken, review, approved', () => {
    const [a] = VP_A.weeks[1].photos
    expect(pairStatus(a, null)).toBe(PAIR.UNAVAILABLE)
    expect(pairStatus(a, VP_A.weeks[2].photos[0])).toBe(PAIR.NOT_RETAKEN)
    expect(pairStatus(a, VP_A.weeks[3].photos[0])).toBe(PAIR.APPROVED)
    expect(pairStatus(VP_B.weeks[0].photos[0], VP_B.weeks[2].photos[0])).toBe(PAIR.REVIEW)
  })
  it('finds the next week with a new photograph and the viewpoint of a record', () => {
    expect(nextNewPhotoWeek(VP_A, 39, 's2')).toEqual({ week: 40, recordId: 'W40-0021' })
    expect(nextNewPhotoWeek(VP_A, 40, 's3')).toBeNull()
    expect(viewpointOfRecord([VP_A, VP_B], 'W39-0099')).toBe(VP_B)
    expect(weekOptionLabel(VP_B.weeks[1], (d) => d)).toBe('Week 38 · 2026-10-08 · no photograph')
  })
})

/* ------------------------------------------------------------------ Compare page */
describe('Image Comparison by viewpoint', () => {
  it('flows location -> viewpoint -> weeks; both sides come from the chosen viewpoint', async () => {
    compareAt()
    const select = await screen.findByLabelText('Viewpoint')
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Top view main deck - view 1 · approved', 'Top view main deck - view 2 · 0 of 3 approved'])
    expect(screen.getByLabelText('Earlier inspection week')).toHaveValue('39')
    expect(screen.getByLabelText('Later inspection week')).toHaveValue('40')
    expect(within(panel('Earlier')).getByAltText('Earlier image W39-0021')).toBeInTheDocument()
    expect(within(panel('Later')).getByAltText('Later image W40-0021')).toBeInTheDocument()
    expect(api.searchInspections).not.toHaveBeenCalled()
    // An approved, different-file pair gets the server pair check (which never calls the AI).
    await waitFor(() => expect(api.validateImagePair).toHaveBeenCalledWith('W39-0021', 'W40-0021', expect.anything()))

    fireEvent.change(select, { target: { value: VP_B.viewpoint_id } })
    expect(await within(panel('Earlier')).findByAltText('Earlier image W37-0006')).toBeInTheDocument()
    expect(within(panel('Later')).getByAltText('Later image W39-0022')).toBeInTheDocument()
  })

  it('shows NOT RETAKEN for the same file and moves to the next week with a new photograph', async () => {
    compareAt(`&vp=${VP_A.viewpoint_id}&ew=38&lw=39`)
    const notice = await screen.findByRole('status', { name: 'Not retaken' })
    expect(notice).toHaveTextContent('NOT RETAKEN – SAME PHOTOGRAPH')
    expect(api.validateImagePair).not.toHaveBeenCalled()
    fireEvent.click(within(notice).getByRole('button', { name: /next week with a new photograph \(Week 40\)/ }))
    await waitFor(() => expect(screen.getByLabelText('Later inspection week')).toHaveValue('40'))
    expect(within(panel('Later')).getByAltText('Later image W40-0021')).toBeInTheDocument()
  })

  it('shows PHOTOGRAPH NOT AVAILABLE for a week without a photograph of the viewpoint', async () => {
    compareAt(`&vp=${VP_B.viewpoint_id}&ew=37&lw=38`)
    expect(await within(await screen.findByRole('region', { name: 'Later image' })).findByText('PHOTOGRAPH NOT AVAILABLE FOR THIS INSPECTION WEEK')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Photograph not available' })).toBeInTheDocument()
    expect(api.validateImagePair).not.toHaveBeenCalled()
  })

  it('blocks AI for an unapproved mapping even with automatic analysis on', async () => {
    enableAutoAnalysisForTab()
    compareAt(`&vp=${VP_B.viewpoint_id}&ew=37&lw=39`)
    expect(await screen.findByRole('status', { name: 'Viewpoint mapping requires review' })).toHaveTextContent('VIEWPOINT MAPPING REQUIRES ADMIN REVIEW')
    await new Promise((r) => setTimeout(r, 60))
    expect(api.validateImagePair).not.toHaveBeenCalled()
    expect(api.compareImages).not.toHaveBeenCalled()
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
  })

  it('shows a historical assessment of an unapproved pair read-only, with a warning and a Reviews link, and never analyses it', async () => {
    enableAutoAnalysisForTab()
    const saved = {
      analysis_id: 'CA-20261123-5917cdff89', created_at: '2026-11-23T09:00:00Z', provider: 'gemini', model: 'm', simulated: false,
      classification: 'AMELIORATED', model_classification: 'AMELIORATED', review: { status: 'APPROVED', history: [] }, ai_result: {},
    }
    api.getAnalysisHistory.mockResolvedValue({ total: 1, count: 1, offset: 0, limit: 1, items: [saved] })
    compareAt(`&vp=${VP_B.viewpoint_id}&ew=37&lw=39`)
    expect(await screen.findByRole('status', { name: 'Viewpoint mapping requires review' })).toBeInTheDocument()
    const card = await screen.findByRole('region', { name: 'Saved assessment' })
    expect(card).toHaveTextContent('IMPROVED')                       // the saved result, unchanged
    expect(card).toHaveTextContent('Review status: Approved')        // the saved decision, unchanged
    expect(within(card).getByRole('note', { name: 'Historical viewpoint warning' })).toHaveTextContent('made before the viewpoint mapping')
    expect(within(card).getByRole('link', { name: 'Open this assessment in Reviews' })).toHaveAttribute('href', '/reviews?id=CA-20261123-5917cdff89')
    // Read-only: no decision or analysis controls on Compare.
    expect(within(card).queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /approve|reject|reassess|analyse/i })).not.toBeInTheDocument()
    expect(api.getAnalysisHistory).toHaveBeenCalledWith({ earlier_record_id: 'W37-0006', later_record_id: 'W39-0022', limit: 1 }, expect.anything())
    await new Promise((r) => setTimeout(r, 60))
    expect(api.compareImages).not.toHaveBeenCalled()
    expect(api.validateImagePair).not.toHaveBeenCalled()
    expect(api.reviewAnalysis).not.toHaveBeenCalled()
  })

  it('shows no saved-assessment card when an unapproved pair has none', async () => {
    compareAt(`&vp=${VP_B.viewpoint_id}&ew=37&lw=39`)
    expect(await screen.findByRole('status', { name: 'Viewpoint mapping requires review' })).toBeInTheDocument()
    await waitFor(() => expect(api.getAnalysisHistory).toHaveBeenCalled())
    expect(screen.queryByRole('region', { name: 'Saved assessment' })).not.toBeInTheDocument()
  })

  it('keeps every photograph of a week available with a picker', async () => {
    compareAt(`&vp=${VP_B.viewpoint_id}&ew=37&lw=39`)
    const later = await screen.findByRole('region', { name: 'Later image' })
    expect(within(later).getByText('Photo 1 of 2')).toBeInTheDocument()
    fireEvent.click(within(later).getByRole('button', { name: 'Next later photograph' }))
    expect(await within(panel('Later')).findByAltText('Later image W39-0099')).toBeInTheDocument()
  })

  it('opens an old link that carries only the a / b record IDs on the right viewpoint and weeks', async () => {
    compareAt('&a=W38-0021&b=W40-0021')
    expect(await screen.findByLabelText('Viewpoint')).toHaveValue(VP_A.viewpoint_id)
    expect(screen.getByLabelText('Earlier inspection week')).toHaveValue('38')
    expect(screen.getByLabelText('Later inspection week')).toHaveValue('40')
  })

  it('refuses an old link pairing two different viewpoints instead of silently replacing a photograph', async () => {
    enableAutoAnalysisForTab()
    api.getAnalysisHistory.mockResolvedValue({ total: 1, count: 1, offset: 0, limit: 1,
      items: [{ analysis_id: 'CA-20261123-b9a3f99ed7', classification: 'AMELIORATED', simulated: false, review: { status: 'PENDING_REVIEW' } }] })
    compareAt('&earlier=2026-10-07&later=2026-10-09&a=W37-0005&b=W39-0022')
    const alert = await screen.findByRole('alert', { name: 'Link pairs photographs that cannot be compared' })
    expect(alert).toHaveTextContent('THESE PHOTOGRAPHS CANNOT BE COMPARED')
    expect(alert).toHaveTextContent('W37-0005 (Top view main deck - view 1) with W39-0022 (Top view main deck - view 2)')
    expect(alert).toHaveTextContent('different physical viewpoints')
    // No photographs, no substitute (W39-0021 is the view-1 photograph of week 39), no result, no AI.
    expect(screen.queryByRole('region', { name: 'Image comparison' })).not.toBeInTheDocument()
    expect(screen.queryByAltText(/image W39-0021/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Inspection result')).not.toBeInTheDocument()
    await waitFor(() => expect(api.getAnalysisHistory).toHaveBeenCalledWith({ earlier_record_id: 'W37-0005', later_record_id: 'W39-0022', limit: 1 }, expect.anything()))
    expect(within(alert).getByRole('link', { name: 'Open this assessment in Reviews' })).toHaveAttribute('href', '/reviews?id=CA-20261123-b9a3f99ed7')
    await new Promise((r) => setTimeout(r, 60))
    expect(api.validateImagePair).not.toHaveBeenCalled()
    expect(api.compareImages).not.toHaveBeenCalled()

    // Each photograph can still be opened on its own viewpoint.
    fireEvent.click(within(alert).getByRole('button', { name: 'Open W39-0022 on Top view main deck - view 2' }))
    expect(await within(panel('Later')).findByAltText('Later image W39-0022')).toBeInTheDocument()
    expect(screen.getByLabelText('Viewpoint')).toHaveValue(VP_B.viewpoint_id)
    expect(screen.queryByRole('alert', { name: 'Link pairs photographs that cannot be compared' })).not.toBeInTheDocument()
  })

  it('refuses a link whose photograph is not linked to any viewpoint of the location', async () => {
    compareAt('&a=W38-0021&b=W99-0001')
    const alert = await screen.findByRole('alert', { name: 'Link pairs photographs that cannot be compared' })
    expect(alert).toHaveTextContent('W99-0001 (not linked to a viewpoint at this location)')
    expect(screen.queryByRole('region', { name: 'Image comparison' })).not.toBeInTheDocument()
    expect(api.validateImagePair).not.toHaveBeenCalled()
  })

  it('falls back to the location view, without AI, when no mapping exists', async () => {
    api.getViewpoints.mockResolvedValue(NO_MAPPING)
    compareAt()
    expect(await screen.findByRole('note', { name: 'Viewpoint mapping unavailable' })).toHaveTextContent('not analysed by the AI')
    await waitFor(() => expect(api.searchInspections).toHaveBeenCalled())
    expect(screen.queryByLabelText('Viewpoint')).not.toBeInTheDocument()
  })

  it('lists Not Labelled photographs separately under their original label and opens their viewpoints', async () => {
    // fx.locations already holds the placeholder { location: 'Not Labelled', is_placeholder: true }.
    expect(fx.locations.filter((l) => l.is_placeholder).map((l) => l.location)).toEqual(['Not Labelled'])
    const NL = 'Not Labelled'
    api.getViewpoints.mockImplementation(async ({ location }) => (location === NL
      ? { ...MAPPED, location: NL, viewpoints: [{ ...VP_B, viewpoint_id: 'VP-NOT-LABELLED-01', display_name: 'Not Labelled - view 1 (AI hint: Main Deck)', official_location: NL }] }
      : MAPPED))
    at('/compare')
    const loc = await screen.findByLabelText('Location')
    await waitFor(() => expect(loc.querySelector('optgroup[label="Photographs without an Official Location"]')).not.toBeNull())
    const group = loc.querySelector('optgroup[label="Photographs without an Official Location"]')
    expect([...group.querySelectorAll('option')].map((o) => [o.value, o.textContent])).toEqual([[NL, 'Not Labelled (no Official Location)']])
    // Not offered as a named location, and never under the Main Deck group.
    expect(loc.querySelector('optgroup[label="All locations"]').querySelector(`option[value="${NL}"]`)).toBeNull()
    expect(loc.querySelector('optgroup[label="Main Deck Areas"]').querySelector(`option[value="${NL}"]`)).toBeNull()

    fireEvent.change(loc, { target: { value: NL } })
    await waitFor(() => expect(api.getViewpoints).toHaveBeenCalledWith(expect.objectContaining({ location: NL }), expect.anything()))
    expect(await screen.findByLabelText('Viewpoint')).toHaveValue('VP-NOT-LABELLED-01')
    expect(loc).toHaveValue(NL)
    // Approval rules unchanged: an unapproved Not Labelled pair is still blocked from AI.
    expect(await screen.findByRole('status', { name: 'Viewpoint mapping requires review' })).toBeInTheDocument()
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('offers the display-only Main Deck Areas group without a fake Deck location', async () => {
    compareAt()
    const loc = await screen.findByLabelText('Location')
    await waitFor(() => expect(loc.querySelector('optgroup[label="Main Deck Areas"]')).not.toBeNull())
    const group = loc.querySelector('optgroup[label="Main Deck Areas"]')
    expect([...group.querySelectorAll('option')].map((o) => o.value)).toEqual(['Main Deck', LOC])
    expect([...loc.querySelectorAll('option')].some((o) => o.value.toLowerCase() === 'deck')).toBe(false)
  })
})

/* ------------------------------------------------------------------ Admin review */
const member = (id, wk, status, extra = {}) => ({
  record_id: id, week: wk, serial: 5, official_location: LOC, method: 'same_serial_within_layout', evidence: ['layout era 2, serial #21'],
  flags: [], status, reviewed_at: null, notes: null, sha256: id, reviewed_by: null, inspection_date: '2026-10-08', image_id: id,
  image_url: `/api/images/${id}`, ai_suggested_location: null, ...extra,
})
const REVIEW = {
  revision: 3,
  summary: { viewpoints: 2, members: { proposed: 2, needs_review: 1, approved: 4, rejected: 0 }, unlinked_records: 0, findings: 1 },
  matches: [{
    viewpoint_id: VP_B.viewpoint_id, display_name: VP_B.display_name, official_location: LOC, status: 'needs_review', repeated_photograph: false,
    earlier: member('W37-0006', 37, 'needs_review', { method: 'proposed_cross_layout', flags: ['cross_layout'] }), later: member('W39-0022', 39, 'proposed'),
  }],
  viewpoints: [{ viewpoint_id: VP_A.viewpoint_id, display_name: VP_A.display_name, official_location: LOC, members: [member('W37-0005', 37, 'approved')], counts: { approved: 1 } },
    { viewpoint_id: VP_B.viewpoint_id, display_name: VP_B.display_name, official_location: LOC, members: [member('W37-0006', 37, 'needs_review')], counts: { needs_review: 1 } }],
  unlinked: [], findings: [], generated: {}, warnings: [], flag_text: { cross_layout: 'Crosses a workbook layout change.' },
}

describe('Admin: viewpoint mapping review', () => {
  beforeEach(() => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.adminUser })
    api.getViewpointReview.mockResolvedValue(REVIEW)
    api.getViewpointHistory.mockResolvedValue({ revision: 3, items: [] })
    api.listUsers.mockResolvedValue({ users: [] })
    api.getAuditEntries.mockResolvedValue({ entries: [] })
    api.verifyAudit.mockResolvedValue({ valid: true, entries: 0, problems: [] })
  })

  it('shows both photographs with evidence and flags, and approves with the mapping revision', async () => {
    api.decideViewpoint.mockResolvedValue({ revision: 4, history_entry: { action: 'approve' } })
    at('/admin?section=viewpoints')
    const card = await screen.findByRole('article', { name: 'Match W37-0006 to W39-0022' })
    expect(within(card).getByText('Crosses a workbook layout change.', { exact: false })).toBeInTheDocument()
    expect(within(card).getAllByLabelText('Evidence')).toHaveLength(2)
    fireEvent.change(within(card).getByLabelText(/Notes/), { target: { value: 'same hatch, same angle' } })
    fireEvent.click(within(card).getByRole('button', { name: /Approve same viewpoint/ }))
    await waitFor(() => expect(api.decideViewpoint).toHaveBeenCalledWith({
      action: 'approve', viewpoint_id: VP_B.viewpoint_id, record_ids: ['W37-0006', 'W39-0022'], notes: 'same hatch, same angle', revision: 3,
    }))
    expect(await screen.findByText(/Saved \(approve\)\. Mapping revision 4\./)).toHaveAttribute('role', 'status')
    await waitFor(() => expect(api.getViewpointReview.mock.calls.length).toBeGreaterThanOrEqual(4))   // reloaded after the decision
  })

  it('uses compact thumbnails beside the evidence and opens the full photograph on click', async () => {
    at('/admin?section=viewpoints')
    const card = await screen.findByRole('article', { name: 'Match W37-0006 to W39-0022' })
    const thumb = within(card).getByRole('button', { name: 'Open earlier photograph W37-0006 full size' })
    // Fixed thumbnail size (no full-width photographs), evidence and flags still visible without expanding anything.
    expect(thumb.className).toContain('h-[9.75rem]')
    expect(thumb.className).toContain('w-[13rem]')
    expect(card.querySelector('.aspect-\\[4\\/3\\]')).toBeNull()
    expect(within(card).getAllByLabelText('Evidence')).toHaveLength(2)
    expect(within(card).getByLabelText('Flags')).toHaveTextContent('Crosses a workbook layout change.')
    // One photograph element per side, each loaded lazily from the normal photo URL.
    const imgs = within(card).getAllByRole('img')
    expect(imgs.map((i) => i.getAttribute('loading'))).toEqual(['lazy', 'lazy'])
    expect(imgs.map((i) => i.getAttribute('src'))).toEqual(['/api/images/W37-0006', '/api/images/W39-0022'])

    fireEvent.click(within(card).getByRole('button', { name: 'Open later photograph W39-0022 full size' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByAltText('Inspection photograph W39-0022')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close viewer' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('shows the server message when the mapping changed in the meantime', async () => {
    api.decideViewpoint.mockRejectedValue(new api.ApiError('mapping_changed', 'The viewpoint mapping was changed by someone else.', 409))
    at('/admin?section=viewpoints')
    const card = await screen.findByRole('article', { name: 'Match W37-0006 to W39-0022' })
    fireEvent.click(within(card).getByRole('button', { name: /Reject later photograph/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('changed by someone else')
  })

  it('offers only viewpoints with an active photograph as Move/Join targets, not ones left with rejected records only', async () => {
    const emptied = { viewpoint_id: `${VP_B.viewpoint_id}-S1`, display_name: `${VP_B.display_name} (from week 38)`, official_location: LOC,
      members: [member('W38-0007', 38, 'rejected')], counts: { rejected: 1 } }
    api.getViewpointReview.mockResolvedValue({ ...REVIEW, viewpoints: [...REVIEW.viewpoints, emptied] })
    at('/admin?section=viewpoints')
    const card = await screen.findByRole('article', { name: 'Match W37-0006 to W39-0022' })
    fireEvent.click(within(card).getByRole('button', { name: /Correct/ }))
    const target = within(card).getByLabelText(/Move the later photograph, or join/)
    expect([...target.options].map((o) => o.value)).toEqual(['', VP_A.viewpoint_id])   // the match's own viewpoint is excluded as before
    expect(within(target).queryByText(/from week 38/)).toBeNull()
    expect(api.decideViewpoint).not.toHaveBeenCalled()
  })

  it('is not offered to reviewers', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
    at('/admin?section=viewpoints')
    expect(await screen.findByText('Administrator access required')).toBeInTheDocument()
    expect(api.getViewpointReview).not.toHaveBeenCalled()
  })
})
