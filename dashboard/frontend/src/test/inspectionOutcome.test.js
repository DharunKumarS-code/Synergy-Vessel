/* Plain-language result and duplicate mapping shared by Compare Photos, Reviews and the Dashboard
   (moved from the removed Inspection Result page tests, 2026-11-23). */
import { describe, expect, it } from 'vitest'
import { isDuplicatePair, simpleResult } from '../components/compare/InspectionOutcome'
import { conditionSummary } from '../pages/DashboardPage'
import * as fx from './fixtures'

const analysis = (classification, extra = {}) => ({
  analysis_id: 'CA-1', classification, model_classification: classification, simulated: false, confidence: 'HIGH',
  explanation: 'Detailed AI explanation text', review: { status: 'PENDING_REVIEW', history: [] }, ...extra,
})

describe('result and duplicate mapping', () => {
  it('maps saved classifications to the plain result', () => {
    expect(simpleResult(analysis('AMELIORATED'))).toBe('IMPROVED')
    expect(simpleResult(analysis('DETERIORATED'))).toBe('DETERIORATED')
    expect(simpleResult(analysis('STABLE'))).toBe('NO_CHANGE')
    expect(simpleResult(analysis('NEEDS_REVIEW'))).toBe('NEEDS_REVIEW')
    expect(simpleResult(analysis('DETERIORATED', { simulated: true }))).toBe('NOT_ASSESSED')            // mock results never shown
    expect(simpleResult(analysis('DETERIORATED', { review: { status: 'REJECTED' } }))).toBe('NOT_ASSESSED')
    expect(simpleResult(undefined)).toBe('NOT_ASSESSED')
  })

  it('reuses the existing pair-check categories', () => {
    expect(isDuplicatePair(fx.pairBlocked)).toBe(true)
    expect(isDuplicatePair({ ...fx.pairPassed, perceptual: { category: 'VISUALLY_IDENTICAL' } })).toBe(true)
    expect(isDuplicatePair({ ...fx.pairPassed, perceptual: { category: 'NEAR_DUPLICATE' } })).toBe(true)
    expect(isDuplicatePair(fx.pairPassed)).toBe(false)
    expect(isDuplicatePair(null)).toBe(false)
  })
})

describe('dashboard condition summary', () => {
  it('counts saved assessments with the same mapping as Compare Photos; nothing becomes improved or deteriorated by default', () => {
    expect(conditionSummary([
      analysis('AMELIORATED'), analysis('STABLE'), analysis('STABLE'), analysis('DETERIORATED'), analysis('NEEDS_REVIEW'),
      analysis('DETERIORATED', { review: { status: 'REJECTED' } }), analysis('AMELIORATED', { simulated: true }),
    ])).toEqual({ IMPROVED: 1, NO_CHANGE: 2, DETERIORATED: 1, NEEDS_REVIEW: 3 })
    expect(conditionSummary(undefined)).toEqual({ IMPROVED: 0, NO_CHANGE: 0, DETERIORATED: 0, NEEDS_REVIEW: 0 })
  })
})
