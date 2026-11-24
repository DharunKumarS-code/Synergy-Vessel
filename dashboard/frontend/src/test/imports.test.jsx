import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
    previewImport: vi.fn(),
    confirmImport: vi.fn(),
    getImportHistory: vi.fn(),
    getImport: vi.fn(),
    getSession: vi.fn(),
    withdrawImport: vi.fn(),
    reinstateImport: vi.fn(),
    verifyImport: vi.fn(),
    decideLocation: vi.fn(),
  }
})

const renderAt = (path = '/imports') =>
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </MemoryRouter>,
  )

const row = (n, status, extra = {}) => ({
  row_number: n, serial_number: n - 1, image_filename: `W43-${String(n - 1).padStart(4, '0')}.jpg`,
  inspection_date: '2026-10-25', date_source: 'report_date', official_location: 'CARGO HOLD NO. 1',
  status, errors: [], warnings: [], notes: [], record_id: null, ...extra,
})

const PREVIEW = {
  filename: 'Week_43_Image_Register.xlsx',
  fingerprint: 'a'.repeat(64),
  size_bytes: 1000,
  can_import: true,
  file_warnings: ['1 embedded picture(s) are damaged and could not be read.'],
  summary: { total_rows: 5, records: 4, ready: 2, duplicates: 1, blank_rows: 1, skipped: 2, failed: 1, repeated_appearances: 1, unique_photographs: 2, warnings: 0 },
  rows: [
    row(2, 'ready', { notes: ['Repeated appearance: this photograph is also used by 3 existing record(s) (e.g. W31-0005). It is kept as a separate appearance.'] }),
    row(3, 'ready', { official_location: null, date_source: 'worksheet_name' }),
    row(4, 'duplicate', { notes: ['Already in the dataset as record W42-0003 (same workbook, worksheet, date, serial and photograph).'] }),
    row(5, 'invalid', { errors: ['No readable photograph is embedded in this row (missing image).'] }),
    row(6, 'skipped', { notes: ['Blank row.'] }),
  ],
}
const IMPORT = {
  import_id: 'IMP-20261118-0123456789', created_at: '2026-11-18T12:00:00+00:00', filename: 'Week_43_Image_Register.xlsx',
  status: 'completed', summary: { ...PREVIEW.summary, imported: 2 }, file_warnings: [],
}
const EMPTY_HISTORY = { imports: [], failed_attempts: [], interrupted_imports: 0, problems: [] }
const xlsx = (name = 'Week_43_Image_Register.xlsx') => new File(['PK'], name, { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
const choose = (file) => fireEvent.change(screen.getByLabelText('Excel workbook'), { target: { files: [file] } })

beforeEach(() => {
  vi.clearAllMocks()
  api.getHealth.mockResolvedValue(fx.health)
  api.getVessels.mockResolvedValue(fx.vessels)
  api.getLocations.mockResolvedValue(fx.locations)
  api.getAllInspections.mockResolvedValue({ total: 4, items: fx.inspections.items })
  api.getImportHistory.mockResolvedValue(EMPTY_HISTORY)
  api.getSession.mockResolvedValue({ authenticated: true, user: fx.adminUser })
})

describe('Excel import page', () => {
  it('is reachable from the sidebar and shows an empty history', async () => {
    renderAt('/')
    fireEvent.click(await screen.findByRole('link', { name: /Excel Import/ }))
    expect(await screen.findByRole('heading', { name: 'Import an Image Register' })).toBeInTheDocument()
    expect(await screen.findByText('No imports yet')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Preview import' })).toBeDisabled()
  })

  it('rejects non-xlsx files in the browser without uploading', async () => {
    renderAt()
    await screen.findByText('No imports yet')
    choose(new File(['a,b'], 'register.csv', { type: 'text/csv' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Only Excel workbooks (.xlsx)')
    expect(screen.getByRole('button', { name: 'Preview import' })).toBeDisabled()
    expect(api.previewImport).not.toHaveBeenCalled()
  })

  it('previews row results, duplicates and failures, then imports after confirmation', async () => {
    api.previewImport.mockResolvedValue(PREVIEW)
    let finish
    api.confirmImport.mockReturnValue(new Promise((r) => (finish = r)))
    renderAt()
    await screen.findByText('No imports yet')
    const file = xlsx()
    choose(file)
    fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
    const preview = await screen.findByLabelText('Import preview')
    expect(api.previewImport).toHaveBeenCalledWith(file)
    expect(api.confirmImport).not.toHaveBeenCalled() // preview never imports
    const summary = within(preview).getByLabelText('Import summary')
    expect(within(summary).getByText('Ready to import').nextSibling).toHaveTextContent('2')
    expect(within(summary).getByText('Duplicates').nextSibling).toHaveTextContent('1')
    expect(within(summary).getByText('Failed').nextSibling).toHaveTextContent('1')
    expect(within(preview).getByText(/legitimate repeated/)).toBeInTheDocument()
    expect(within(preview).getByText(/damaged and could not be read/)).toBeInTheDocument()
    expect(within(preview).getByText(/Already in the dataset as record W42-0003/)).toBeInTheDocument()
    expect(within(preview).getByText(/missing image/)).toBeInTheDocument()
    expect(within(preview).getByText('Not labelled')).toBeInTheDocument()
    expect(within(preview).getByText('from worksheet name')).toBeInTheDocument()

    fireEvent.click(within(preview).getByRole('tab', { name: /Failed/ }))
    expect(within(preview).getAllByRole('row')).toHaveLength(2) // header + 1 failed row

    api.getImportHistory.mockResolvedValue({ ...EMPTY_HISTORY, imports: [IMPORT] })
    fireEvent.click(within(preview).getByRole('button', { name: 'Import 2 records' }))
    expect(screen.getByRole('status', { name: 'Importing records' })).toBeInTheDocument()
    expect(api.confirmImport).toHaveBeenCalledWith(file, 'a'.repeat(64))
    finish({ ...IMPORT, rows: PREVIEW.rows })
    const done = await screen.findByRole('status', { name: 'Import complete' })
    expect(done).toHaveTextContent('IMP-20261118-0123456789 completed: 2 record(s) imported')
    const history = await screen.findByRole('table', { name: 'Import history' })
    expect(within(history).getByText('Week_43_Image_Register.xlsx')).toBeInTheDocument()
  })

  it('shows server validation errors with recovery guidance', async () => {
    api.previewImport.mockRejectedValue(Object.assign(
      new api.ApiError('missing_columns', 'Required column(s) missing: Report Date.', 400),
      { guidance: 'The header row must contain: Serial Number, Image Filename, Report Date.' },
    ))
    renderAt()
    await screen.findByText('No imports yet')
    choose(xlsx())
    fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Required column(s) missing: Report Date.')
    expect(alert).toHaveTextContent('The header row must contain')
    expect(screen.queryByLabelText('Import preview')).not.toBeInTheDocument()
  })

  it('keeps the preview and explains a failed commit', async () => {
    api.previewImport.mockResolvedValue(PREVIEW)
    api.confirmImport.mockRejectedValue(Object.assign(
      new api.ApiError('import_failed', 'The import could not be completed. Nothing was imported.', 500),
      { guidance: 'Check free disk space and folder permissions, then try again.' },
    ))
    renderAt()
    await screen.findByText('No imports yet')
    choose(xlsx())
    fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Import 2 records' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was imported')
    expect(screen.getByLabelText('Import preview')).toBeInTheDocument()
    expect(screen.queryByRole('status', { name: 'Import complete' })).not.toBeInTheDocument()
  })

  it('explains how a Deck Photos workbook is read (Phase 8.5)', async () => {
    api.previewImport.mockResolvedValue({ ...PREVIEW, source_format: 'deck_photos' })
    renderAt()
    await screen.findByText('No imports yet')
    choose(xlsx())
    fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
    expect(await screen.findByRole('note', { name: 'Detected format' })).toHaveTextContent('Deck Photos workbook')
  })

  it('does not label an Image Register as Deck Photos', async () => {
    api.previewImport.mockResolvedValue({ ...PREVIEW, source_format: 'image_register' })
    renderAt()
    await screen.findByText('No imports yet')
    choose(xlsx())
    fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
    await screen.findByText(/Preview ·/)
    expect(screen.queryByRole('note', { name: 'Detected format' })).not.toBeInTheDocument()
  })

  it('disables confirmation when nothing new can be imported', async () => {
    api.previewImport.mockResolvedValue({ ...PREVIEW, can_import: false, summary: { ...PREVIEW.summary, ready: 0 } })
    renderAt()
    await screen.findByText('No imports yet')
    choose(xlsx())
    fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
    expect(await screen.findByText('There are no new valid records to import.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Import 0 records' })).toBeDisabled()
  })

  it('lists history with failed attempts and opens import details', async () => {
    api.getImportHistory.mockResolvedValue({
      imports: [IMPORT],
      failed_attempts: [{ attempt_id: 'ATT-1', created_at: '2026-11-17T09:00:00+00:00', filename: 'old.xlsx', status: 'failed', code: 'nothing_to_import', message: 'No new valid records.', summary: null }],
      interrupted_imports: 1,
      problems: [],
    })
    api.getImport.mockResolvedValue({ ...IMPORT, rows: PREVIEW.rows.map((r) => ({ ...r, status: r.status === 'ready' ? 'imported' : r.status })) })
    renderAt()
    const table = await screen.findByRole('table', { name: 'Import history' })
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(rows[1]).toHaveTextContent('Week_43_Image_Register.xlsx')
    expect(rows[1]).toHaveTextContent('Imported')
    expect(rows[2]).toHaveTextContent('old.xlsx')
    expect(rows[2]).toHaveTextContent('Not imported')
    expect(screen.getByRole('note')).toHaveTextContent('1 interrupted import(s)')
    fireEvent.click(within(table).getByRole('button', { name: 'Week_43_Image_Register.xlsx' }))
    const detail = await screen.findByRole('region', { name: 'Import details' })
    expect(api.getImport).toHaveBeenCalledWith('IMP-20261118-0123456789', expect.anything())
    await waitFor(() => expect(within(detail).getAllByText('Imported').length).toBeGreaterThan(1))
  })
})

describe('Import rollback (Phase 8.4)', () => {
  const DETAIL = { ...IMPORT, status: 'completed', state_events: [], rows: PREVIEW.rows.map((r) => ({ ...r, status: r.status === 'ready' ? 'imported' : r.status })) }

  it('lets an administrator verify and withdraw an import after typing its ID', async () => {
    api.getImportHistory.mockResolvedValue({ ...EMPTY_HISTORY, imports: [IMPORT] })
    api.getImport.mockResolvedValue(DETAIL)
    api.verifyImport.mockResolvedValue({ valid: true, photographs_checked: 2, problems: [] })
    api.withdrawImport.mockResolvedValue({ ...IMPORT, status: 'withdrawn', event: { affected_analyses: ['CA-1'] } })
    renderAt('/imports?id=IMP-20261118-0123456789')
    const panel = await screen.findByRole('region', { name: 'Import administration' })
    fireEvent.click(within(panel).getByRole('button', { name: 'Check file integrity' }))
    expect(await within(panel).findByRole('status', { name: 'Integrity result' })).toHaveTextContent('Intact: workbook and 2 photograph(s)')
    const button = within(panel).getByRole('button', { name: 'Withdraw import' })
    fireEvent.change(within(panel).getByLabelText('Reason for withdrawing'), { target: { value: 'wrong vessel' } })
    expect(button).toBeDisabled() // confirmation still missing
    fireEvent.change(within(panel).getByLabelText('Type the import ID to confirm'), { target: { value: 'IMP-20261118-0123456789' } })
    fireEvent.click(button)
    expect(await within(panel).findByText(/Import withdrawn; its records left the dataset/)).toHaveTextContent('1 saved analysis(es)')
    expect(api.withdrawImport).toHaveBeenCalledWith('IMP-20261118-0123456789', 'wrong vessel')
  })

  it('shows withdrawn imports in the history; the import page is limited to administrators', async () => {
    api.getImportHistory.mockResolvedValue({ ...EMPTY_HISTORY, imports: [{ ...IMPORT, status: 'withdrawn' }] })
    api.getImport.mockResolvedValue({ ...DETAIL, status: 'withdrawn' })
    const { unmount } = renderAt('/imports?id=IMP-20261118-0123456789')
    const table = await screen.findByRole('table', { name: 'Import history' })
    expect(within(table).getAllByRole('row')[1]).toHaveTextContent('Withdrawn')
    unmount()
    api.getImportHistory.mockClear()
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
    renderAt('/imports?id=IMP-20261118-0123456789')
    expect(await screen.findByRole('heading', { name: 'Administrator access required' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Import administration' })).not.toBeInTheDocument()
    expect(api.getImportHistory).not.toHaveBeenCalled()
  })
})

describe('Excel Location Intelligence (Phase 9)', () => {
  const li = (extra) => ({
    original_location: "Foc'sle", normalized_location: 'FORECASTLE DECK AREA', rule_method: 'synonym', rule_confidence: 0.95,
    candidates: [], explanation: '', ai_status: 'not_needed', ai_suggestion: null, ai_confidence: null, ai_reason: null,
    normalization_method: 'synonym', confidence: 0.95, confidence_band: 'high', review_required: false,
    review_status: 'auto_applied', applied_location: 'FORECASTLE DECK AREA', ...extra,
  })
  // Phase 9.4 shape: rule confidence and (capped) AI confidence are separate; ambiguous rows are never sent to the AI
  const AI_ROW = li({ original_location: 'Hold 3 hatch', normalized_location: null, rule_method: 'unknown', rule_confidence: 0,
    normalization_method: 'ai_suggestion', ai_status: 'ok', ai_suggestion: 'CARGO HOLD NO. 3', ai_confidence: 0.72,
    ai_confidence_band: 'medium', ai_agreement: 'no_rule_candidate', confidence: 0, confidence_band: 'low', rule_candidate: null,
    review_required: true, review_status: 'pending', applied_location: 'Hold 3 hatch', candidates: [],
    review_reason: 'The rules found no matching location. AI suggestion only; requires review.' })
  const UNKNOWN = li({ original_location: 'Forward section', normalized_location: null, rule_method: 'unknown', normalization_method: 'unknown',
    confidence: 0, confidence_band: 'low', review_required: true, review_status: 'pending', applied_location: 'Forward section' })
  const LOC_PREVIEW = {
    ...PREVIEW,
    location_intelligence: { counts: { auto_applied: 1, not_required: 0, ai_suggestions: 1, review_required: 2, missing: 0, unknown: 1 },
      ai: { enabled: true, provider: 'mock', simulated: true, used: 'ok', problems: [] } },
    rows: [
      row(2, 'ready', { official_location: "Foc'sle", location_intel: li() }),
      row(3, 'ready', { official_location: 'Hold 3 hatch', location_intel: AI_ROW }),
      row(4, 'ready', { official_location: 'Forward section', location_intel: UNKNOWN }),
    ],
  }

  it('shows original, standardised location, method, AI suggestion, confidence and review status in the preview', async () => {
    api.previewImport.mockResolvedValue(LOC_PREVIEW)
    renderAt()
    await screen.findByText('No imports yet')
    choose(xlsx())
    fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
    expect(await screen.findByRole('note', { name: 'Location intelligence' })).toHaveTextContent('1 standardised automatically')
    expect(screen.getByRole('note', { name: 'Location intelligence' })).toHaveTextContent('simulated (mock)')
    const foc = document.querySelector('tr[data-row="2"]')
    expect(foc).toHaveTextContent("Foc'sle")
    expect(within(foc).getByLabelText('Location used')).toHaveTextContent('FORECASTLE DECK AREA')
    expect(foc).toHaveTextContent('Known synonym · 95%')
    expect(foc).toHaveTextContent('Applied automatically')
    const ai = document.querySelector('tr[data-row="3"]')
    expect(within(ai).getByLabelText('AI suggestion')).toHaveTextContent('AI Suggestion — Requires Review: CARGO HOLD NO. 3 (medium AI confidence)')
    expect(ai).not.toHaveTextContent('72%')                                         // raw AI confidence is never shown
    expect(ai).toHaveTextContent('Unknown location · 0%')                           // rule result kept separate
    expect(within(ai).getByLabelText('Review reason')).toHaveTextContent('AI suggestion only')
    expect(ai).toHaveTextContent('Needs review')
    expect(ai.querySelector('[data-location-method]').dataset.locationMethod).toBe('ai_suggestion')
    expect(document.querySelector('tr[data-row="4"]')).toHaveTextContent('Unknown location')
    // the preview offers no decision buttons: decisions are made after import
    expect(screen.queryByRole('group', { name: /Location decision/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Location review/ }))
    expect(document.querySelector('tr[data-row="2"]')).toBeNull()
    expect(document.querySelector('tr[data-row="3"]')).not.toBeNull()
  })

  it('lets an administrator accept a suggestion or enter a location after import', async () => {
    const detail = { ...IMPORT, state_events: [], location_decisions: {}, location_problems: [],
      rows: [row(3, 'imported', { official_location: 'Hold 3 hatch', location_intel: AI_ROW,
        effective_location: { location: 'Hold 3 hatch', method: 'ai_suggestion', review_status: 'pending' } }),
      row(4, 'imported', { official_location: 'Forward section', location_intel: UNKNOWN,
        effective_location: { location: 'Forward section', method: 'unknown', review_status: 'pending' } })] }
    api.getImportHistory.mockResolvedValue({ ...EMPTY_HISTORY, imports: [IMPORT] })
    api.getImport.mockResolvedValue(detail)
    api.decideLocation.mockResolvedValue({ effective_location: { location: 'CARGO HOLD NO. 3' } })
    renderAt('/imports?id=IMP-20261118-0123456789')
    const group = await screen.findByRole('group', { name: 'Location decision for row 3' })
    fireEvent.click(within(group).getByRole('button', { name: 'Accept' }))
    await waitFor(() => expect(api.decideLocation).toHaveBeenCalledWith('IMP-20261118-0123456789', 3, 'accept', undefined))
    const unknown = screen.getByRole('group', { name: 'Location decision for row 4' })
    expect(within(unknown).queryByRole('button', { name: 'Accept' })).not.toBeInTheDocument()   // nothing to accept
    fireEvent.click(within(unknown).getByRole('button', { name: 'Enter location' }))
    fireEvent.change(within(unknown).getByLabelText('Location for row 4'), { target: { value: 'FORECASTLE DECK AREA' } })
    fireEvent.click(within(unknown).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.decideLocation).toHaveBeenCalledWith('IMP-20261118-0123456789', 4, 'manual', 'FORECASTLE DECK AREA'))
    await waitFor(() => expect(api.getImport.mock.calls.length).toBeGreaterThanOrEqual(3))         // details refreshed
  })

  it('shows rule/AI disagreement and makes the reviewer choose which suggestion to accept', async () => {
    const DISAGREE = li({ original_location: 'Aft deck area', normalized_location: 'BOAT DECK AREA', rule_method: 'spelling',
      rule_confidence: 0.72, rule_candidate: 'BOAT DECK AREA', normalization_method: 'ai_suggestion', ai_status: 'ok',
      ai_suggestion: 'POOP DECK AND MOORING WINCH', ai_agreement: 'disagrees', confidence: 0.72, confidence_band: 'medium',
      // an older stored row: raw AI confidence 0.95 and no ai_confidence_band -- still shown as "medium", never 95%
      ai_confidence: 0.95, review_required: true, review_status: 'pending', applied_location: 'Aft deck area',
      review_reason: 'The AI suggestion differs from the rule candidate; choose one, keep the original or enter a location.' })
    const detail = { ...IMPORT, state_events: [], location_decisions: {}, location_problems: [],
      rows: [row(5, 'imported', { official_location: 'Aft deck area', location_intel: DISAGREE,
        effective_location: { location: 'Aft deck area', method: 'ai_suggestion', review_status: 'pending' } })] }
    api.getImportHistory.mockResolvedValue({ ...EMPTY_HISTORY, imports: [IMPORT] })
    api.getImport.mockResolvedValue(detail)
    api.decideLocation.mockResolvedValue({ effective_location: { location: 'BOAT DECK AREA' } })
    renderAt('/imports?id=IMP-20261118-0123456789')
    const group = await screen.findByRole('group', { name: 'Location decision for row 5' })
    const cell = document.querySelector('tr[data-row="5"]')
    expect(within(cell).getByLabelText('Rule candidate')).toHaveTextContent('Rule candidate: BOAT DECK AREA')
    expect(within(cell).getByLabelText('AI suggestion')).toHaveTextContent('POOP DECK AND MOORING WINCH (medium AI confidence)')
    expect(within(cell).getByLabelText('Disagreement')).toHaveTextContent('AI disagrees with the rule candidate')
    expect(cell).toHaveTextContent('Spelling variant · 72%')
    expect(cell).not.toHaveTextContent('95%')
    expect(within(group).queryByRole('button', { name: 'Accept' })).not.toBeInTheDocument()       // no silent default
    fireEvent.click(within(group).getByRole('button', { name: 'Accept rule candidate' }))
    await waitFor(() => expect(api.decideLocation).toHaveBeenCalledWith('IMP-20261118-0123456789', 5, 'accept', 'BOAT DECK AREA'))
    fireEvent.click(within(group).getByRole('button', { name: 'Accept AI suggestion' }))
    await waitFor(() => expect(api.decideLocation).toHaveBeenCalledWith('IMP-20261118-0123456789', 5, 'accept', 'POOP DECK AND MOORING WINCH'))
  })

  it('shows a decision error and hides decision controls from non-admins', async () => {
    const detail = { ...IMPORT, state_events: [], location_decisions: {}, location_problems: [],
      rows: [row(3, 'imported', { official_location: 'Hold 3 hatch', location_intel: AI_ROW,
        effective_location: { location: 'Hold 3 hatch', method: 'ai_suggestion', review_status: 'pending' } })] }
    api.getImportHistory.mockResolvedValue({ ...EMPTY_HISTORY, imports: [IMPORT] })
    api.getImport.mockResolvedValue(detail)
    api.decideLocation.mockRejectedValue(Object.assign(new Error('Import IMP-1 is withdrawn.'), { code: 'import_withdrawn' }))
    const { unmount } = renderAt('/imports?id=IMP-20261118-0123456789')
    const group = await screen.findByRole('group', { name: 'Location decision for row 3' })
    fireEvent.click(within(group).getByRole('button', { name: 'Reject' }))
    expect(await within(group).findByRole('alert')).toHaveTextContent('withdrawn')
    unmount()
    api.getSession.mockResolvedValue({ authenticated: true, user: fx.reviewerUser })
    renderAt('/imports?id=IMP-20261118-0123456789')
    expect(await screen.findByRole('heading', { name: 'Administrator access required' })).toBeInTheDocument()   // whole page guarded
    expect(screen.queryByRole('group', { name: /Location decision/ })).not.toBeInTheDocument()
  })

  describe('row links from the review queue (Phase 10.4)', () => {
    const eff = (location, status) => ({ effective_location: { location, method: 'x', review_status: status } })
    const LINK_DETAIL = { ...IMPORT, state_events: [], location_decisions: {}, location_problems: [],
      rows: [row(2, 'imported', { official_location: "Foc'sle", location_intel: li(), ...eff('FORECASTLE DECK AREA', 'auto_applied') }),
        row(3, 'imported', { official_location: 'Hold 3 hatch', location_intel: AI_ROW, ...eff('Hold 3 hatch', 'pending') }),
        row(4, 'imported', { official_location: 'Forward section', location_intel: UNKNOWN, ...eff('Forward section', 'pending') }),
        row(5, 'invalid', { errors: ['No readable photograph is embedded in this row (missing image).'] })] }
    const ID = 'IMP-20261118-0123456789'
    let scrolled
    beforeEach(() => {
      scrolled = []
      Element.prototype.scrollIntoView = vi.fn(function scroll() { scrolled.push(this.dataset.row) })
      api.getImportHistory.mockResolvedValue({ ...EMPTY_HISTORY, imports: [IMPORT] })
      api.getImport.mockResolvedValue(LINK_DETAIL)
    })
    const tab = (name) => screen.getByRole('tab', { name: new RegExp(`^${name}`) })

    it('opens the import on the Location review filter with the pending row highlighted and scrolled into view', async () => {
      renderAt(`/imports?id=${ID}&row=4`)
      expect(await screen.findByRole('status', { name: 'Linked row' })).toHaveTextContent('Row 4 needs a location review')
      expect(tab('Location review')).toHaveAttribute('aria-selected', 'true')
      const tr = document.querySelector('tr[data-row="4"]')
      expect(tr).toHaveAttribute('aria-current', 'true')
      expect(tr).toHaveTextContent('Linked')
      expect(document.querySelector('tr[data-row="2"]')).toBeNull()                       // filtered to pending rows
      expect(document.querySelectorAll('tr[aria-current="true"]')).toHaveLength(1)
      await waitFor(() => expect(scrolled).toContain('4'))
      expect(api.getImport).toHaveBeenCalledWith(ID, expect.anything())
    })

    it('shows a row that no longer needs review among all rows', async () => {
      renderAt(`/imports?id=${ID}&row=2`)
      expect(await screen.findByRole('status', { name: 'Linked row' })).toHaveTextContent('Row 2 no longer needs a location review')
      expect(tab('All')).toHaveAttribute('aria-selected', 'true')
      expect(document.querySelector('tr[data-row="2"]')).toHaveAttribute('aria-current', 'true')
    })

    it.each([['99', 'Row 99 is not an imported record'], ['5', 'Row 5 is not an imported record'],
      ['abc', 'is not a valid row number']])('handles row=%s gracefully', async (param, text) => {
      renderAt(`/imports?id=${ID}&row=${param}`)
      expect(await screen.findByRole('status', { name: 'Linked row' })).toHaveTextContent(text)
      expect(tab('All')).toHaveAttribute('aria-selected', 'true')
      expect(document.querySelector('tr[aria-current="true"]')).toBeNull()
      expect(scrolled).toEqual([])
    })

    it('is unchanged without a row parameter', async () => {
      renderAt(`/imports?id=${ID}`)
      await screen.findByRole('region', { name: 'Import details' })
      await screen.findByText('Forward section')
      expect(screen.queryByRole('status', { name: 'Linked row' })).not.toBeInTheDocument()
      expect(tab('All')).toHaveAttribute('aria-selected', 'true')
      expect(document.querySelector('tr[aria-current="true"]')).toBeNull()
    })

    it('highlights the row of a withdrawn import without offering decisions', async () => {
      api.getImport.mockResolvedValue({ ...LINK_DETAIL, status: 'withdrawn' })
      renderAt(`/imports?id=${ID}&row=3`)
      await screen.findByRole('status', { name: 'Linked row' })
      expect(document.querySelector('tr[data-row="3"]')).toHaveAttribute('aria-current', 'true')
      expect(screen.queryByRole('group', { name: /Location decision/ })).not.toBeInTheDocument()
    })
  })
})
