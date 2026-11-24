// Small fixtures shaped exactly like the Phase 1 API responses (values are synthetic).
export const vessels = [
  {
    vessel_id: 'test-vessel',
    vessel_name: 'Test Vessel',
    name_source: 'original_excel_filename',
    record_count: 4,
    weeks: ['Week 30', 'week 34'],
    first_inspection_date: '2026-08-02',
    last_inspection_date: '2026-08-30',
  },
]

const rec = (id, week, week_number, date, loc, labelled, sha, extra = {}) => ({
  record_id: id,
  image_id: id,
  image_url: `/api/images/${id}`,
  image_status: 'resolved',
  week,
  week_number,
  inspection_date: date,
  date_source: 'report_date',
  official_location: loc,
  official_location_labelled: labelled,
  extracted_photograph: { sha256: sha },
  ...extra,
})

export const inspections = {
  total: 4,
  count: 4,
  offset: 0,
  limit: 2000,
  items: [
    rec('A-1', 'Week 30', 30, '2026-08-02', 'Deck One', true, 'aaa'),
    rec('A-2', 'Week 30', 30, '2026-08-02', 'Not Labelled', false, 'bbb'),
    rec('B-1', 'week 34', 34, '2026-08-30', 'Deck One', true, 'aaa', { date_source: 'worksheet_name' }),
    rec('B-2', 'week 34', 34, '2026-08-30', 'Hold Two', true, 'ccc'),
  ],
}

export const locations = [
  { location: 'Deck One', variants: ['Deck One'], is_placeholder: false, record_count: 2, date_count: 2, vessel_ids: ['test-vessel'] },
  { location: 'Hold Two', variants: ['Hold Two'], is_placeholder: false, record_count: 1, date_count: 1, vessel_ids: ['test-vessel'] },
  { location: 'Not Labelled', variants: ['Not Labelled'], is_placeholder: true, record_count: 1, date_count: 1, vessel_ids: ['test-vessel'] },
]

export const health = { status: 'ok', weeks_loaded: 2, records: 4, resolved_images: 4, loaded_at: '2026-11-18T10:00:00' }

// GET /api/inspections/search shaped result (items carry matched_on).
const hit = (id, week, week_number, date, official, ai, matched_on, sha, extra = {}) => ({
  record_id: id,
  image_id: id,
  image_url: `/api/images/${id}`,
  image_status: 'resolved',
  week,
  week_folder: week,
  week_number,
  inspection_date: date,
  date_source: 'report_date',
  report_date: date,
  official_location: official,
  official_location_labelled: official !== 'Not Labelled',
  ai_suggested_location: ai,
  location_confidence: ai ? 'High' : null,
  activity_identified: 'No activity observed',
  source: 'json_report',
  same_photo_appearances: 1,
  extracted_photograph: { sha256: sha },
  matched_on,
  ...extra,
})

export const searchResult = {
  location: 'main deck',
  match: 'contains',
  include_ai_location: true,
  count: 4,
  items: [
    hit('W30-0001', 'Week 30', 30, '2026-08-02', 'Not Labelled', 'Main Deck', ['ai_suggested_location'], 'aaa'),
    hit('W30-0002', 'Week 30', 30, '2026-08-02', 'Main Deck', null, ['official_location'], 'bbb', { same_photo_appearances: 2 }),
    hit('W34-0001', 'week 34', 34, '2026-08-30', 'Top View Main Deck', 'Main Deck', ['official_location', 'ai_suggested_location'], 'bbb', {
      date_source: 'worksheet_name',
      report_date: 'AMBIGUOUS/PENDING',
      same_photo_appearances: 2,
    }),
    hit('W35-0001', 'week 35', 35, '2026-09-06', 'Main Deck', null, ['official_location'], 'ccc', {
      image_id: null,
      image_url: null,
      image_status: 'missing',
      record_id: 'week_35-json-0000',
    }),
  ],
}

/* ------------------------------------------------------------------ Phase 5A: image-pair validation */
const side = (record_id, extra = {}) => ({
  record_id, image_id: record_id, readable: true, format: 'JPEG', width: 850, height: 637,
  quality: 'GOOD', quality_notes: [], sha256_matches_record: true, ...extra,
})
const NOTICE = 'Perceptual hashing detects copies of the same photograph; it cannot prove that two photographs show the same physical area or the same view.'

export const pairPassed = {
  valid: true, requires_acknowledgement: false, requires_human_review: false, sha256_duplicate: false,
  date_order_valid: true, vessel_match: true,
  perceptual: { phash_distance: 24, dhash_distance: 27, similarity: 0.6, category: 'NOT_DUPLICATE' },
  location: { match: 'OFFICIAL', explanation: 'Both records have the same Official Location.' },
  earlier: side('W30-0001'), later: side('W34-0001'), errors: [], warnings: [], info: [], notice: NOTICE,
}

export const pairWarnings = {
  ...pairPassed, requires_acknowledgement: true, requires_human_review: true,
  perceptual: { phash_distance: 6, dhash_distance: 5, similarity: 0.914, category: 'NEAR_DUPLICATE' },
  location: { match: 'AI_ONLY', explanation: 'Neither record has an Official Location.' },
  warnings: [
    { code: 'ai_only_location', severity: 'warning', message: 'Location match relies only on unverified AI-suggested locations.' },
    { code: 'near_duplicate', severity: 'warning', message: 'The photographs are nearly identical in content.' },
  ],
  info: [{ code: 'derived_date', severity: 'info', message: 'The later inspection date was derived from the worksheet name.' }],
}

export const pairBlocked = {
  ...pairPassed, valid: false, sha256_duplicate: true,
  perceptual: { phash_distance: null, dhash_distance: null, similarity: null, category: 'IDENTICAL_FILE' },
  errors: [{ code: 'same_image', severity: 'error', message: 'Both selections are the same photograph file (byte-for-byte identical, same SHA-256).' }],
}

/* Phase 8.2 signed-in users */
export const reviewerUser = { user_id: 'USR-000000000001', username: 'r.jones', display_name: 'R. Jones', role: 'reviewer' }
export const adminUser = { user_id: 'USR-000000000002', username: 'chief', display_name: 'Chief Officer', role: 'admin' }
// SV-02: read-only account (sees every page's data, no analysis or review controls).
export const viewerUser = { user_id: 'USR-000000000003', username: 'v.smith', display_name: 'V. Smith', role: 'viewer' }
