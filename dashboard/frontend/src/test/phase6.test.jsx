/* Reviews (formerly AI Condition Analysis) and Reports, simplified for managers (2026-11-23).
   Mocked API responses only: no analysis is run and no request leaves the test. */
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
    getHealth: vi.fn(),
    getVessels: vi.fn(),
    getLocations: vi.fn(),
    getAllInspections: vi.fn(),
    searchInspections: vi.fn(),
    getAiStatus: vi.fn(),
    getAnalysisHistory: vi.fn(),
    getAllAnalyses: vi.fn(),
    getAnalysis: vi.fn(),
    reviewAnalysis: vi.fn(),
    getReportPreview: vi.fn(),
    downloadReport: vi.fn(),
    saveBlob: vi.fn(),
    getSession: vi.fn(),
  }
})

let currentUrl = ''
function UrlProbe() {
  const l = useLocation()
  currentUrl = l.pathname + l.search
  return null
}
const renderAt = (path) =>
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
      <UrlProbe />
    </MemoryRouter>,
  )

function analysis(id, overrides = {}) {
  const classification = overrides.classification ?? 'STABLE'
  return {
    analysis_id: id,
    created_at: '2026-11-18T10:00:00Z',
    vessel_id: 'test-vessel',
    vessel_name: 'Test Vessel',
    location: 'Main Deck',
    provider: 'gemini',
    model: 'gemini-3.8-flash',
    prompt_version: 'condition-compare-v1',
    simulated: false,
    interval_days: 28,
    earlier: { record_id: 'W30-0001', image_id: 'W30-0001', inspection_date: '2026-08-02', week: 'Week 30', official_location: null, ai_suggested_location: 'Main Deck' },
    later: { record_id: 'W34-0001', image_id: 'W34-0001', inspection_date: '2026-08-30', week: 'week 34', official_location: 'Main Deck' },
    classification,
    model_classification: classification,
    confidence: 'HIGH',
    comparison_reliability: 'MEDIUM',
    policy_adjustments: [],
    human_review_required: true,
    review_reasons: [],
    usage: { total_tokens: 4321 },
    supersedes: null,
    review: { status: 'PENDING_REVIEW', history: [] },
    ai_result: {
      same_area: 'SAME_AREA', earlier_image_quality: 'GOOD', later_image_quality: 'GOOD', classification,
      confidence: 'HIGH', comparison_reliability: 'MEDIUM', visual_observations: ['Same railing visible.'],
      deterioration_evidence: [], improvement_evidence: [], non_condition_differences: [], limitations: ['Lighting differs.'],
      explanation: 'No significant visible change can be established.',
    },
    ...overrides,
  }
}
// Deliberately out of order: the page must put the assessment awaiting review first.
const APPROVED = analysis('CA-20261118-bbbbbbbbbb', { location: 'Hold Two', created_at: '2026-11-19T10:00:00Z',
  review: { status: 'APPROVED', history: [{ status: 'APPROVED', reviewer: 'Chief Officer', at: '2026-11-19T11:00:00Z', note: 'Agreed.' }] } })
const PENDING = analysis('CA-20261118-aaaaaaaaaa', { classification: 'DETERIORATED',
  ai_result: { ...analysis('x').ai_result, classification: 'DETERIORATED',
    deterioration_evidence: [{ region: 'Railing', change: 'New rust streaks', earlier_appearance: 'clean', later_appearance: 'rust' }] } })
const REASSESS = analysis('CA-20261118-cccccccccc', { location: 'Deck One', classification: 'NEEDS_REVIEW',
  review: { status: 'NEEDS_REASSESSMENT', history: [] } })
const all = (items) => ({ total: items.length, items })
const PREVIEW = {
  vessel_name: 'Test Vessel',
  period: { from: '2026-08-02', to: '2026-09-06' },
  summary: { appearances: 4, unique_photographs: 3, inspection_dates: 3, weeks: ['Week 30', 'week 34'], locations: { 'Main Deck': 2 },
    not_labelled: 1, missing_photographs: 1, undated: 0, analyses: 1, simulated_analyses: 1, approved_analyses: 0 },
  analyses: [],
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: 4, items: fx.inspections.items })
  api.getAiStatus.mockResolvedValue({ available: false, message: 'AI condition analysis is not enabled on this server.' })
  api.getAllAnalyses.mockResolvedValue(all([APPROVED, PENDING, REASSESS]))
  api.getAnalysis.mockImplementation((id) => Promise.resolve([APPROVED, PENDING, REASSESS].find((a) => a.analysis_id === id)))
  api.getReportPreview.mockResolvedValue(PREVIEW)
  api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
  // Like current Chrome: scrollIntoView returns a Promise (an effect returning it used to crash the detail view).
  Element.prototype.scrollIntoView = vi.fn(() => Promise.resolve())
})

const assessments = () => screen.findByRole('list', { name: 'Assessments' })
const openDetail = async (location) => {
  fireEvent.click(within(await assessments()).getByRole('button', { name: new RegExp(`Open assessment: ${location}`) }))
  return screen.findByRole('region', { name: 'Assessment details' })
}

describe('Reviews page', () => {
  it('is in the menu and lists the assessments awaiting review first, without technical details', async () => {
    renderAt('/dashboard')
    fireEvent.click(within(await screen.findByRole('navigation', { name: 'Main navigation' })).getByRole('link', { name: 'Reviews' }))
    const list = await assessments()
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(1)                                                       // only "awaiting review" by default
    expect(rows[0]).toHaveTextContent('Main Deck')
    expect(rows[0]).toHaveTextContent('02 Aug 2026 → 30 Aug 2026')
    expect(rows[0]).toHaveTextContent('🔴 DETERIORATED')
    expect(rows[0]).toHaveTextContent('Awaiting review')
    expect(screen.getByRole('tab', { name: 'Awaiting review (1)' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'All assessments (3)' })).toBeInTheDocument()
    expect(api.getAllAnalyses).toHaveBeenCalledWith({ vessel: 'test-vessel' }, expect.anything())
    expect(document.body.textContent).not.toMatch(/gemini|condition-compare|confidence|token|High \/ Medium/i)
  })

  it('"All assessments" keeps pending first, then reassessment requests, then decided ones', async () => {
    renderAt('/reviews')
    fireEvent.click(await screen.findByRole('tab', { name: /All assessments/ }))
    await waitFor(() => expect(currentUrl).toBe('/reviews?show=all'))
    const rows = within(await assessments()).getAllByRole('listitem')
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('Main Deck'), expect.stringContaining('Deck One'), expect.stringContaining('Hold Two'),
    ])
    expect(rows[1]).toHaveTextContent('Reassessment requested')
    expect(rows[2]).toHaveTextContent('⚪ NEUTRAL')
    expect(rows[2]).toHaveTextContent('Approved')
  })

  it('opens an assessment with both photos, the dates, the result, the evidence and a link back to Compare Photos', async () => {
    renderAt('/reviews')
    const detail = await openDetail('Main Deck')
    expect(currentUrl).toBe('/reviews?id=CA-20261118-aaaaaaaaaa')
    expect(await within(detail).findByAltText('Earlier photo W30-0001')).toBeInTheDocument()
    expect(within(detail).getByAltText('Later photo W34-0001')).toBeInTheDocument()
    expect(within(detail).getByText('Main Deck', { selector: 'h3' })).toBeInTheDocument()
    expect(within(detail).getByLabelText('Inspection result')).toHaveTextContent('🔴 DETERIORATED')
    expect(within(detail).getByText('No significant visible change can be established.')).toBeInTheDocument()
    expect(within(detail).getByText(/New rust streaks/)).toBeInTheDocument()
    expect(within(detail).getByText('Lighting differs.')).toBeInTheDocument()
    expect(within(detail).getByLabelText('Review status')).toHaveTextContent('Awaiting review')
    expect(within(detail).getByRole('link', { name: /Open in Compare Photos/ }).getAttribute('href'))
      .toBe('/compare?location=Main+Deck&earlier=2026-08-02&later=2026-08-30&a=W30-0001&b=W34-0001&match=contains')
    expect(detail.textContent).not.toMatch(/gemini|prompt|token|CA-20261118/i)
  })

  it('approves after a confirmation, once, then shows the new status and success message', async () => {
    let resolve
    api.reviewAnalysis.mockReturnValue(new Promise((r) => { resolve = r }))
    renderAt('/reviews?id=CA-20261118-aaaaaaaaaa')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    fireEvent.click(await within(detail).findByRole('button', { name: 'Approve' }))
    const confirm = within(detail).getByRole('group', { name: 'Confirm review decision' })
    expect(confirm).toHaveTextContent('Approve this assessment?')
    expect(api.reviewAnalysis).not.toHaveBeenCalled()                                    // nothing until confirmed
    const go = within(confirm).getByRole('button', { name: 'Confirm: Approve' })
    fireEvent.click(go)
    fireEvent.click(go)                                                                  // a second click sends nothing more
    expect(api.reviewAnalysis).toHaveBeenCalledTimes(1)
    expect(api.reviewAnalysis).toHaveBeenCalledWith('CA-20261118-aaaaaaaaaa', { status: 'APPROVED', note: null })
    const reloads = api.getAllAnalyses.mock.calls.length
    resolve({ ...PENDING, review: { status: 'APPROVED', history: [{ status: 'APPROVED', reviewer: 'R. Jones', at: '2026-11-23T09:00:00Z' }] } })
    expect(await within(detail).findByRole('status', { name: 'Review saved' })).toHaveTextContent('Assessment approved.')
    expect(within(detail).getByLabelText('Review status')).toHaveTextContent('Approved')
    expect(within(detail).queryByRole('button', { name: 'Approve' })).toBeNull()             // no repeated approval
    expect(within(detail).getByLabelText('Review history')).toHaveTextContent('Approved by R. Jones')
    await waitFor(() => expect(api.getAllAnalyses.mock.calls.length).toBeGreaterThan(reloads)) // list refreshed
  })

  it('rejects with a note, and requests a reassessment; Cancel sends nothing', async () => {
    api.reviewAnalysis.mockImplementation((id, body) => Promise.resolve({ ...PENDING, review: { status: body.status, history: [] } }))
    renderAt('/reviews?id=CA-20261118-aaaaaaaaaa')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    fireEvent.click(await within(detail).findByRole('button', { name: 'Reject' }))
    fireEvent.click(within(detail).getByRole('button', { name: 'Cancel' }))
    expect(api.reviewAnalysis).not.toHaveBeenCalled()
    fireEvent.click(within(detail).getByRole('button', { name: 'Reject' }))
    fireEvent.change(within(detail).getByLabelText('Note (optional)'), { target: { value: ' Wrong area ' } })
    fireEvent.click(within(detail).getByRole('button', { name: 'Confirm: Reject' }))
    expect(await within(detail).findByText('Assessment rejected.')).toBeInTheDocument()
    expect(api.reviewAnalysis).toHaveBeenLastCalledWith('CA-20261118-aaaaaaaaaa', { status: 'REJECTED', note: 'Wrong area' })
    expect(within(detail).getByLabelText('Inspection result')).toHaveTextContent('🟡 NEEDS REVIEW')  // a rejected result is not shown as a change
    // SV-03: once decided, a reviewer is offered no further decision on it
    expect(within(detail).queryByRole('button', { name: /^(Approve|Reject|Request Reassessment)$/ })).toBeNull()
  })

  it('requests a reassessment of an assessment awaiting review', async () => {
    api.reviewAnalysis.mockImplementation((id, body) => Promise.resolve({ ...PENDING, review: { status: body.status, history: [] } }))
    renderAt('/reviews?id=CA-20261118-aaaaaaaaaa')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    fireEvent.click(await within(detail).findByRole('button', { name: 'Request Reassessment' }))
    fireEvent.click(within(detail).getByRole('button', { name: 'Confirm: Request Reassessment' }))
    expect(await within(detail).findByText('Reassessment requested.', { selector: 'p' })).toBeInTheDocument()
    expect(api.reviewAnalysis).toHaveBeenLastCalledWith('CA-20261118-aaaaaaaaaa', { status: 'NEEDS_REASSESSMENT', note: null })
  })

  // ------------------------------------------------------------------ SV-03: review rules (the server enforces them)
  it('a reviewer sees no decisions on a decided assessment, only who may correct it', async () => {
    renderAt('/reviews?id=CA-20261118-bbbbbbbbbb')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    expect(await within(detail).findByText('This assessment has been decided. Only an administrator can correct a decision.')).toBeInTheDocument()
    expect(within(detail).queryAllByRole('button', { name: /^(Approve|Reject|Request Reassessment|Change to)/ })).toHaveLength(0)
  })

  it('a reviewer who ran the analysis cannot approve it, but can reject it', async () => {
    // SV-02: reviewers receive no account identifiers -- only the server-computed requested_by_me flag.
    const own = { ...PENDING, requested_by: null, requested_by_me: true }
    api.getAnalysis.mockResolvedValue(own)
    api.getAllAnalyses.mockResolvedValue(all([own]))
    renderAt('/reviews?id=CA-20261118-aaaaaaaaaa')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    expect(await within(detail).findByText('You ran this analysis, so another reviewer or an administrator must approve it.')).toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(within(detail).getByRole('button', { name: 'Reject' })).toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Request Reassessment' })).toBeInTheDocument()
  })

  it('an analysis run by someone else can be approved', async () => {
    const theirs = { ...PENDING, requested_by: null, requested_by_me: false }
    api.getAnalysis.mockResolvedValue(theirs)
    api.getAllAnalyses.mockResolvedValue(all([theirs]))
    renderAt('/reviews?id=CA-20261118-aaaaaaaaaa')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    expect(await within(detail).findByRole('button', { name: 'Approve' })).toBeInTheDocument()
  })

  it('an administrator can correct a decision only with a reason of at least 10 characters', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.adminUser })
    api.reviewAnalysis.mockImplementation((id, body) => Promise.resolve({
      ...APPROVED, review: { status: body.status, history: [{ status: body.status, reviewer: 'Chief Officer', correction: true, note: body.note, at: '2026-11-23T09:00:00Z' }] },
    }))
    renderAt('/reviews?id=CA-20261118-bbbbbbbbbb')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    fireEvent.click(await within(detail).findByRole('button', { name: 'Change to Rejected' }))
    expect(within(detail).queryByText(/Only an administrator can correct/)).toBeNull()
    expect(within(detail).queryByRole('button', { name: 'Approve' })).toBeNull()       // already approved
    const confirm = within(detail).getByRole('group', { name: 'Confirm review decision' })
    expect(confirm).toHaveTextContent('Correct this decision from Approved to Rejected?')
    const go = within(confirm).getByRole('button', { name: 'Confirm: Change to Rejected' })
    expect(go).toBeDisabled()                                                            // reason required
    const reason = within(confirm).getByLabelText(/Reason for the correction \(required/)
    fireEvent.change(reason, { target: { value: 'too short' } })
    expect(go).toBeDisabled()
    fireEvent.change(reason, { target: { value: '  Wrong hatch photographed.  ' } })
    expect(go).toBeEnabled()
    fireEvent.click(go)
    await waitFor(() => expect(api.reviewAnalysis).toHaveBeenCalledWith('CA-20261118-bbbbbbbbbb', { status: 'REJECTED', note: 'Wrong hatch photographed.' }))
    expect(await within(detail).findByLabelText('Review history')).toHaveTextContent('Rejected (correction) by Chief Officer')
  })

  it('shows the server refusal (409) clearly and keeps the status unchanged', async () => {
    api.reviewAnalysis.mockRejectedValue(new api.ApiError('review_already_decided', api.AI_ERROR_MESSAGES.review_already_decided, 409))
    renderAt('/reviews?id=CA-20261118-aaaaaaaaaa')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    fireEvent.click(await within(detail).findByRole('button', { name: 'Approve' }))
    fireEvent.click(within(detail).getByRole('button', { name: 'Confirm: Approve' }))
    expect(await within(detail).findByRole('alert')).toHaveTextContent('This assessment has already been decided. Only an administrator can correct a decision.')
    expect(within(detail).getByLabelText('Review status')).toHaveTextContent('Awaiting review')
    expect(within(detail).queryByRole('status', { name: 'Review saved' })).toBeNull()
  })

  it('shows the server error and keeps the decision unsaved', async () => {
    api.reviewAnalysis.mockRejectedValue(new api.ApiError('forbidden', 'You do not have permission to review assessments.', 403))
    renderAt('/reviews?id=CA-20261118-aaaaaaaaaa')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    fireEvent.click(await within(detail).findByRole('button', { name: 'Approve' }))
    fireEvent.click(within(detail).getByRole('button', { name: 'Confirm: Approve' }))
    expect(await within(detail).findByRole('alert')).toHaveTextContent('You do not have permission to review assessments.')
    expect(within(detail).getByLabelText('Review status')).toHaveTextContent('Awaiting review')
    expect(within(detail).queryByRole('status', { name: 'Review saved' })).toBeNull()
  })

  it('a viewer sees the assessment and its history but no review buttons (SV-02)', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.viewerUser })
    renderAt('/reviews?id=CA-20261118-aaaaaaaaaa')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    expect(await within(detail).findByLabelText('View only')).toHaveTextContent('You have view-only access.')
    expect(within(detail).queryByRole('button', { name: /^(Approve|Reject|Request Reassessment)$/ })).toBeNull()
    expect(within(detail).queryByRole('button', { name: /Change to/ })).toBeNull()
    expect(api.reviewAnalysis).not.toHaveBeenCalled()
  })

  it('a viewer sees no correction controls on a decided assessment either', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.viewerUser })
    renderAt('/reviews?id=CA-20261118-cccccccccc&status=all')
    const detail = await screen.findByRole('region', { name: 'Assessment details' })
    expect((await within(detail).findAllByText(/Deck One/)).length).toBeGreaterThan(0)   // the decided assessment is shown
    expect(within(detail).queryByRole('button', { name: /^(Approve|Reject|Request Reassessment)$|Change to/ })).toBeNull()
    expect(within(detail).queryByLabelText('View only')).toBeNull()       // only shown while awaiting review
  })

  it('keeps the safety notes: automatic downgrades and test results are explained', async () => {
    const adjusted = analysis('CA-20261118-dddddddddd', { classification: 'NEEDS_REVIEW', model_classification: 'DETERIORATED',
      policy_adjustments: ['the AI reported a different or uncertain area'] })
    const mock = analysis('CA-20261118-eeeeeeeeee', { simulated: true })
    api.getAnalysis.mockImplementation((id) => Promise.resolve([adjusted, mock].find((a) => a.analysis_id === id)))
    const { unmount } = renderAt('/reviews?id=CA-20261118-dddddddddd')
    const note = await screen.findByRole('note', { name: 'Safety checks' })
    expect(note).toHaveTextContent('the AI reported a different or uncertain area')
    expect(screen.getByLabelText('Inspection result')).toHaveTextContent('🟡 NEEDS REVIEW')
    unmount()
    renderAt('/reviews?id=CA-20261118-eeeeeeeeee')
    expect(await screen.findByText(/test result from the development mock provider/)).toBeInTheDocument()
    expect(screen.getByLabelText('Inspection result')).toHaveTextContent('🟡 NEEDS REVIEW')
  })

  it('old AI Condition Analysis links open the same assessment on Reviews', async () => {
    renderAt('/analysis?id=CA-20261118-aaaaaaaaaa')
    expect(await screen.findByRole('region', { name: 'Assessment details' })).toBeInTheDocument()
    expect(currentUrl).toBe('/reviews?id=CA-20261118-aaaaaaaaaa')
  })

  it('shows empty, nothing-pending, loading and error states', async () => {
    api.getAllAnalyses.mockResolvedValue(all([]))
    const first = renderAt('/reviews')
    expect(await screen.findByText('No assessments yet.')).toBeInTheDocument()
    const box = screen.getByRole('region', { name: 'Assessments to review' })
    expect(within(box).getByRole('link', { name: 'Compare Photos' }).getAttribute('href')).toBe('/compare')
    first.unmount()
    api.getAllAnalyses.mockResolvedValue(all([APPROVED]))
    const second = renderAt('/reviews')
    expect(await screen.findByText('Nothing is awaiting review. All assessments have a decision.')).toBeInTheDocument()
    second.unmount()
    api.getAllAnalyses.mockReturnValue(new Promise(() => {}))
    const third = renderAt('/reviews')
    expect(await screen.findByRole('status', { name: 'Loading assessments' })).toBeInTheDocument()
    third.unmount()
    api.getAllAnalyses.mockRejectedValue(new api.ApiError('offline', 'Unable to reach the inspection service.'))
    renderAt('/reviews')
    expect(await screen.findByText('The assessments could not be loaded')).toBeInTheDocument()
  })
})

describe('Reports page', () => {
  it('offers a date range and two downloads, without AI filters or technical wording', async () => {
    renderAt('/reports')
    expect(await screen.findByRole('heading', { name: 'Inspection Reports' })).toBeInTheDocument()
    expect(screen.getByLabelText('Start date')).toBeInTheDocument()
    expect(screen.getByLabelText('End date')).toBeInTheDocument()
    expect(await screen.findByLabelText('Report contents')).toHaveTextContent('The report will include 4 photos from 3 inspections (02 Aug 2026 – 06 Sept 2026).')
    expect(screen.getByRole('button', { name: 'Download Inspection PDF' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Download Spreadsheet' })).toBeEnabled()
    expect(screen.queryByLabelText(/classification|location filter|inspection week/i)).toBeNull()
    expect(document.body.textContent).not.toMatch(/simulated|appearances|confidence/i)
  })

  it('sends the date range to the existing report endpoints and keeps it in the URL', async () => {
    renderAt('/reports')
    fireEvent.change(await screen.findByLabelText('Start date'), { target: { value: '2026-08-01' } })
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2026-08-31' } })
    await waitFor(() => expect(currentUrl).toBe('/reports?from=2026-08-01&to=2026-08-31'))
    await waitFor(() => expect(api.getReportPreview).toHaveBeenLastCalledWith(
      { vessel: 'test-vessel', date_from: '2026-08-01', date_to: '2026-08-31' }, expect.anything()))
    fireEvent.click(screen.getByRole('button', { name: 'Clear dates (all inspections)' }))
    await waitFor(() => expect(currentUrl).toBe('/reports'))
  })

  it('rejects a start date after the end date without requesting anything', async () => {
    renderAt('/reports?from=2026-09-30&to=2026-09-01')
    expect(await screen.findByRole('alert')).toHaveTextContent('The start date must be on or before the end date.')
    expect(api.getReportPreview).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Download Inspection PDF' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Download Spreadsheet' })).toBeDisabled()
  })

  it('downloads the PDF and the spreadsheet with the same filters', async () => {
    api.downloadReport.mockImplementation((kind) => Promise.resolve({ blob: new Blob(['x']), filename: kind === 'pdf' ? 'synergy-inspection-report_test.pdf' : 'synergy-inspections_test.csv' }))
    renderAt('/reports?from=2026-08-01')
    fireEvent.click(await screen.findByRole('button', { name: 'Download Inspection PDF' }))
    expect(await screen.findByText('Downloaded synergy-inspection-report_test.pdf')).toBeInTheDocument()
    expect(api.downloadReport).toHaveBeenCalledWith('pdf', { vessel: 'test-vessel', date_from: '2026-08-01', date_to: undefined })
    fireEvent.click(screen.getByRole('button', { name: 'Download Spreadsheet' }))
    expect(await screen.findByText('Downloaded synergy-inspections_test.csv')).toBeInTheDocument()
    expect(api.downloadReport).toHaveBeenLastCalledWith('inspectionsCsv', expect.objectContaining({ date_from: '2026-08-01' }))
    expect(api.saveBlob).toHaveBeenCalledTimes(2)
  })

  it('shows a friendly download error, and an empty range disables the downloads', async () => {
    api.downloadReport.mockRejectedValue(new api.ApiError('empty_report', api.AI_ERROR_MESSAGES.empty_report, 422))
    const { unmount } = renderAt('/reports')
    fireEvent.click(await screen.findByRole('button', { name: 'Download Inspection PDF' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(api.AI_ERROR_MESSAGES.empty_report)
    expect(api.saveBlob).not.toHaveBeenCalled()
    unmount()
    api.getReportPreview.mockResolvedValue({ ...PREVIEW, summary: { ...PREVIEW.summary, appearances: 0, analyses: 0 } })
    renderAt('/reports?from=2027-01-01')
    expect(await screen.findByText('No inspection photos fall within these dates. Widen the date range.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download Inspection PDF' })).toBeDisabled()
  })

  it('shows loading and error states for the report contents', async () => {
    api.getReportPreview.mockRejectedValue(new api.ApiError('offline', 'Unable to reach the inspection service.'))
    renderAt('/reports')
    expect(await screen.findByText('The report could not be prepared')).toBeInTheDocument()
  })

  it('the AI assessments spreadsheet is offered to administrators only', async () => {
    const { unmount } = renderAt('/reports')
    await screen.findByLabelText('Report contents')
    expect(screen.queryByRole('button', { name: /AI assessments/ })).toBeNull()             // reviewer
    unmount()
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.adminUser })
    api.downloadReport.mockResolvedValue({ blob: new Blob(['x']), filename: 'synergy-analyses_test.csv' })
    renderAt('/reports')
    fireEvent.click(await screen.findByRole('button', { name: 'Download AI assessments (CSV)' }))
    expect(await screen.findByText('Downloaded synergy-analyses_test.csv')).toBeInTheDocument()
    expect(api.downloadReport).toHaveBeenCalledWith('analysesCsv', expect.objectContaining({ vessel: 'test-vessel' }))
  })
})

describe('report download helper', () => {
  it('turns a JSON error inside a Blob into a friendly message', async () => {
    const actual = await vi.importActual('../services/api')
    const body = new Blob([JSON.stringify({ code: 'invalid_date_range', detail: 'raw' })], { type: 'application/json' })
    vi.spyOn(actual.http, 'get').mockRejectedValueOnce({ response: { status: 400, data: body } })
    await expect(actual.downloadReport('pdf', { date_from: '2026-09-30', date_to: '2026-09-01' })).rejects.toMatchObject({
      kind: 'invalid_date_range',
      message: actual.AI_ERROR_MESSAGES.invalid_date_range,
    })
  })
})
