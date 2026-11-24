/* Settings page: a read-only view of the server's actual configuration. Mocked API responses only;
   no request leaves the test, and no AI analysis is ever requested. */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { version as DASHBOARD_VERSION } from '../../package.json'
import App from '../App'
import * as api from '../services/api'
import * as fx from './fixtures'

vi.mock('../services/api', async (orig) => {
  const actual = await orig()
  return {
    ...actual,
    getHealth: vi.fn(), getVessels: vi.fn(), getLocations: vi.fn(), getAllInspections: vi.fn(), searchInspections: vi.fn(),
    getSession: vi.fn(), getAiStatus: vi.fn(), compareImages: vi.fn(),
  }
})

const GROQ_LIVE = {
  available: true, message: 'Groq live analysis enabled (qwen/qwen3.8-27b).', provider: 'groq', model: 'qwen/qwen3.8-27b',
  simulated: false, live: true, prompt_version: 'condition-compare-v1',
  classifications: ['DETERIORATED', 'AMELIORATED', 'STABLE', 'NEEDS_REVIEW'],
  review_statuses: ['PENDING_REVIEW', 'APPROVED', 'REJECTED', 'NEEDS_REASSESSMENT'],
  store: { stored_analyses: 3, unreadable_files: 0, interrupted_writes: 0 },
}
const HEALTH = { status: 'ok', weeks_loaded: 11, records: 1078, resolved_images: 1078, loaded_at: '2026-11-20T14:20:31' }
const FAKE_TOKEN = 'session-token-that-must-never-be-shown'

const renderSettings = () =>
  render(
    <MemoryRouter initialEntries={['/settings']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </MemoryRouter>,
  )
const section = async (name) => (await screen.findByRole('heading', { name, level: 3 })).closest('section')
const row = (container, label) => within(container).getByText(label, { selector: 'dt' }).nextElementSibling

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(HEALTH)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: fx.inspections.total, items: fx.inspections.items })
  api.getSession.mockResolvedValue({ authenticated: true, user: fx.adminUser })
  api.getAiStatus.mockResolvedValue(GROQ_LIVE)
  api.tokenStore.set(FAKE_TOKEN)
})
afterEach(() => {
  api.tokenStore.set(null)
  vi.useRealTimers()
})

describe('Settings page', () => {
  it('renders every section as read-only information, with no editable controls', async () => {
    renderSettings()
    expect(await screen.findByRole('heading', { name: 'Settings', level: 2 })).toBeInTheDocument()
    for (const name of ['Application Information', 'Inspection Data', 'AI Configuration', 'Security', 'System Information', 'Managed on the server']) {
      expect(await section(name)).toBeInTheDocument()
    }
    expect(screen.getAllByText('Read-only')).toHaveLength(5)
    expect(screen.queryByText(/planned for a later phase/i)).toBeNull()                     // no longer a placeholder
    const page = screen.getByTestId('settings-grid').parentElement
    for (const role of ['textbox', 'checkbox', 'combobox', 'switch', 'radio', 'spinbutton']) {
      expect(within(page).queryAllByRole(role)).toHaveLength(0)
    }
    expect(within(page).getAllByRole('button').map((b) => b.textContent.trim())).toEqual(['Refresh'])   // the only action
  })

  it('shows application information from the backend', async () => {
    renderSettings()
    const s = await section('Application Information')
    expect(row(s, 'Application')).toHaveTextContent('Synergy Vessel')
    expect(row(s, 'Dashboard version')).toHaveTextContent(DASHBOARD_VERSION)
    expect(await within(s).findByText('Connected')).toBeInTheDocument()
    expect(row(s, 'Data loading')).toHaveTextContent('Loaded')
    expect(row(s, 'Inspection weeks')).toHaveTextContent('11')
    expect(row(s, 'Inspection records')).toHaveTextContent('1,078')
  })

  it('shows inspection data: source status, period, records, photographs and weeks', async () => {
    renderSettings()
    const s = await section('Inspection Data')
    expect(await within(s).findByText('All photographs found')).toBeInTheDocument()
    expect(row(s, 'Vessels')).toHaveTextContent('Test Vessel')
    expect(row(s, 'Inspection period')).toHaveTextContent('02 Aug 2026 – 30 Aug 2026')
    expect(row(s, 'Photographs')).toHaveTextContent('1,078')
    expect(row(s, 'Inspection weeks')).toHaveTextContent('11')
  })

  it('flags photographs that could not be found', async () => {
    api.getHealth.mockResolvedValue({ ...HEALTH, resolved_images: 1070 })
    renderSettings()
    const s = await section('Inspection Data')
    expect(await within(s).findByText('8 photographs not found')).toBeInTheDocument()
    expect(await within(screen.getByRole('heading', { name: 'Settings', level: 2 }).closest('section')).findByText('Degraded')).toBeInTheDocument()
  })

  it('shows the actual AI configuration: Groq, model, enabled, live, human review; no Location AI row', async () => {
    renderSettings()
    const s = await section('AI Configuration')
    expect(await within(s).findByText('Groq')).toBeInTheDocument()
    expect(row(s, 'Model')).toHaveTextContent('qwen/qwen3.8-27b')
    expect(within(s).getByLabelText('Condition Analysis status')).toHaveTextContent('Enabled')
    expect(row(s, 'Mode')).toHaveTextContent('Live')
    expect(row(s, 'Human review')).toHaveTextContent('Required')
    expect(s.textContent).not.toMatch(/Location AI/)                               // removed from the UI
    expect(row(s, 'Saved condition analyses')).toHaveTextContent('3')
    expect(within(s).getByRole('note', { name: 'AI data use' }))
      .toHaveTextContent('the two selected photographs are sent to the configured external AI provider (Groq). Nothing is sent automatically')
    expect(api.compareImages).not.toHaveBeenCalled()
  })

  it('never shows secrets, key status, environment variable names or the session token', async () => {
    renderSettings()
    await screen.findByText('Groq')
    const text = document.body.textContent
    expect(text).not.toContain(FAKE_TOKEN)
    // (the "Managed on the server" explanation may mention "API keys" in prose; no names, values or key status)
    expect(text).not.toMatch(/gsk_|api_key|GROQ_API_KEY|key_configured|key configured|\.env|SYNERGY_/i)
  })

  it('shows AI as disabled when the server has it switched off', async () => {
    api.getAiStatus.mockResolvedValue({ ...GROQ_LIVE, available: false, provider: 'disabled', model: null, live: false,
      message: 'AI condition analysis is disabled.' })
    renderSettings()
    const s = await section('AI Configuration')
    expect(await within(s).findByLabelText('Condition Analysis status')).toHaveTextContent('Disabled')
    expect(row(s, 'Condition Analysis provider')).toHaveTextContent('Disabled')
    expect(within(s).getByRole('note', { name: 'AI data use' })).toHaveTextContent('no photographs are sent to any AI provider')
  })

  it('shows AI as unavailable with the server reason (e.g. key not configured, awaiting approval)', async () => {
    api.getAiStatus.mockResolvedValue({ ...GROQ_LIVE, available: false,
      message: 'Groq is selected but its API key is not configured (GROQ_API_KEY).' })
    renderSettings()
    const s = await section('AI Configuration')
    expect(await within(s).findByLabelText('Condition Analysis status')).toHaveTextContent('Unavailable')
    expect(within(s).getByRole('note', { name: 'AI status message' })).toHaveTextContent('not configured')
  })

  it('labels a mock provider as simulated', async () => {
    api.getAiStatus.mockResolvedValue({ ...GROQ_LIVE, provider: 'mock', model: 'mock-condition-model', simulated: true, live: false })
    renderSettings()
    const s = await section('AI Configuration')
    expect(await within(s).findByText('Simulated (mock)')).toBeInTheDocument()
    expect(within(s).getByRole('note', { name: 'AI data use' })).toHaveTextContent('no photographs leave the server')
  })

  it('shows security information for a signed-in administrator', async () => {
    renderSettings()
    const s = await section('Security')
    expect(row(s, 'Authentication')).toHaveTextContent('Enabled')
    expect(await within(s).findByText('Chief Officer')).toBeInTheDocument()
    expect(row(s, 'Role')).toHaveTextContent('Administrator')
    expect(row(s, 'Session')).toHaveTextContent('Active')
    expect(row(s, 'AI analysis')).toHaveTextContent('Requires sign-in')
    expect(within(s).getByRole('list', { name: 'Protected functions' })).toHaveTextContent('User accounts and the audit log — administrators only')
  })

  it('is limited to administrators: reviewers and viewers see only an access notice; signed-out users the sign-in screen', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
    const { unmount } = renderSettings()
    expect(await screen.findByRole('heading', { name: 'Administrator access required' })).toBeInTheDocument()
    expect(screen.getByText(/signed in as .*\(Reviewer\)/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Security', level: 3 })).toBeNull()
    unmount()
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.viewerUser })
    const viewer = renderSettings()
    expect(await screen.findByRole('heading', { name: 'Administrator access required' })).toBeInTheDocument()
    expect(screen.getByText(/signed in as .*\(Viewer\)/)).toBeInTheDocument()
    expect(screen.queryByTestId('settings-grid')).toBeNull()
    viewer.unmount()
    api.getSession.mockResolvedValue({ authenticated: false, user: null })
    renderSettings()
    expect(await screen.findByRole('form', { name: 'Sign in' })).toBeInTheDocument()         // SV-02 sign-in gate
    expect(screen.queryByRole('heading', { name: 'Administrator access required' })).toBeNull()
    expect(screen.queryByTestId('settings-grid')).toBeNull()
    expect(api.getAiStatus).not.toHaveBeenCalled()                                         // nothing is loaded for them
  })

  it('shows loading states while the configuration is being read', async () => {
    const never = () => new Promise(() => {})
    api.getAiStatus.mockImplementation(never)
    api.getHealth.mockImplementation(never)
    renderSettings()
    expect(await screen.findByText('Loading AI configuration…')).toBeInTheDocument()
    expect(screen.getByText('Loading inspection data…')).toBeInTheDocument()
    expect(within(screen.getByLabelText('System status')).getByText('Checking…')).toBeInTheDocument()
  })

  it('shows errors when the backend is unreachable, and Refresh tries again', async () => {
    const offline = new api.ApiError('offline', 'Cannot reach the inspection service.', null)
    api.getHealth.mockRejectedValue(offline)
    api.getAiStatus.mockRejectedValue(offline)
    renderSettings()
    const sys = await section('System Information')
    expect(await within(sys).findByText('Unreachable')).toBeInTheDocument()
    expect(row(sys, 'API connectivity')).toHaveTextContent('Cannot reach the inspection service.')
    expect(within(screen.getByLabelText('System status')).getByText('Unavailable')).toBeInTheDocument()
    expect(within(await section('Application Information')).getByText('Unavailable')).toBeInTheDocument()
    const ai = await section('AI Configuration')
    expect(within(ai).getByRole('alert')).toHaveTextContent('Unable to load the AI configuration')
    // recovery
    api.getHealth.mockResolvedValue(HEALTH)
    api.getAiStatus.mockResolvedValue(GROQ_LIVE)
    const calls = api.getHealth.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await within(ai).findByText('Groq')).toBeInTheDocument()
    expect(api.getHealth.mock.calls.length).toBeGreaterThan(calls)
    expect(within(screen.getByLabelText('System status')).getByText('Operational')).toBeInTheDocument()
  })

  it('refreshes the displayed status automatically every minute', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    renderSettings()
    await screen.findByText('Groq')
    const before = api.getAiStatus.mock.calls.length
    await act(async () => { vi.advanceTimersByTime(60000) })
    expect(api.getAiStatus.mock.calls.length).toBe(before + 1)
  })

  it('uses a responsive layout: one column on small screens, two on large; rows stack on phones', async () => {
    renderSettings()
    const grid = await screen.findByTestId('settings-grid')
    expect(grid.className).toMatch(/\bgrid\b/)
    expect(grid.className).toContain('lg:grid-cols-2')
    expect(grid.className).not.toMatch(/(^|\s)grid-cols-2/)                                 // single column by default
    expect(screen.getByTestId('settings-ai').className).toContain('lg:col-span-2')
    const s = await section('Application Information')
    const r = within(s).getByText('Application', { selector: 'dt' }).parentElement
    expect(r.className).toContain('flex-col')
    expect(r.className).toContain('sm:flex-row')
  })
})
