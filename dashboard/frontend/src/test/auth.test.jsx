import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { act } from 'react'
import { MemoryRouter } from 'react-router-dom'
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
    compareImages: vi.fn(),
    validateImagePair: vi.fn(),
    reviewAnalysis: vi.fn(),
    getImportHistory: vi.fn(),
    previewImport: vi.fn(),
    getSession: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
    listUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    verifyAudit: vi.fn(),
    getAuditEntries: vi.fn(),
  }
})

const renderAt = (path) =>
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </MemoryRouter>,
  )

const COMPARED = '/compare?location=main%20deck&earlier=2026-08-02&later=2026-08-30&a=W30-0001&b=W34-0001'
const EMPTY = { total: 0, count: 0, offset: 0, limit: 1, items: [] }
const signedOut = { authenticated: false, user: null }
const USERS = {
  users: [
    { ...fx.adminUser, active: true, created_at: '2026-11-19T08:00:00+00:00', active_sessions: 1 },
    { ...fx.reviewerUser, active: true, created_at: '2026-11-19T09:00:00+00:00', active_sessions: 0 },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: 4, items: fx.inspections.items })
  api.searchInspections.mockResolvedValue(fx.searchResult)
  api.getAiStatus.mockResolvedValue({ available: true, message: 'ok', provider: 'mock', model: 'mock', simulated: true, live: false })
  api.validateImagePair.mockResolvedValue(fx.pairPassed)
  api.getAnalysisHistory.mockResolvedValue(EMPTY)
  api.getImportHistory.mockResolvedValue({ imports: [], failed_attempts: [], interrupted_imports: 0, problems: [] })
  api.getSession.mockResolvedValue(signedOut)
  api.logout.mockResolvedValue()
  api.listUsers.mockResolvedValue(USERS)
  api.verifyAudit.mockResolvedValue({ valid: true, entries: 12, problems: [], last_hash: 'a'.repeat(64) })
  api.getAuditEntries.mockResolvedValue({
    entries: [{ seq: 12, at: '2026-11-19T10:00:00+00:00', event: 'login_succeeded', actor: { username: 'chief' }, target: 'chief' }],
  })
})

// SV-02: the application is only shown to a signed-in user.
const DATA_CALLS = ['getVessels', 'getLocations', 'getAllInspections', 'searchInspections', 'getAnalysisHistory',
  'getAiStatus', 'validateImagePair', 'getImportHistory', 'listUsers']
const noDataRequested = () => DATA_CALLS.forEach((name) => expect(api[name], name).not.toHaveBeenCalled())

describe('Sign-in gate (SV-02)', () => {
  it('signed out: shows only the sign-in screen and health, and requests no protected data', async () => {
    renderAt('/dashboard')
    const form = await screen.findByRole('form', { name: 'Sign in' })
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(within(form).getByLabelText('Username')).toBeInTheDocument()
    expect(await screen.findByRole('status', { name: 'API connected' })).toBeInTheDocument()   // public, minimal health
    expect(screen.queryByRole('navigation', { name: 'Main navigation' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Something went wrong/)).not.toBeInTheDocument()
    expect(document.querySelector('img[src^="/api/images/"]')).toBeNull()                    // no photo requests
    noDataRequested()
  })

  it('signs in on the sign-in screen, loads the application, shows the identity and signs out', async () => {
    api.login.mockResolvedValue(fx.reviewerUser)
    renderAt('/dashboard')
    const form = await screen.findByRole('form', { name: 'Sign in' })
    fireEvent.click(within(form).getByRole('button', { name: 'Sign in' }))
    expect(within(form).getByRole('alert')).toHaveTextContent('Enter your username and password.')
    fireEvent.change(within(form).getByLabelText('Username'), { target: { value: ' r.jones ' } })
    fireEvent.change(within(form).getByLabelText('Password'), { target: { value: 'a long password' } })
    noDataRequested()
    fireEvent.click(within(form).getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByLabelText('Signed-in user')).toHaveTextContent('R. Jones')
    expect(api.login).toHaveBeenCalledWith('r.jones', 'a long password')
    await waitFor(() => expect(api.getVessels).toHaveBeenCalled())                             // data only after sign-in
    expect(screen.getByRole('navigation', { name: 'Main navigation' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('form', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Main navigation' })).not.toBeInTheDocument()
    expect(api.logout).toHaveBeenCalled()
  })

  it('shows the server message for rejected credentials and clears the password', async () => {
    api.login.mockRejectedValue(new api.ApiError('invalid_credentials', 'The username or password is incorrect.', 401))
    renderAt('/')
    const form = await screen.findByRole('form', { name: 'Sign in' })
    fireEvent.change(within(form).getByLabelText('Username'), { target: { value: 'x' } })
    fireEvent.change(within(form).getByLabelText('Password'), { target: { value: 'wrong one' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Sign in' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('The username or password is incorrect.')
    expect(within(form).getByLabelText('Password')).toHaveValue('')
    noDataRequested()
  })

  it('an expired session returns to the sign-in screen with a notice and clears the photo cookie', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
    renderAt('/dashboard')
    expect(await screen.findByLabelText('Signed-in user')).toHaveTextContent('R. Jones')
    act(() => window.dispatchEvent(new Event(api.SESSION_EXPIRED_EVENT)))
    expect(await screen.findByRole('status', { name: 'Session notice' })).toHaveTextContent('Your session has expired')
    expect(screen.queryByLabelText('Signed-in user')).not.toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Main navigation' })).not.toBeInTheDocument()
    expect(screen.getByRole('form', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText(/Something went wrong/)).not.toBeInTheDocument()
    expect(api.logout).toHaveBeenCalled()       // the server clears the HttpOnly photo cookie
  })

  it('a stale session found at start-up shows the sign-in screen without an "expired" notice', async () => {
    renderAt('/reviews')
    await screen.findByRole('form', { name: 'Sign in' })
    act(() => window.dispatchEvent(new Event(api.SESSION_EXPIRED_EVENT)))
    expect(screen.queryByRole('status', { name: 'Session notice' })).not.toBeInTheDocument()
  })
})

describe('Access policy', () => {
  it('a signed-out visitor opening an administration page sees only the sign-in screen', async () => {
    renderAt('/imports')
    expect(await screen.findByRole('form', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Excel workbook')).not.toBeInTheDocument()
    noDataRequested()
  })

  it('a viewer sees the manager pages, but administration pages and their menu need an administrator', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.viewerUser })
    renderAt('/imports')
    expect(await screen.findByRole('heading', { name: 'Administrator access required' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Excel workbook')).not.toBeInTheDocument()
    expect(api.getImportHistory).not.toHaveBeenCalled()
    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(within(nav).getAllByRole('link').map((l) => l.textContent.trim())).toEqual(['Dashboard', 'Compare Photos', 'Reviews', 'Reports'])
    expect(within(nav).queryByRole('group', { name: 'Administration menu' })).toBeNull()
  })

  // Running an analysis as a signed-in reviewer is covered in conditionAnalysis.test.jsx (the panel is no longer on a page).
  it('a signed-in reviewer cannot import or administer', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
    const { unmount } = renderAt(COMPARED)
    await screen.findByLabelText('Signed-in user')
    unmount()
    const page = renderAt('/imports')
    expect(await screen.findByRole('heading', { name: 'Administrator access required' })).toBeInTheDocument()
    expect(within(screen.getByRole('navigation', { name: 'Main navigation' })).queryByRole('link', { name: /Excel Import|Administration|Settings/ })).toBeNull()
    page.unmount()
    renderAt('/admin')
    expect(await screen.findByRole('heading', { name: 'Administrator access required' })).toBeInTheDocument()
    expect(api.listUsers).not.toHaveBeenCalled()
  })

  it('an administrator sees the import upload panel', async () => {
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.adminUser })
    renderAt('/imports')
    expect(await screen.findByLabelText('Excel workbook')).toBeInTheDocument()
    const admin = within(screen.getByRole('navigation', { name: 'Main navigation' })).getByRole('group', { name: 'Administration menu' })
    expect(within(admin).getAllByRole('link').map((l) => l.textContent.trim())).toEqual(['Excel Import', 'Administration', 'Settings'])
  })
})

describe('Administration page', () => {
  beforeEach(() => api.getSession.mockResolvedValue({ authenticated: true, user: fx.adminUser }))

  it('lists accounts without exposing secrets and protects the signed-in admin', async () => {
    renderAt('/admin')
    const table = await screen.findByRole('table', { name: 'User accounts' })
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(rows[1]).toHaveTextContent('Chief Officer')
    expect(rows[1]).toHaveTextContent('you')
    expect(within(rows[1]).getByRole('button', { name: 'Disable' })).toBeDisabled()
    expect(within(rows[1]).getByRole('combobox', { name: 'Role for chief' })).toBeDisabled()   // own role: fixed
    expect(within(rows[2]).getByRole('combobox', { name: /^Role for / })).toBeEnabled()
    expect(within(rows[2]).getAllByRole('option').map((o) => o.textContent)).toEqual(['Viewer', 'Reviewer', 'Administrator'])
    expect(within(rows[2]).getByRole('button', { name: 'Disable' })).toBeEnabled()
    expect(await screen.findByRole('status', { name: 'Audit integrity' })).toHaveTextContent('Verified: 12 entries, chain intact.')
    expect(screen.getByLabelText('Recent audit entries')).toHaveTextContent('login succeeded')
  })

  it('creates an account and reports server validation errors', async () => {
    api.createUser.mockRejectedValueOnce(new api.ApiError('weak_password', 'The password must be at least 12 characters long.', 400))
    api.createUser.mockResolvedValueOnce({ ...fx.reviewerUser, username: 'new.user' })
    renderAt('/admin')
    const form = await screen.findByRole('form', { name: 'Create account' })
    fireEvent.change(within(form).getByLabelText('Username'), { target: { value: 'new.user' } })
    fireEvent.change(within(form).getByLabelText(/Initial password/), { target: { value: 'short' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Create account' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('at least 12 characters')
    fireEvent.change(within(form).getByLabelText(/Initial password/), { target: { value: 'a much longer password' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Create account' }))
    expect(await within(form).findByRole('status')).toHaveTextContent("Account 'new.user' created.")
    expect(api.createUser).toHaveBeenLastCalledWith({ username: 'new.user', display_name: null, role: 'reviewer', password: 'a much longer password' })
    await waitFor(() => expect(api.listUsers).toHaveBeenCalledTimes(2))
  })

  it('disables an account after confirmation and shows last-admin refusals', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.updateUser.mockResolvedValueOnce({})
    api.updateUser.mockRejectedValueOnce(new api.ApiError('last_admin', 'This is the last active administrator.', 409))
    renderAt('/admin')
    const table = await screen.findByRole('table', { name: 'User accounts' })
    const reviewerRow = within(table).getAllByRole('row')[2]
    fireEvent.click(within(reviewerRow).getByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(api.updateUser).toHaveBeenCalledWith('USR-000000000001', { active: false }))
    expect(confirm).toHaveBeenCalled()
    fireEvent.change(within(reviewerRow).getByRole('combobox', { name: /^Role for / }), { target: { value: 'admin' } })
    expect(await screen.findByRole('alert')).toHaveTextContent('This is the last active administrator.')
    confirm.mockRestore()
  })
})
