import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { imageUrl } from '../../services/api'
import { formatDate, formatFilename, formatWeekLabel } from '../../utils/format'
import FileName from '../ui/FileName'
import { isDerivedDate } from '../../utils/searchResults'
import { DerivedDateBadge, MatchBadge } from './Badges'
import Photo, { hasImage } from './Photo'

function Row({ label, children }) {
  return (
    <div className="grid grid-cols-[8.5rem_1fr] gap-3 py-2">
      <dt className="text-xs text-ink-faint">{label}</dt>
      <dd className="min-w-0 break-words text-sm text-ink">{children}</dd>
    </div>
  )
}

const None = ({ children = 'Not available' }) => <span className="italic text-ink-faint">{children}</span>

/** Modal viewer for one photograph; ←/→ navigate, Esc closes. */
export default function ImageViewer({ items, index, onIndex, onClose }) {
  const item = items[index]
  const closeRef = useRef(null)
  const hasPrev = index > 0
  const hasNext = index < items.length - 1

  // keyboard navigation
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowLeft' && hasPrev) onIndex(index - 1)
      else if (e.key === 'ArrowRight' && hasNext) onIndex(index + 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [index, hasPrev, hasNext, onIndex, onClose])

  // focus management + background scroll lock
  useEffect(() => {
    const previous = document.activeElement
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    closeRef.current?.focus()
    return () => {
      document.body.style.overflow = overflow
      if (previous instanceof HTMLElement) previous.focus()
    }
  }, [])

  // warm the browser cache for neighbouring photographs
  useEffect(() => {
    for (const n of [items[index - 1], items[index + 1]]) {
      if (hasImage(n)) new Image().src = imageUrl(n.image_id)
    }
  }, [items, index])

  if (!item) return null
  const title = item.image_id ?? item.record_id

  return (
    <div
      className="fixed inset-0 z-50 flex items-stretch justify-center bg-surface-950/90 p-0 backdrop-blur-sm sm:p-4 lg:p-8"
      role="dialog"
      aria-modal="true"
      aria-labelledby="viewer-title"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="card flex w-full max-w-7xl flex-col overflow-hidden lg:flex-row">
        {/* image */}
        <div className="relative flex min-h-[40vh] flex-1 items-center justify-center bg-surface-950">
          <Photo key={item.record_id} item={item} eager className="h-full max-h-[80vh] w-full" alt={`Inspection photograph ${title}`} />
          <button
            type="button"
            onClick={() => onIndex(index - 1)}
            disabled={!hasPrev}
            aria-label="Previous photograph"
            className="focus-ring absolute left-3 top-1/2 grid h-11 w-11 -translate-y-1/2 place-items-center rounded-full border border-surface-600 bg-surface-900/90 text-ink transition-colors hover:border-accent/60 hover:text-accent disabled:pointer-events-none disabled:opacity-30"
          >
            <ChevronLeft size={22} />
          </button>
          <button
            type="button"
            onClick={() => onIndex(index + 1)}
            disabled={!hasNext}
            aria-label="Next photograph"
            className="focus-ring absolute right-3 top-1/2 grid h-11 w-11 -translate-y-1/2 place-items-center rounded-full border border-surface-600 bg-surface-900/90 text-ink transition-colors hover:border-accent/60 hover:text-accent disabled:pointer-events-none disabled:opacity-30"
          >
            <ChevronRight size={22} />
          </button>
          <span className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full bg-surface-900/90 px-3 py-1 text-xs tabular-nums text-ink-muted">
            {index + 1} / {items.length}
          </span>
        </div>

        {/* metadata */}
        <aside className="flex w-full flex-col border-t border-surface-700 lg:w-96 lg:border-l lg:border-t-0">
          <div className="flex items-start justify-between gap-3 border-b border-surface-700 px-5 py-4">
            <div className="min-w-0">
              <p className="text-[11px] uppercase tracking-[0.1em] text-ink-faint">Photograph</p>
              <h2 id="viewer-title" className="truncate font-mono text-lg font-semibold text-ink">{title}</h2>
            </div>
            <button
              ref={closeRef}
              type="button"
              onClick={onClose}
              aria-label="Close viewer"
              className="focus-ring grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-surface-600 text-ink-muted transition-colors hover:border-accent/50 hover:text-ink"
            >
              <X size={18} />
            </button>
          </div>
          <dl className="flex-1 divide-y divide-surface-700/60 overflow-y-auto px-5 py-2">
            <Row label="Inspection date">
              <span className="flex flex-wrap items-center gap-2">
                {item.inspection_date ? formatDate(item.inspection_date, { day: '2-digit', month: 'long', year: 'numeric' }) : <None>Undated</None>}
                {isDerivedDate(item) && <DerivedDateBadge reportDate={item.report_date} />}
              </span>
              {isDerivedDate(item) && (
                <span className="mt-1 block text-[11px] text-ink-faint">Original report date: {item.report_date ?? '—'}</span>
              )}
            </Row>
            <Row label="Week">
              {formatWeekLabel(item.week)}
              {item.week_folder && item.week_folder !== item.week && <span className="text-ink-faint"> · folder “{item.week_folder}”</span>}
            </Row>
            <Row label="Official Location">
              {item.official_location_labelled ? item.official_location : <None>Not labelled</None>}
            </Row>
            <Row label="AI Location">{item.ai_suggested_location ?? <None />}</Row>
            <Row label="AI confidence">{item.location_confidence ?? <None />}</Row>
            <Row label="Activity">{item.activity_identified?.trim() ? item.activity_identified : <None />}</Row>
            <Row label="Search match">
              <MatchBadge item={item} />
            </Row>
            <Row label="Record source">{item.source === 'image_register' ? 'Image register' : item.source === 'excel_import' ? `Excel import (${item.import_id})` : 'Weekly JSON report'}</Row>
            <Row label="Worksheet">{formatFilename(item.worksheet_name) ?? <None />}</Row>
            <Row label="Source workbook"><FileName name={item.original_excel_filename} className="text-xs text-ink-muted" /></Row>
            {item.same_photo_appearances > 1 && (
              <Row label="Same photo">Appears in {item.same_photo_appearances} inspection records</Row>
            )}
            {!hasImage(item) && <Row label="Image status"><span className="text-bad">Photograph unavailable</span></Row>}
          </dl>
          <p className="border-t border-surface-700 px-5 py-3 text-[11px] text-ink-faint">
            Use ← → to move between photographs · Esc to close
          </p>
        </aside>
      </div>
    </div>
  )
}
