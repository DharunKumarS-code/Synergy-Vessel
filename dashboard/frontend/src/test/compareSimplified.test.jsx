/* Simplified Image Comparison (manager feedback, 2026-11-21): vessel and location, earlier/later images, a plain duplicate
   warning and the plain inspection result; no detailed comparison workspace. All API calls are mocked. */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { resultNote, simpleResult } from '../components/compare/InspectionOutcome'
import { AUTO_ANALYSIS, enableAutoAnalysisForTab, resetAutoAnalysisRegistry } from '../hooks/useAutoAnalysis'
import * as api from '../services/api'
import * as fx from './fixtures'

vi.mock('../services/api', async (orig) => {
  const actual = await orig()
  return {
    ...actual,
    getHealth: vi.fn(), getVessels: vi.fn(), getLocations: vi.fn(), getAllInspections: vi.fn(), searchInspections: vi.fn(),
    getAiStatus: vi.fn(), getAnalysisHistory: vi.fn(), compareImages: vi.fn(), validateImagePair: vi.fn(), reviewAnalysis: vi.fn(),
    getSession: vi.fn(),
  }
})

const COMPARED = '/compare?location=main%20deck&earlier=2026-08-02&later=2026-08-30&a=W30-0001&b=W34-0001'
const renderAt = (path = COMPARED) =>
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </MemoryRouter>,
  )

const analysis = (classification, extra = {}) => ({
  analysis_id: 'CA-20261121-0000000001', created_at: '2026-11-21T09:00:00Z', provider: 'groq', model: 'm', prompt_version: 'v1',
  simulated: false, classification, model_classification: classification, confidence: 'HIGH', comparison_reliability: 'HIGH',
  policy_adjustments: [], human_review_required: true, review_reasons: [], supersedes: null,
  review: { status: 'PENDING_REVIEW', history: [] },
  ai_result: {
    same_area: 'SAME_AREA', earlier_image_quality: 'GOOD', later_image_quality: 'GOOD', visual_observations: [], limitations: [],
    deterioration_evidence: [], improvement_evidence: [], non_condition_differences: [], explanation: 'Detailed AI explanation text.',
  },
  ...extra,
})
const history = (...items) => ({ total: items.length, count: items.length, offset: 0, limit: 1, items })
const result = () => screen.findByLabelText('Inspection result')
const expectResult = (text) => waitFor(() => expect(screen.getByLabelText('Inspection result')).toHaveTextContent(text))

beforeEach(() => {
  vi.clearAllMocks()
  resetAutoAnalysisRegistry()          // a freshly opened tab for every test (automatic analysis ON by default)
  AUTO_ANALYSIS.delayMs = 20           // the 2 s debounce itself is tested in sv05AutoAnalysis.test.jsx
  sessionStorage.clear()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: 4, items: fx.inspections.items })
  api.searchInspections.mockResolvedValue(fx.searchResult)
  api.getAiStatus.mockResolvedValue({ available: false, message: 'AI condition analysis is not enabled on this server.', provider: 'disabled', model: 'none' })
  api.validateImagePair.mockResolvedValue(fx.pairPassed)
  api.getAnalysisHistory.mockResolvedValue(history())
  api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
})

describe('primary comparison view', () => {
  it('shows only the selection, the two images and the result; no detailed comparison anywhere', async () => {
    renderAt()
    expect(await screen.findByAltText('Earlier image W30-0001')).toBeInTheDocument()
    expect(screen.getByAltText('Later image W34-0001')).toBeInTheDocument()
    expect(await result()).toHaveTextContent('🟡 NEEDS REVIEW')
    expect(screen.getByLabelText('Vessel')).toHaveValue('test-vessel')
    expect(screen.getByLabelText('Location')).toHaveValue('main deck')
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
    expect(screen.queryByRole('heading', { name: 'Comparison Workspace' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Comparison summary' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'AI Condition Analysis' })).toBeNull()
    expect(screen.queryByRole('button', { name: /detailed comparison/i })).toBeNull()
    expect(screen.queryByRole('slider')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('search')).toBeNull()
    expect(document.body.textContent).not.toMatch(/SHA-256|pHash|confidence|geometrically aligned/i)
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('changing the location from the dropdown starts a new exact comparison', async () => {
    renderAt('/compare?location=deck&match=contains')
    const select = await screen.findByLabelText('Location')
    expect(within(select).getByRole('option', { name: 'Contains “deck”' })).toBeInTheDocument()
    await within(select).findByRole('option', { name: 'Deck One' })
    fireEvent.change(select, { target: { value: 'Deck One' } })
    await waitFor(() => expect(api.searchInspections).toHaveBeenLastCalledWith(
      { location: 'Deck One', vessel: 'test-vessel', match: 'exact', includeAi: false }, expect.anything()))
  })

  it('checks the pair and loads its saved result once', async () => {
    renderAt()
    await result()
    expect(api.validateImagePair).toHaveBeenCalledTimes(1)
    expect(api.validateImagePair).toHaveBeenCalledWith('W30-0001', 'W34-0001', expect.anything())
    expect(api.getAnalysisHistory).toHaveBeenCalledTimes(1)
    expect(api.getAnalysisHistory).toHaveBeenCalledWith({ earlier_record_id: 'W30-0001', later_record_id: 'W34-0001', limit: 1 }, expect.anything())
  })

  it('opens either image full size', async () => {
    renderAt()
    fireEvent.click(await screen.findByRole('button', { name: 'Open later image full size' }))
    expect(within(await screen.findByRole('dialog')).getByRole('heading', { name: 'W34-0001' })).toBeInTheDocument()
  })

  it('stacks the images on small screens and pairs them on larger ones', async () => {
    renderAt()
    const earlier = (await screen.findByAltText('Earlier image W30-0001')).closest('section')
    expect(earlier.parentElement.className).toContain('grid-cols-1')
    expect(earlier.parentElement.className).toContain('md:grid-cols-2')
  })
})

describe('duplicate warning', () => {
  it.each([
    ['an exact duplicate', fx.pairBlocked],
    ['visually identical images', { ...fx.pairPassed, perceptual: { ...fx.pairPassed.perceptual, category: 'VISUALLY_IDENTICAL' } }],
    ['a near-duplicate', fx.pairWarnings],
  ])('is shown below the images for %s, in plain words', async (_, check) => {
    api.validateImagePair.mockResolvedValue(check)
    renderAt()
    const alert = await screen.findByRole('alert', { name: 'Duplicate image' })
    expect(alert).toHaveTextContent('🔴 DUPLICATE IMAGE')
    expect(alert).toHaveTextContent('These images show the same picture and cannot be used to identify a change.')
    expect(alert).not.toHaveTextContent(/SHA|hash|perceptual|distance/i)
    const later = screen.getByRole('region', { name: 'Later image' })
    expect(later.compareDocumentPosition(alert) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(alert.compareDocumentPosition(await result()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('uses the SHA-256 already on the records for the same file', async () => {
    renderAt('/compare?location=main%20deck&earlier=2026-08-02&later=2026-08-30&a=W30-0002&b=W34-0001')
    expect(await screen.findByRole('alert', { name: 'Duplicate image' })).toBeInTheDocument()
  })

  it('is not shown for separate photographs', async () => {
    renderAt()
    await result()
    expect(screen.queryByRole('alert', { name: 'Duplicate image' })).toBeNull()
  })
})

describe('inspection result', () => {
  it.each([
    ['AMELIORATED', '🟢 IMPROVED'],
    ['DETERIORATED', '🔴 DETERIORATED'],
    ['STABLE', '⚪ NEUTRAL'],
    ['NEEDS_REVIEW', '🟡 NEEDS REVIEW'],
  ])('shows %s as %s without confidence or explanations', async (classification, text) => {
    api.getAnalysisHistory.mockResolvedValue(history(analysis(classification, { review: { status: 'APPROVED', history: [] } })))
    renderAt()
    expect(await result()).toHaveTextContent(text)
    expect(screen.getByText('Confirmed by a reviewer.')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/Detailed AI explanation|confidence|Ameliorated|reliability/i)
  })

  it('shows NEEDS REVIEW when no assessment exists; nothing is started while analysis is unavailable', async () => {
    renderAt()
    expect(await result()).toHaveTextContent('🟡 NEEDS REVIEW')
    expect(screen.getByText('These photos have not been analysed yet.')).toBeInTheDocument()
    await new Promise((r) => setTimeout(r, 150))
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('never presents simulated or rejected assessments as a condition change', async () => {
    api.getAnalysisHistory.mockResolvedValue(history(analysis('DETERIORATED', { simulated: true })))
    const { unmount } = renderAt()
    expect(await result()).toHaveTextContent('🟡 NEEDS REVIEW')
    expect(screen.getByText('Only a test result exists for these photos.')).toBeInTheDocument()
    unmount()
    api.getAnalysisHistory.mockResolvedValue(history(analysis('DETERIORATED', { review: { status: 'REJECTED', history: [] } })))
    renderAt()
    expect(await result()).toHaveTextContent('🟡 NEEDS REVIEW')
    expect(screen.getByText('The previous assessment was rejected by a reviewer.')).toBeInTheDocument()
    expect(screen.queryByText(/DETERIORATED/)).toBeNull()
  })

  it('a result awaiting confirmation links to its details on Reviews', async () => {
    api.getAnalysisHistory.mockResolvedValue(history(analysis('DETERIORATED')))
    renderAt()
    expect(await result()).toHaveTextContent('🔴 DETERIORATED')
    expect(screen.getByText('Awaiting confirmation by a reviewer.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'See the details in Reviews' })).toHaveAttribute('href', '/reviews?id=CA-20261121-0000000001')
  })

  it('a confirmed result has no review link', async () => {
    api.getAnalysisHistory.mockResolvedValue(history(analysis('AMELIORATED', { review: { status: 'APPROVED', history: [] } })))
    renderAt()
    expect(await result()).toHaveTextContent('🟢 IMPROVED')
    expect(screen.queryByRole('link', { name: 'See the details in Reviews' })).toBeNull()
  })

  it('reports a result that cannot be loaded instead of guessing', async () => {
    api.getAnalysisHistory.mockRejectedValue(new api.ApiError('offline', 'Unable to reach the inspection service.'))
    renderAt()
    expect(await screen.findByText('The inspection result could not be loaded')).toBeInTheDocument()
    expect(screen.queryByLabelText('Inspection result')).toBeNull()
  })
})

describe('result mapping', () => {
  it('treats a reassessment request as needing review and explains it simply', () => {
    const a = analysis('AMELIORATED', { review: { status: 'NEEDS_REASSESSMENT', history: [] } })
    expect(simpleResult(a)).toBe('NEEDS_REVIEW')
    expect(resultNote(a, 'NEEDS_REVIEW')).toMatch(/assessed again/)
    expect(resultNote(undefined, 'NOT_ASSESSED')).toBe('These photos have not been assessed yet.')
  })
})

describe('Automatic analysis (SV-05)', () => {
  const ENABLED = { available: true, message: 'Groq live analysis enabled.', provider: 'groq', model: 'm', simulated: false, live: true }
  const settle = () => new Promise((r) => setTimeout(r, 150))   // well past the shortened test debounce
  beforeEach(() => {
    api.getAiStatus.mockResolvedValue(ENABLED)
    enableAutoAnalysisForTab()           // SV-06: the reviewer has switched automatic analysis ON in this tab
  })

  it('has no Analyse Images button; an eligible pair is analysed automatically, once, with progress, then the result', async () => {
    let resolve
    api.compareImages.mockReturnValue(new Promise((r) => { resolve = r }))
    renderAt()
    expect(await screen.findByRole('status', { name: 'Analysing photographs' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Analyse Images/ })).toBeNull()
    expect(api.compareImages).toHaveBeenCalledTimes(1)
    expect(api.compareImages).toHaveBeenCalledWith({
      earlier_record_id: 'W30-0001', later_record_id: 'W34-0001', location: 'main deck',
      earlier_inspection_date: '2026-08-02', later_inspection_date: '2026-08-30', reanalyse: false, acknowledge_warnings: false,
    })
    resolve({ analysis: analysis('AMELIORATED') })
    await expectResult('🟢 IMPROVED')
    expect(screen.getByText('Awaiting confirmation by a reviewer.')).toBeInTheDocument()
    await settle()
    expect(api.compareImages).toHaveBeenCalledTimes(1)                                  // never repeated for this pair
    expect(api.reviewAnalysis).not.toHaveBeenCalled()                                   // never approved or rejected
    expect(document.body.textContent).not.toMatch(/confidence|Detailed AI explanation/i)
  })

  it('shows a clear error and no invented result; no automatic retry, but a manual Try Again', async () => {
    api.compareImages.mockRejectedValueOnce(new api.ApiError('quota_exhausted', 'The AI service usage quota has been reached. Please try again later.'))
    renderAt()
    expect(await screen.findByText('The AI service usage quota has been reached. Please try again later.')).toBeInTheDocument()
    await expectResult('🟡 NEEDS REVIEW')
    await settle()
    expect(api.compareImages).toHaveBeenCalledTimes(1)
    api.compareImages.mockResolvedValueOnce({ analysis: analysis('STABLE') })
    fireEvent.click(screen.getByRole('button', { name: 'Try Again' }))
    await expectResult('⚪ NEUTRAL')
    expect(api.compareImages).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['too_many_requests', 429], ['analysis_in_progress', 409], ['ai_circuit_open', 503], ['ai_unavailable', 503],
  ])('explains a %s (%i) refusal and offers Try Again', async (code, status) => {
    api.compareImages.mockRejectedValue(new api.ApiError(code, api.AI_ERROR_MESSAGES[code], status))
    renderAt()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(api.AI_ERROR_MESSAGES[code])
    expect(within(alert).getByRole('button', { name: 'Try Again' })).toBeInTheDocument()
    await settle()
    expect(api.compareImages).toHaveBeenCalledTimes(1)
  })

  it('choosing another photograph clears the previous result and analyses the new pair', async () => {
    api.compareImages.mockResolvedValueOnce({ analysis: analysis('DETERIORATED') }).mockReturnValueOnce(new Promise(() => {}))
    renderAt()
    await expectResult('🔴 DETERIORATED')
    fireEvent.click(screen.getByRole('button', { name: 'Next earlier photograph' }))
    await screen.findByAltText('Earlier image W30-0002')
    expect(await screen.findByRole('status', { name: 'Analysing photographs' })).toBeInTheDocument()
    expect(screen.queryByText(/DETERIORATED/)).toBeNull()
    expect(api.compareImages).toHaveBeenLastCalledWith(expect.objectContaining({ earlier_record_id: 'W30-0002', later_record_id: 'W34-0001' }))
    expect(api.getAnalysisHistory).toHaveBeenLastCalledWith({ earlier_record_id: 'W30-0002', later_record_id: 'W34-0001', limit: 1 }, expect.anything())
  })

  it('a late reply for a pair that is no longer selected is ignored', async () => {
    let resolveFirst
    api.compareImages.mockReturnValueOnce(new Promise((r) => { resolveFirst = r })).mockReturnValueOnce(new Promise(() => {}))
    renderAt()
    await screen.findByRole('status', { name: 'Analysing photographs' })
    fireEvent.click(screen.getByRole('button', { name: 'Next earlier photograph' }))
    await screen.findByAltText('Earlier image W30-0002')
    await waitFor(() => expect(api.compareImages).toHaveBeenCalledTimes(2))
    resolveFirst({ analysis: analysis('DETERIORATED') })
    await settle()
    expect(screen.queryByText(/DETERIORATED/)).toBeNull()
    expect(screen.getByRole('status', { name: 'Analysing photographs' })).toBeInTheDocument()
  })

  it('never analyses an exact duplicate, and says why', async () => {
    api.validateImagePair.mockResolvedValue(fx.pairBlocked)
    renderAt()
    expect(await screen.findByRole('alert', { name: 'Duplicate image' })).toBeInTheDocument()
    expect(await screen.findByText('These photos are duplicates, so they cannot be analysed for a change.')).toBeInTheDocument()
    await settle()
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('a near-duplicate waits for the warnings to be acknowledged, then analyses with that acknowledgement', async () => {
    api.validateImagePair.mockResolvedValue(fx.pairWarnings)
    api.compareImages.mockResolvedValue({ analysis: analysis('NEEDS_REVIEW') })
    renderAt()
    const note = await screen.findByRole('note', { name: 'Check before analysing' })
    expect(note).toHaveTextContent('The photographs are nearly identical in content.')
    expect(note).toHaveTextContent('may not show a real change in condition')
    expect(await screen.findByText('Tick the box above to analyse these photos.')).toBeInTheDocument()
    await settle()
    expect(api.compareImages).not.toHaveBeenCalled()
    fireEvent.click(within(note).getByRole('checkbox'))
    await expectResult('🟡 NEEDS REVIEW')
    await waitFor(() => expect(api.compareImages).toHaveBeenCalledTimes(1))
    expect(api.compareImages).toHaveBeenCalledWith(expect.objectContaining({ acknowledge_warnings: true }))
  })

  it('a viewer never starts an analysis and sees no switch (SV-02)', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.viewerUser })
    renderAt()
    expect(await screen.findByText('You have view-only access. A reviewer can analyse these photos.')).toBeInTheDocument()
    await settle()
    expect(api.compareImages).not.toHaveBeenCalled()
    expect(screen.queryByRole('switch', { name: 'Analyse automatically' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Analyse/ })).toBeNull()
    expect(screen.queryByRole('dialog', { name: 'Sign in' })).toBeNull()
  })

  it('does nothing when analysis is not available on the server', async () => {
    api.getAiStatus.mockResolvedValue({ available: false, message: 'GROQ_API_KEY is not configured.', provider: 'groq', model: 'm' })
    renderAt()
    expect(await screen.findByText('Analysis is not available on this server right now.')).toBeInTheDocument()
    await settle()
    expect(api.compareImages).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toMatch(/GROQ_API_KEY/)
  })

  it('an existing result is kept; a rejected one is never re-analysed automatically, only by Analyse Again', async () => {
    api.getAnalysisHistory.mockResolvedValue(history(analysis('STABLE', { review: { status: 'APPROVED', history: [] } })))
    const { unmount } = renderAt()
    await expectResult('⚪ NEUTRAL')
    await settle()
    expect(api.compareImages).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Analyse Again' })).toBeNull()
    unmount()
    api.getAnalysisHistory.mockResolvedValue(history(analysis('DETERIORATED', { review: { status: 'REJECTED', history: [] } })))
    api.compareImages.mockResolvedValue({ analysis: analysis('STABLE', { analysis_id: 'CA-20261121-0000000002' }) })
    renderAt()
    const again = await screen.findByRole('button', { name: 'Analyse Again' })
    await waitFor(() => expect(again).toBeEnabled())
    await settle()
    expect(api.compareImages).not.toHaveBeenCalled()                                    // opening the pair created nothing
    fireEvent.click(again)
    await expectResult('⚪ NEUTRAL')
    expect(api.compareImages).toHaveBeenCalledTimes(1)
    expect(api.compareImages).toHaveBeenCalledWith(expect.objectContaining({ reanalyse: true }))
  })

  it('a rejected near-duplicate can be analysed again once its warnings are acknowledged', async () => {
    api.validateImagePair.mockResolvedValue(fx.pairWarnings)
    api.getAnalysisHistory.mockResolvedValue(history(analysis('DETERIORATED', { review: { status: 'REJECTED', history: [] } })))
    api.compareImages.mockResolvedValue({ analysis: analysis('STABLE', { analysis_id: 'CA-20261121-0000000002' }) })
    renderAt()
    const again = await screen.findByRole('button', { name: 'Analyse Again' })
    const note = await screen.findByRole('note', { name: 'Check before analysing' })
    expect(again).toBeDisabled()
    fireEvent.click(within(note).getByRole('checkbox'))
    await waitFor(() => expect(again).toBeEnabled())
    fireEvent.click(again)
    await expectResult('⚪ NEUTRAL')
    expect(api.compareImages).toHaveBeenCalledTimes(1)
    expect(api.compareImages).toHaveBeenCalledWith(expect.objectContaining({ reanalyse: true, acknowledge_warnings: true }))
  })

})

describe('Automatic analysis is ON by default', () => {
  const ENABLED = { available: true, message: 'Groq live analysis enabled.', provider: 'groq', model: 'm', simulated: false, live: true }
  const settle = () => new Promise((r) => setTimeout(r, 150))
  beforeEach(() => api.getAiStatus.mockResolvedValue(ENABLED))

  it('a newly opened page starts with the switch ON and analyses an eligible pair automatically (once)', async () => {
    api.compareImages.mockResolvedValue({ analysis: analysis('DETERIORATED') })
    renderAt()
    expect(await screen.findByRole('switch', { name: 'Analyse automatically' })).toBeChecked()
    await expectResult('🔴 DETERIORATED')
    await settle()
    expect(api.compareImages).toHaveBeenCalledTimes(1)
    expect(api.compareImages).toHaveBeenCalledWith(expect.objectContaining({ reanalyse: false }))
    expect(api.reviewAnalysis).not.toHaveBeenCalled()
  })

  it('switching it OFF before the short delay ends sends no AI request for the selected pair', async () => {
    AUTO_ANALYSIS.delayMs = 600                           // long enough to switch OFF before the automatic start
    api.compareImages.mockResolvedValue({ analysis: analysis('DETERIORATED') })
    renderAt()
    const sw = await screen.findByRole('switch', { name: 'Analyse automatically' })
    fireEvent.click(sw)
    expect(sw).not.toBeChecked()
    expect(await screen.findByText('Automatic analysis is off. Turn it on to analyse these photos.')).toBeInTheDocument()
    await new Promise((r) => setTimeout(r, 900))
    expect(api.compareImages).not.toHaveBeenCalled()
    await expectResult('🟡 NEEDS REVIEW')
  })

  it('switched OFF during a comparison, it is ON again when Compare Photos is reopened (and in a new tab)', async () => {
    AUTO_ANALYSIS.delayMs = 600
    api.compareImages.mockReturnValue(new Promise(() => {}))
    const first = renderAt()
    const sw = await screen.findByRole('switch', { name: 'Analyse automatically' })
    fireEvent.click(sw)
    expect(sw).not.toBeChecked()
    first.unmount()                                       // left Compare Photos (same tab)
    renderAt()
    expect(await screen.findByRole('switch', { name: 'Analyse automatically' })).toBeChecked()
    cleanup()
    resetAutoAnalysisRegistry()                           // a newly opened tab / a page refresh
    vi.clearAllMocks()
    api.getAiStatus.mockResolvedValue(ENABLED)
    renderAt()
    expect(await screen.findByRole('switch', { name: 'Analyse automatically' })).toBeChecked()
  })

  it('an old "off" setting left in the tab storage is ignored', async () => {
    sessionStorage.setItem('sv_auto_analysis', 'off')
    renderAt()
    expect(await screen.findByRole('switch', { name: 'Analyse automatically' })).toBeChecked()
  })

  it('switching it OFF stops further automatic requests', async () => {
    api.compareImages.mockResolvedValue({ analysis: analysis('DETERIORATED') })
    renderAt()
    const sw = await screen.findByRole('switch', { name: 'Analyse automatically' })
    expect(sw).toBeChecked()
    await expectResult('🔴 DETERIORATED')                 // analysed automatically
    fireEvent.click(sw)
    expect(sw).not.toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: 'Next earlier photograph' }))
    await screen.findByAltText('Earlier image W30-0002')
    await settle()
    expect(api.compareImages).toHaveBeenCalledTimes(1)
  })

  it('a viewer never sees the switch and never starts an analysis (SV-02)', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.viewerUser })
    enableAutoAnalysisForTab()                            // even if the setting were ON in this tab
    renderAt()
    expect(await screen.findByText('You have view-only access. A reviewer can analyse these photos.')).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Analyse automatically' })).toBeNull()
    await settle()
    expect(api.compareImages).not.toHaveBeenCalled()
  })
})

describe('Inline review on Compare Photos', () => {
  const ENABLED = { available: true, message: 'Gemini live analysis enabled.', provider: 'gemini', model: 'm', simulated: false, live: true }
  const panel = async () => {
    await result()
    return screen.findByRole('group', { name: 'Review this assessment' })
  }

  it('a pending result can be approved here after a confirmation, once, with the same review API', async () => {
    api.getAnalysisHistory.mockResolvedValue(history(analysis('DETERIORATED')))
    let resolve
    api.reviewAnalysis.mockReturnValue(new Promise((r) => { resolve = r }))
    renderAt()
    const g = await panel()
    fireEvent.click(within(g).getByRole('button', { name: 'Approve' }))
    expect(api.reviewAnalysis).not.toHaveBeenCalled()
    const go = within(g).getByRole('button', { name: 'Confirm: Approve' })
    fireEvent.click(go)
    fireEvent.click(go)
    expect(api.reviewAnalysis).toHaveBeenCalledTimes(1)
    expect(api.reviewAnalysis).toHaveBeenCalledWith('CA-20261121-0000000001', { status: 'APPROVED', note: null })
    resolve(analysis('DETERIORATED', { review: { status: 'APPROVED', history: [] } }))
    expect(await screen.findByRole('status', { name: 'Review saved' })).toHaveTextContent('Assessment approved.')
    expect(await result()).toHaveTextContent('🔴 DETERIORATED')
    expect(screen.getByText('Confirmed by a reviewer.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^(Approve|Reject|Request Reassessment)$/ })).toBeNull()   // no repeated decision
    expect(screen.queryByRole('link', { name: 'See the details in Reviews' })).toBeNull()
  })

  it('rejecting makes the result NEEDS REVIEW and allows analysing again; nothing is re-analysed automatically', async () => {
    api.getAiStatus.mockResolvedValue(ENABLED)
    api.getAnalysisHistory.mockResolvedValue(history(analysis('AMELIORATED')))
    api.reviewAnalysis.mockResolvedValue(analysis('AMELIORATED', { review: { status: 'REJECTED', history: [] } }))
    renderAt()
    const g = await panel()
    fireEvent.click(within(g).getByRole('button', { name: 'Reject' }))
    fireEvent.change(within(g).getByLabelText('Note (optional)'), { target: { value: 'Different hatch' } })
    fireEvent.click(within(g).getByRole('button', { name: 'Confirm: Reject' }))
    expect(await screen.findByText('Assessment rejected.')).toBeInTheDocument()
    expect(api.reviewAnalysis).toHaveBeenCalledWith('CA-20261121-0000000001', { status: 'REJECTED', note: 'Different hatch' })
    expect(await result()).toHaveTextContent('🟡 NEEDS REVIEW')
    const again = await screen.findByRole('button', { name: 'Analyse Again' })
    await waitFor(() => expect(again).toBeEnabled())
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('a reassessment request is sent with the existing status', async () => {
    api.getAnalysisHistory.mockResolvedValue(history(analysis('STABLE')))
    api.reviewAnalysis.mockResolvedValue(analysis('STABLE', { review: { status: 'NEEDS_REASSESSMENT', history: [] } }))
    renderAt()
    const g = await panel()
    fireEvent.click(within(g).getByRole('button', { name: 'Request Reassessment' }))
    fireEvent.click(within(g).getByRole('button', { name: 'Confirm: Request Reassessment' }))
    expect(await screen.findByText('Reassessment requested.')).toBeInTheDocument()
    expect(api.reviewAnalysis).toHaveBeenCalledWith('CA-20261121-0000000001', { status: 'NEEDS_REASSESSMENT', note: null })
    expect(await result()).toHaveTextContent('🟡 NEEDS REVIEW')
  })

  it('a freshly analysed pair can be reviewed straight away', async () => {
    api.getAiStatus.mockResolvedValue(ENABLED)
    api.compareImages.mockResolvedValue({ analysis: analysis('DETERIORATED') })
    enableAutoAnalysisForTab()                                                          // switched ON in this tab (SV-06)
    renderAt()                                                                          // analysed automatically (SV-05)
    await expectResult('🔴 DETERIORATED')
    expect(await screen.findByRole('group', { name: 'Review this assessment' })).toBeInTheDocument()
    expect(api.compareImages).toHaveBeenCalledTimes(1)
  })

  it('a viewer sees the result without review buttons (SV-02)', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.viewerUser })
    api.getAnalysisHistory.mockResolvedValue(history(analysis('DETERIORATED')))
    renderAt()
    expect(await result()).toHaveTextContent('🔴 DETERIORATED')
    expect(await screen.findByLabelText('View only')).toHaveTextContent('A reviewer records the decision')
    expect(screen.queryByRole('button', { name: /^(Approve|Reject|Request Reassessment)$/ })).toBeNull()
    expect(api.reviewAnalysis).not.toHaveBeenCalled()
  })

  it('a reviewer sees the review buttons for the same result', async () => {
    api.getAnalysisHistory.mockResolvedValue(history(analysis('DETERIORATED')))
    renderAt()
    expect(await screen.findByRole('button', { name: 'Approve' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
    expect(screen.queryByLabelText('View only')).toBeNull()
  })

  it('decided, rejected and test results offer no review buttons here', async () => {
    for (const a of [
      analysis('STABLE', { review: { status: 'APPROVED', history: [] } }),
      analysis('DETERIORATED', { review: { status: 'REJECTED', history: [] } }),
      analysis('DETERIORATED', { simulated: true }),
    ]) {
      api.getAnalysisHistory.mockResolvedValue(history(a))
      const { unmount } = renderAt()
      await result()
      await waitFor(() => expect(api.getAnalysisHistory).toHaveBeenCalled())
      expect(screen.queryByRole('group', { name: 'Review this assessment' })).toBeNull()
      unmount()
    }
  })

  it('shows a server error and keeps the result awaiting review', async () => {
    api.getAnalysisHistory.mockResolvedValue(history(analysis('DETERIORATED')))
    api.reviewAnalysis.mockRejectedValue(new api.ApiError('session_expired', 'Your session has expired. Sign in again.', 401))
    renderAt()
    const g = await panel()
    fireEvent.click(within(g).getByRole('button', { name: 'Approve' }))
    fireEvent.click(within(g).getByRole('button', { name: 'Confirm: Approve' }))
    expect(await within(g).findByRole('alert')).toHaveTextContent('Your session has expired. Sign in again.')
    expect(screen.getByText('Awaiting confirmation by a reviewer.')).toBeInTheDocument()
  })
})
