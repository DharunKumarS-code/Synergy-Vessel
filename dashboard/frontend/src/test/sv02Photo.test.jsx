import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../services/api'
import Photo from '../components/search/Photo'

/** SV-02 -- photos are authorised by the HttpOnly cookie; a failed load re-checks the session once. */
vi.mock('../services/api', async (orig) => ({ ...(await orig()), recheckPhotoAccess: vi.fn() }))

const item = { image_id: 'W30-0001', image_status: 'resolved' }

beforeEach(() => api.recheckPhotoAccess.mockReset())

describe('Photo (SV-02)', () => {
  it('loads through a plain <img> with no token in the URL', () => {
    render(<Photo item={item} />)
    const img = screen.getByAltText('Inspection photograph W30-0001')
    expect(img).toHaveAttribute('src', '/api/images/W30-0001')
    expect(img.getAttribute('src')).not.toMatch(/[?&]|token/)
    fireEvent.load(img)
    expect(img.className).toMatch(/opacity-100/)
  })

  it('after a failure with a live session (cookie renewed) it retries once with the same URL', async () => {
    api.recheckPhotoAccess.mockResolvedValue(true)
    render(<Photo item={item} />)
    const first = screen.getByAltText('Inspection photograph W30-0001')
    fireEvent.error(first)
    await waitFor(() => expect(screen.getByAltText('Inspection photograph W30-0001')).not.toBe(first))   // new element
    const second = screen.getByAltText('Inspection photograph W30-0001')
    expect(second).toHaveAttribute('src', '/api/images/W30-0001')
    expect(screen.queryByRole('img', { name: 'Photograph could not be loaded' })).toBeNull()
    fireEvent.error(second)                                                         // a second failure: give up
    expect(await screen.findByRole('img', { name: 'Photograph could not be loaded' })).toBeInTheDocument()
    expect(api.recheckPhotoAccess).toHaveBeenCalledTimes(1)
  })

  it('after a failure with an ended session it shows the safe fallback (the app returns to sign-in)', async () => {
    api.recheckPhotoAccess.mockResolvedValue(false)
    render(<Photo item={item} />)
    fireEvent.error(screen.getByAltText('Inspection photograph W30-0001'))
    expect(await screen.findByRole('img', { name: 'Photograph could not be loaded' })).toBeInTheDocument()
  })

  it('a missing photograph never requests anything', () => {
    render(<Photo item={{ image_id: 'W30-0002', image_status: 'missing' }} />)
    expect(screen.getByRole('img', { name: 'Photograph unavailable' })).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
  })
})
