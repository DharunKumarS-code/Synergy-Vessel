import { afterEach, describe, expect, it, vi } from 'vitest'
import { AI_ERROR_MESSAGES, ApiError, getAllInspections, http, imageUrl, toAiError, toApiError } from '../services/api'

describe('toApiError', () => {
  it('maps a network failure to a friendly offline error', () => {
    const e = toApiError({ code: 'ERR_NETWORK', message: 'Network Error' })
    expect(e).toBeInstanceOf(ApiError)
    expect(e.kind).toBe('offline')
    expect(e.message).not.toMatch(/Network Error/)
  })

  it('never exposes backend detail text', () => {
    const e = toApiError({ response: { status: 500, data: { detail: 'Traceback: secret path E:\\x' } } })
    expect(e.kind).toBe('server')
    expect(e.message).not.toMatch(/Traceback|E:\\/)
  })

  it.each([
    [404, 'not_found'],
    [422, 'bad_request'],
    [400, 'bad_request'],
    [503, 'offline'],
    [418, 'unknown'],
    [429, 'too_many_requests'],
  ])('status %i -> %s', (status, kind) => {
    expect(toApiError({ response: { status } }).kind).toBe(kind)
  })

  it('rate-limited requests and a paused AI service get friendly messages (infrastructure, 2026-11-23)', () => {
    expect(toApiError({ response: { status: 429, data: { detail: 'raw' } } }).message).toBe('Too many requests. Please wait a moment and try again.')
    const open = toAiError({ response: { status: 503, data: { code: 'ai_circuit_open', detail: 'raw text' } } })
    expect(open.kind).toBe('ai_circuit_open')
    expect(open.message).toBe(AI_ERROR_MESSAGES.ai_circuit_open)
    expect(open.message).toMatch(/No result was saved/)
    expect(toAiError({ response: { status: 429, data: { code: 'too_many_requests' } } }).message).toBe(AI_ERROR_MESSAGES.too_many_requests)
  })

  it('recognises timeouts and cancellation', () => {
    expect(toApiError({ code: 'ECONNABORTED' }).kind).toBe('timeout')
    expect(toApiError({ code: 'ERR_CANCELED' }).kind).toBe('cancelled')
  })
})

describe('api calls', () => {
  afterEach(() => vi.restoreAllMocks())

  it('getAllInspections pages until total is reached and passes the vessel', async () => {
    const spy = vi.spyOn(http, 'get').mockImplementation(async (_url, { params }) => ({
      data: params.offset === 0
        ? { total: 3, count: 2, offset: 0, limit: 2000, items: [{ record_id: 'a' }, { record_id: 'b' }] }
        : { total: 3, count: 1, offset: params.offset, limit: 2000, items: [{ record_id: 'c' }] },
    }))
    const res = await getAllInspections({ vessel: 'v1' })
    expect(res.items.map((i) => i.record_id)).toEqual(['a', 'b', 'c'])
    expect(spy.mock.calls[0][0]).toBe('/api/inspections')
    expect(spy.mock.calls[0][1].params).toMatchObject({ vessel: 'v1', limit: 2000, offset: 0 })
  })

  it('wraps axios errors in ApiError', async () => {
    vi.spyOn(http, 'get').mockRejectedValue({ response: { status: 404, data: { detail: 'x' } } })
    await expect(getAllInspections()).rejects.toMatchObject({ name: 'ApiError', kind: 'not_found' })
  })

  it('builds encoded image URLs', () => {
    expect(imageUrl('W30-0001')).toBe('/api/images/W30-0001')
    expect(imageUrl('../x')).toBe('/api/images/..%2Fx')
  })
})

describe('SV-03 review refusals', () => {
  it.each([
    ['review_already_decided', 409, 'Only an administrator can correct a decision'],
    ['invalid_review_transition', 409, 'cannot be returned to Awaiting review'],
    ['review_unchanged', 409, 'already has that review status'],
    ['correction_reason_required', 422, 'at least 10 characters'],
    ['self_approval_not_allowed', 403, 'you analysed yourself'],
  ])('%s (%i) becomes a readable message', async (code, status, text) => {
    const { toAiError } = await import('../services/api')
    const e = toAiError({ response: { status, data: { code, detail: 'server text' } } })
    expect(e.status).toBe(status)
    expect(e.message).toContain(text)
  })
})
