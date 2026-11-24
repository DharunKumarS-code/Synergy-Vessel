import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  SESSION_EXPIRED_EVENT, downloadReport, getSession, getVessels, http, imageUrl, logout, recheckPhotoAccess, toApiError, tokenStore,
} from '../services/api'

/** SV-02 -- the API layer: 401/403 handling, session expiry, the photo cookie re-check, tokens on downloads. */

let calls
let respond
const original = http.defaults.adapter

beforeEach(() => {
  calls = []
  respond = () => ({ status: 200, data: {} })
  http.defaults.adapter = async (config) => {
    calls.push(config)
    const { status, data, headers = {} } = respond(config)
    if (status >= 400) {
      const err = new Error(`HTTP ${status}`)
      err.response = { status, data, headers, config }
      err.config = config
      throw err
    }
    return { status, statusText: 'OK', data, headers, config }
  }
  tokenStore.set(null)
  vi.useFakeTimers({ toFake: ['setTimeout'] })
})

afterEach(() => {
  vi.advanceTimersByTime(10_000)   // let the shared photo re-check expire between tests
  vi.useRealTimers()
  http.defaults.adapter = original
  tokenStore.set(null)
})

const auth = (config) => config.headers?.Authorization ?? config.headers?.get?.('Authorization')

describe('error mapping', () => {
  it('a 401 is a session that ended, never "Something went wrong"', () => {
    const e = toApiError({ response: { status: 401, data: { code: 'not_authenticated', detail: 'raw' } } })
    expect(e.kind).toBe('unauthenticated')
    expect(e.message).toBe('Your session has ended. Please sign in again.')
    expect(e.message).not.toMatch(/Something went wrong/)
  })

  it('a 403 says the account lacks permission', () => {
    const e = toApiError({ response: { status: 403, data: { code: 'forbidden' } } })
    expect(e.kind).toBe('forbidden')
    expect(e.message).toBe('Your account does not have permission for this.')
  })
})

describe('session expiry', () => {
  it('a 401 on a data request clears the token and signals the expiry', async () => {
    tokenStore.set('tok-1')
    respond = () => ({ status: 401, data: { code: 'not_authenticated', detail: 'x' } })
    const seen = vi.fn()
    window.addEventListener(SESSION_EXPIRED_EVENT, seen)
    await expect(getVessels()).rejects.toMatchObject({ kind: 'unauthenticated' })
    window.removeEventListener(SESSION_EXPIRED_EVENT, seen)
    expect(seen).toHaveBeenCalledTimes(1)
    expect(tokenStore.get()).toBeNull()
  })

  it('also signals when no token was stored (a request that raced a sign-out)', async () => {
    respond = () => ({ status: 401, data: { code: 'not_authenticated' } })
    const seen = vi.fn()
    window.addEventListener(SESSION_EXPIRED_EVENT, seen)
    await expect(getVessels()).rejects.toBeTruthy()
    window.removeEventListener(SESSION_EXPIRED_EVENT, seen)
    expect(seen).toHaveBeenCalledTimes(1)
  })

  it('a 403 does not end the session', async () => {
    tokenStore.set('tok-2')
    respond = () => ({ status: 403, data: { code: 'forbidden' } })
    const seen = vi.fn()
    window.addEventListener(SESSION_EXPIRED_EVENT, seen)
    await expect(getVessels()).rejects.toMatchObject({ kind: 'forbidden' })
    window.removeEventListener(SESSION_EXPIRED_EVENT, seen)
    expect(seen).not.toHaveBeenCalled()
    expect(tokenStore.get()).toBe('tok-2')
  })

  it('sign-out always reaches the server (it clears the HttpOnly photo cookie), even without a token', async () => {
    respond = () => ({ status: 204, data: '' })
    await logout()
    expect(calls.map((c) => [c.method, c.url])).toEqual([['post', '/api/auth/logout']])
    respond = () => { throw new Error('offline') }
    tokenStore.set('tok-3')
    await expect(logout()).resolves.toBeUndefined()
    expect(tokenStore.get()).toBeNull()
  })
})

describe('authenticated requests', () => {
  it('data requests and report downloads carry the bearer token in the header only', async () => {
    tokenStore.set('secret-token-4')
    respond = (c) => (c.responseType === 'blob'
      ? { status: 200, data: new Blob(['a,b']), headers: { 'content-disposition': 'attachment; filename="r.csv"' } }
      : { status: 200, data: [] })
    await getVessels()
    for (const kind of ['inspectionsCsv', 'analysesCsv', 'pdf']) {
      expect((await downloadReport(kind)).filename).toBe('r.csv')
    }
    expect(calls.map((c) => c.url)).toEqual(['/api/vessels', '/api/reports/inspections.csv', '/api/reports/analyses.csv',
      '/api/reports/inspection.pdf'])
    for (const c of calls) {
      expect(auth(c)).toBe('Bearer secret-token-4')
      expect(JSON.stringify(c.params ?? {})).not.toContain('secret-token-4')
      expect(c.url).not.toContain('secret-token-4')
    }
  })

  it('photo URLs never contain a token or any query parameter', () => {
    tokenStore.set('secret-token-5')
    const url = imageUrl('W30-0001')
    expect(url).toBe('/api/images/W30-0001')
    expect(url).not.toMatch(/[?&#]|token|secret/)
  })

  it('the session check is skipped without a token', async () => {
    expect(await getSession()).toEqual({ authenticated: false, user: null })
    expect(calls).toEqual([])
  })
})

describe('photo access re-check', () => {
  it('without a session in this tab it reports failure without signalling an expiry', async () => {
    const seen = vi.fn()
    window.addEventListener(SESSION_EXPIRED_EVENT, seen)
    expect(await recheckPhotoAccess()).toBe(false)
    window.removeEventListener(SESSION_EXPIRED_EVENT, seen)
    expect(seen).not.toHaveBeenCalled()
    expect(calls).toEqual([])
  })

  it('a live session renews the cookie (via the session check) and allows one retry; concurrent failures share it', async () => {
    tokenStore.set('tok-6')
    respond = () => ({ status: 200, data: { authenticated: true, user: { role: 'viewer' } } })
    const [a, b, c] = await Promise.all([recheckPhotoAccess(), recheckPhotoAccess(), recheckPhotoAccess()])
    expect([a, b, c]).toEqual([true, true, true])
    expect(calls.map((x) => x.url)).toEqual(['/api/auth/session'])
  })

  it('an ended session signals the expiry so the app returns to sign-in', async () => {
    tokenStore.set('tok-7')
    respond = () => ({ status: 200, data: { authenticated: false, user: null } })
    const seen = vi.fn()
    window.addEventListener(SESSION_EXPIRED_EVENT, seen)
    expect(await recheckPhotoAccess()).toBe(false)
    window.removeEventListener(SESSION_EXPIRED_EVENT, seen)
    expect(seen).toHaveBeenCalledTimes(1)
    expect(tokenStore.get()).toBeNull()
  })
})
