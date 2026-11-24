/* Post-audit Phase 4C: minor UI clarity fixes (review wording, filename display, Condition Trends and Settings
   placeholders). Mocked API responses and synthetic records only. */
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import FileName from '../components/ui/FileName'
import * as api from '../services/api'
import { formatFilename } from '../utils/format'
import * as fx from './fixtures'

vi.mock('../services/api', async (orig) => {
  const actual = await orig()
  return {
    ...actual,
    getHealth: vi.fn(), getVessels: vi.fn(), getLocations: vi.fn(), getAllInspections: vi.fn(), searchInspections: vi.fn(),
    getSession: vi.fn(), getAllAnalyses: vi.fn(),
  }
})

const renderAt = (path) =>
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </MemoryRouter>,
  )

// Shaped like the real data: the register file names contain a double space; worksheet names a trailing space.
const ODD_WORKBOOK = 'Nord Kudu  Deck Photos - SEPT 13  2026 Part 1 (1).xlsx'
const LONG = `${'Very_long_inspection_workbook_name_without_spaces_'.repeat(4)}.xlsx`

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: fx.inspections.total, items: fx.inspections.items })
  api.getSession.mockResolvedValue({ authenticated: true, user: fx.adminUser })
  api.getAllAnalyses.mockResolvedValue({ total: 0, items: [] })
})

describe('filename display', () => {
  it('formatFilename tidies whitespace for display only', () => {
    expect(formatFilename('Week_34_Image_Register.xlsx')).toBe('Week_34_Image_Register.xlsx')               // normal
    expect(formatFilename(ODD_WORKBOOK)).toBe('Nord Kudu Deck Photos - SEPT 13 2026 Part 1 (1).xlsx')       // repeated spaces
    expect(formatFilename('  AUG 02 2026 \t')).toBe('AUG 02 2026')                                          // leading/trailing
    expect(formatFilename(LONG)).toBe(LONG)                                                                  // long: content kept
    for (const blank of ['', '   ', null, undefined, 42]) expect(formatFilename(blank)).toBeNull()
  })

  it('FileName shows the tidy name, keeps the exact original, and wraps long names', () => {
    const original = `  ${ODD_WORKBOOK} `
    const { container } = render(<><FileName name={original} /><FileName name={LONG} /><FileName name="   " /></>)
    const [odd, long, blank] = container.querySelectorAll('span')
    expect(odd.textContent).toBe('Nord Kudu Deck Photos - SEPT 13 2026 Part 1 (1).xlsx')
    expect(odd).toHaveAttribute('title', original)                                    // exact stored value, untouched
    expect(odd.dataset.originalName).toBe(original)
    expect(long.className).toContain('[overflow-wrap:anywhere]')                     // breaks instead of overflowing
    expect(long.textContent).toBe(LONG)
    expect(blank.textContent).toBe('—')
    expect(original).toBe(`  ${ODD_WORKBOOK} `)                                       // the value itself is unchanged
  })

  it('the photograph viewer shows workbook and worksheet names tidily', async () => {
    const item = { ...fx.searchResult.items[1], original_excel_filename: ODD_WORKBOOK, worksheet_name: 'SEPT 13  2026 ' }
    api.searchInspections.mockResolvedValue({ ...fx.searchResult, items: [item] })
    renderAt(`/search?location=main%20deck&image=${item.record_id}`)
    const dialog = await screen.findByRole('dialog')
    // (Testing Library's title matcher collapses whitespace itself, so read the attribute directly)
    const workbook = [...dialog.querySelectorAll('[data-original-name]')].find((el) => el.getAttribute('title') === ODD_WORKBOOK)
    expect(workbook).toBeDefined()
    expect(workbook).toHaveTextContent(/^Nord Kudu Deck Photos - SEPT 13 2026 Part 1 \(1\)\.xlsx$/)
    expect(within(dialog).getByText('SEPT 13 2026')).toBeInTheDocument()
    expect(item.original_excel_filename).toBe(ODD_WORKBOOK)                           // record data not modified
  })
})

// The Location Review page was removed from the UI (2026-11-23); see locationGemini.test.jsx.
// The Condition Trends placeholder card was replaced by real figures; see conditionTrends.test.jsx.
// The Settings placeholder was replaced by the read-only Settings page; see settings.test.jsx.
