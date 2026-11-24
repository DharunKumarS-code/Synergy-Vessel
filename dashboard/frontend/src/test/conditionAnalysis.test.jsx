import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ConditionAnalysis from '../components/compare/ConditionAnalysis'
import { AuthProvider } from '../context/AuthContext'
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
    compareImages: vi.fn(),
    validateImagePair: vi.fn(),
    reviewAnalysis: vi.fn(),
    getSession: vi.fn(),
  }
})

// The AI analysis component is no longer shown on the Image Comparison page (manager feedback, 2026-11-21);
// it is tested directly with the same pair the page used to pass it.
const record = (id) => fx.searchResult.items.find((r) => r.record_id === id)
const renderPage = () =>
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AuthProvider>
        <ConditionAnalysis earlier={record('W30-0001')} later={record('W34-0001')} location="main deck" />
      </AuthProvider>
    </MemoryRouter>,
  )

const ENABLED = { available: true, message: 'Groq live analysis enabled (qwen/qwen3.8-27b).', provider: 'groq', model: 'qwen/qwen3.8-27b', simulated: false, live: true }
const EMPTY = { total: 0, count: 0, offset: 0, limit: 1, items: [] }

const evidence = {
  region: 'deck plating, lower left',
  earlier_appearance: 'Light rust staining.',
  later_appearance: 'Rust staining extends further.',
  change: 'Corrosion staining has spread.',
}

function makeAnalysis(overrides = {}) {
  const classification = overrides.classification ?? 'DETERIORATED'
  return {
    analysis_id: 'CA-20261118-0123456789',
    created_at: '2026-11-18T10:00:00Z',
    provider: 'groq',
    model: 'qwen/qwen3.8-27b',
    prompt_version: 'condition-compare-v1',
    simulated: false,
    earlier: { record_id: 'W30-0001', image_id: 'W30-0001', inspection_date: '2026-08-02' },
    later: { record_id: 'W34-0001', image_id: 'W34-0001', inspection_date: '2026-08-30' },
    classification,
    model_classification: overrides.model_classification ?? classification,
    confidence: 'MEDIUM',
    comparison_reliability: 'HIGH',
    policy_adjustments: [],
    human_review_required: true,
    review_reasons: ['deterioration findings always require confirmation'],
    supersedes: null,
    review: { status: 'PENDING_REVIEW', history: [] },
    ai_result: {
      same_area: 'SAME_AREA',
      earlier_image_quality: 'GOOD',
      later_image_quality: 'GOOD',
      classification: overrides.model_classification ?? classification,
      confidence: 'MEDIUM',
      comparison_reliability: 'HIGH',
      visual_observations: ['Same winch foundation visible in both photographs.'],
      deterioration_evidence: classification === 'DETERIORATED' ? [evidence] : [],
      improvement_evidence: classification === 'AMELIORATED' ? [{ ...evidence, change: 'Coating restored.' }] : [],
      non_condition_differences: ['Stronger sunlight in the later photograph.'],
      limitations: ['Slightly different camera position.'],
      explanation: 'Corrosion staining along the weld seam is more extensive later.',
    },
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: 4, items: fx.inspections.items })
  api.searchInspections.mockResolvedValue(fx.searchResult)
  api.getAiStatus.mockResolvedValue(ENABLED)
  api.validateImagePair.mockResolvedValue(fx.pairPassed)
  api.getAnalysisHistory.mockResolvedValue(EMPTY)
  api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
})

const section = () => screen.findByRole('region', { name: 'AI Condition Analysis' })

describe('AI condition analysis section', () => {
  it('asks for sign-in before running an AI analysis and sends nothing', async () => {
    api.getSession.mockResolvedValue({ authenticated: false, user: null })
    renderPage()
    const s = await section()
    expect(await within(s).findByText('Sign-in required to run an analysis')).toBeInTheDocument()
    fireEvent.click(within(s).getByRole('button', { name: 'Analyse images' }))
    expect(screen.getByRole('dialog', { name: 'Sign in' })).toHaveTextContent('Sign in to run an AI condition analysis.')
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('shows the empty state and never analyses automatically', async () => {
    renderPage()
    const s = await section()
    expect(await within(s).findByText('No AI condition assessment available.')).toBeInTheDocument()
    expect(api.getAnalysisHistory).toHaveBeenCalledWith({ earlier_record_id: 'W30-0001', later_record_id: 'W34-0001', limit: 1 }, expect.anything())
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('sends exactly the selected pair (earlier then later) and shows progress', async () => {
    let resolve
    api.compareImages.mockReturnValue(new Promise((r) => (resolve = r)))
    renderPage()
    const s = await section()
    fireEvent.click(await within(s).findByRole('button', { name: 'Analyse images' }))
    expect(within(s).getByRole('status', { name: 'Analysing photographs' })).toBeInTheDocument()
    expect(within(s).getByRole('button', { name: 'Analyse images' })).toBeDisabled()
    expect(api.compareImages).toHaveBeenCalledWith({
      earlier_record_id: 'W30-0001',
      later_record_id: 'W34-0001',
      location: 'main deck',
      earlier_inspection_date: '2026-08-02',
      later_inspection_date: '2026-08-30',
      reanalyse: false,
      acknowledge_warnings: false,
    })
    resolve({ reused: false, analysis: makeAnalysis() })
    expect(await within(s).findByText('Deteriorated')).toBeInTheDocument()
    expect(within(s).queryByRole('status', { name: 'Analysing photographs' })).not.toBeInTheDocument()
  })

  it('renders a full result: badge, confidence, evidence, limitations, explanation and review status', async () => {
    api.getAnalysisHistory.mockResolvedValue({ ...EMPTY, total: 1, count: 1, items: [makeAnalysis()] })
    renderPage()
    const s = await section()
    expect(await within(s).findByText('Deteriorated')).toBeInTheDocument()
    expect(within(s).getByRole('img', { name: 'Confidence: Medium' })).toBeInTheDocument()
    expect(within(s).getByRole('img', { name: 'Reliability: High' })).toBeInTheDocument()
    expect(within(s).getByText('Same winch foundation visible in both photographs.')).toBeInTheDocument()
    expect(within(s).getByText('Corrosion staining has spread.')).toBeInTheDocument()
    expect(within(s).getByText('Slightly different camera position.')).toBeInTheDocument()
    expect(within(s).getByText('Stronger sunlight in the later photograph.')).toBeInTheDocument()
    expect(within(s).getByText('Corrosion staining along the weld seam is more extensive later.')).toBeInTheDocument()
    expect(within(s).getByLabelText('Review status')).toHaveTextContent('Pending review')
    expect(within(s).getByText('Human review required')).toBeInTheDocument()
    // an existing, unreviewed result cannot be re-run (prevents duplicate API calls)
    expect(within(s).getByRole('button', { name: 'Analyse images' })).toBeDisabled()
  })

  it.each([
    ['AMELIORATED', 'Ameliorated'],
    ['STABLE', 'Stable'],
    ['NEEDS_REVIEW', 'Needs Review'],
  ])('displays the %s classification', async (value, label) => {
    api.getAnalysisHistory.mockResolvedValue({ ...EMPTY, total: 1, count: 1, items: [makeAnalysis({ classification: value })] })
    renderPage()
    const s = await section()
    expect(await within(s).findByText(label)).toBeInTheDocument()
  })

  it('explains when validation rules changed the model’s answer', async () => {
    const a = makeAnalysis({
      classification: 'NEEDS_REVIEW',
      model_classification: 'DETERIORATED',
      policy_adjustments: ['Changed from DETERIORATED to NEEDS_REVIEW: comparison reliability is LOW.'],
    })
    api.getAnalysisHistory.mockResolvedValue({ ...EMPTY, total: 1, count: 1, items: [a] })
    renderPage()
    const s = await section()
    expect(await within(s).findByText(/Changed from DETERIORATED to NEEDS_REVIEW/)).toBeInTheDocument()
    expect(within(s).getByText(/adjusted by validation rules/)).toBeInTheDocument()
  })

  it('names the provider, model and live status, and marks results AI-generated for human review', async () => {
    renderPage()
    const s = await section()
    expect(await within(s).findByLabelText('AI provider')).toHaveTextContent('Groq · qwen/qwen3.8-27b · live · one request per pair')
    expect(within(s).getByText(/AI-generated, requires human review/)).toBeInTheDocument()
  })

  it('shows a mock provider as simulated', async () => {
    api.getAiStatus.mockResolvedValue({ ...ENABLED, provider: 'mock', model: 'mock-condition-model', simulated: true, live: false })
    renderPage()
    const s = await section()
    expect(await within(s).findByLabelText('AI provider')).toHaveTextContent('Mock provider · mock-condition-model · simulated (mock)')
  })

  it('reports a Groq key that is not configured as unavailable and keeps analysis disabled', async () => {
    api.getAiStatus.mockResolvedValue({ available: false, message: 'Groq is selected but its API key is not configured (GROQ_API_KEY).', provider: 'groq', model: 'qwen/qwen3.8-27b', simulated: false, live: true })
    renderPage()
    const s = await section()
    expect(await within(s).findByText(/not configured/)).toBeInTheDocument()
    expect(within(s).getByRole('button', { name: 'Analyse images' })).toBeDisabled()
    expect(within(s).getByLabelText('AI provider')).toHaveTextContent('AI service not enabled')
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('labels simulated (mock) results clearly', async () => {
    api.getAnalysisHistory.mockResolvedValue({ ...EMPTY, total: 1, count: 1, items: [makeAnalysis({ simulated: true, provider: 'mock' })] })
    renderPage()
    const s = await section()
    expect(await within(s).findByText(/Simulated result from the development mock provider/)).toBeInTheDocument()
  })

  it('saves a human review and keeps the AI result', async () => {
    const a = makeAnalysis()
    api.getAnalysisHistory.mockResolvedValue({ ...EMPTY, total: 1, count: 1, items: [a] })
    api.reviewAnalysis.mockResolvedValue({
      ...a,
      review: { status: 'REJECTED', history: [{ status: 'REJECTED', reviewer: 'Inspector A', note: 'Different area', at: '2026-11-18T11:00:00Z' }] },
    })
    renderPage()
    const s = await section()
    const review = await within(s).findByRole('region', { name: 'Human review' })
    // Phase 8.2: the reviewer is the signed-in user; there is no free-text name to fill in
    expect(within(review).getByLabelText('Reviewer identity')).toHaveTextContent('R. Jones (Reviewer · r.jones)')
    expect(within(review).queryByRole('textbox', { name: 'Reviewer' })).not.toBeInTheDocument()
    fireEvent.change(within(review).getByLabelText('Review decision'), { target: { value: 'REJECTED' } })
    fireEvent.change(within(review).getByLabelText('Note (optional)'), { target: { value: 'Different area' } })
    fireEvent.click(within(review).getByRole('button', { name: 'Save review' }))
    await waitFor(() => expect(within(s).getByLabelText('Review status')).toHaveTextContent('Rejected'))
    expect(api.reviewAnalysis).toHaveBeenCalledWith(a.analysis_id, { status: 'REJECTED', note: 'Different area' })
    expect(within(s).getByText('Deteriorated')).toBeInTheDocument()
    // rejected results may be re-analysed, explicitly
    expect(within(s).getByRole('button', { name: 'Re-analyse images' })).toBeEnabled()
  })

  it('shows a friendly message when the AI service is unavailable', async () => {
    api.getAiStatus.mockResolvedValue({ available: false, message: 'Live Groq analysis is awaiting approval (SYNERGY_AI_ALLOW_LIVE is not set).', provider: 'groq', model: 'qwen/qwen3.8-27b' })
    renderPage()
    const s = await section()
    expect(await within(s).findByText(/awaiting approval/)).toBeInTheDocument()
    expect(within(s).getByRole('button', { name: 'Analyse images' })).toBeDisabled()
  })

  it('shows quota and API failures without fabricating a result', async () => {
    api.compareImages.mockRejectedValue(new api.ApiError('quota_exhausted', api.AI_ERROR_MESSAGES.quota_exhausted, 429))
    renderPage()
    const s = await section()
    fireEvent.click(await within(s).findByRole('button', { name: 'Analyse images' }))
    expect(await within(s).findByRole('alert')).toHaveTextContent('usage quota has been reached')
    expect(within(s).getByText('No AI condition assessment available.')).toBeInTheDocument()
  })
})

describe('AI error mapping', () => {
  it('maps backend codes to authored messages and hides raw details', () => {
    const err = (status, data) => ({ response: { status, data } })
    expect(api.toAiError(err(429, { code: 'quota_exhausted', detail: 'raw' })).message).toBe(api.AI_ERROR_MESSAGES.quota_exhausted)
    expect(api.toAiError(err(502, { code: 'invalid_ai_response', detail: 'invalid fields: x' })).message).toMatch(/No result was saved/)
    expect(api.toAiError(err(503, { detail: 'host 10.0.0.1' })).message).toBe(api.AI_ERROR_MESSAGES.ai_unavailable)
    expect(api.toAiError(err(400, { code: 'same_image' })).kind).toBe('same_image')
  })
})

describe('Phase 5A image-pair check', () => {
  const panel = async () => within(await section()).findByRole('region', { name: 'Image-pair check' })

  it('validates exactly the selected pair and shows a passed check', async () => {
    renderPage()
    const p = await panel()
    expect(api.validateImagePair).toHaveBeenCalledWith('W30-0001', 'W34-0001', expect.anything())
    expect(await within(p).findByTestId('pair-state')).toHaveTextContent('Checks passed')
    expect(within(p).getByText('Official Locations match')).toBeInTheDocument()
    expect(within(p).getByText('Separate photographs')).toBeInTheDocument()
    expect(within(p).getByText(/cannot prove that two photographs show the same physical area/)).toBeInTheDocument()
    const s = await section()
    await waitFor(() => expect(within(s).getByRole('button', { name: 'Analyse images' })).toBeEnabled())
  })

  it('renders warnings and requires acknowledgement before analysis', async () => {
    api.validateImagePair.mockResolvedValue(fx.pairWarnings)
    api.compareImages.mockResolvedValue({ reused: false, analysis: makeAnalysis() })
    renderPage()
    const s = await section()
    const p = await panel()
    expect(await within(p).findByTestId('pair-state')).toHaveTextContent('Warnings – review required')
    const warnings = within(p).getByRole('list', { name: 'Warnings' })
    expect(within(warnings).getAllByRole('listitem').map((li) => li.dataset.code)).toEqual(['ai_only_location', 'near_duplicate'])
    expect(within(p).getByRole('list', { name: 'Notes' })).toHaveTextContent('worksheet name')
    expect(within(p).getByText('AI-suggested locations only (unverified)')).toBeInTheDocument()
    expect(within(p).getByText('Near-duplicate content')).toBeInTheDocument()
    const button = within(s).getByRole('button', { name: 'Analyse images' })
    expect(button).toBeDisabled()
    expect(within(s).getByText('Acknowledge the warnings below to continue')).toBeInTheDocument()
    fireEvent.click(button)
    expect(api.compareImages).not.toHaveBeenCalled()

    fireEvent.click(within(p).getByRole('checkbox', { name: /I have checked these warnings/ }))
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(api.compareImages).toHaveBeenCalledTimes(1))
    expect(api.compareImages.mock.calls[0][0]).toMatchObject({ earlier_record_id: 'W30-0001', later_record_id: 'W34-0001', acknowledge_warnings: true })
  })

  it('blocks analysis and explains an exact duplicate without sending anything', async () => {
    api.validateImagePair.mockResolvedValue(fx.pairBlocked)
    renderPage()
    const s = await section()
    const p = await panel()
    expect(await within(p).findByTestId('pair-state')).toHaveTextContent('Analysis blocked')
    expect(within(p).getByRole('list', { name: 'Blocking problems' })).toHaveTextContent('byte-for-byte identical')
    expect(within(p).getByText('Same file (byte-for-byte identical)')).toBeInTheDocument()
    expect(within(p).queryByRole('checkbox')).not.toBeInTheDocument()
    const button = within(s).getByRole('button', { name: 'Analyse images' })
    expect(button).toBeDisabled()
    expect(within(s).getByText('Blocked by the image-pair check')).toBeInTheDocument()
    fireEvent.click(button)
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('keeps analysis disabled when the check itself fails', async () => {
    api.validateImagePair.mockRejectedValue(new api.ApiError('offline', 'Network error', 0))
    renderPage()
    const s = await section()
    expect(await within(s).findByText(/image-pair check could not be completed/)).toBeInTheDocument()
    expect(within(s).getByRole('button', { name: 'Analyse images' })).toBeDisabled()
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('maps the new server error codes to readable messages', () => {
    const err = (status, data) => ({ response: { status, data } })
    expect(api.toAiError(err(409, { code: 'validation_warnings' })).message).toMatch(/acknowledge/)
    expect(api.toAiError(err(422, { code: 'photograph_unreadable' })).message).toMatch(/damaged or unreadable/)
    expect(api.toAiError(err(400, { code: 'same_inspection_date' })).kind).toBe('same_inspection_date')
  })
})
