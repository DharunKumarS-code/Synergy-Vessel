import { ImageOff } from 'lucide-react'
import { useEffect, useState } from 'react'
import { usePhotoRetry } from '../../hooks/usePhotoRetry'
import { imageUrl } from '../../services/api'

export const hasImage = (item) => Boolean(item?.image_id) && item?.image_status !== 'missing'

/**
 * Inspection photograph served by GET /api/images/{image_id}. Lazy-loaded with a
 * skeleton while loading; aspect ratio preserved (object-contain); safe fallback on error.
 * SV-02: the request is authorised by the HttpOnly photo cookie set at sign-in (never by the URL); a failed load
 * re-checks the session once before showing the fallback.
 */
export default function Photo({ item, eager = false, className = '', alt }) {
  const [state, setState] = useState(hasImage(item) ? 'loading' : 'missing')
  useEffect(() => setState(hasImage(item) ? 'loading' : 'missing'), [item?.image_id, item?.image_status])
  const retry = usePhotoRetry(item?.image_id)

  if (state === 'missing' || state === 'error') {
    return (
      <div className={`flex flex-col items-center justify-center gap-2 bg-surface-900 text-ink-faint ${className}`} role="img"
        aria-label={state === 'missing' ? 'Photograph unavailable' : 'Photograph could not be loaded'}>
        <ImageOff size={22} aria-hidden="true" />
        <span className="text-xs">{state === 'missing' ? 'Photograph unavailable' : 'Could not load photograph'}</span>
      </div>
    )
  }
  return (
    <div className={`relative bg-surface-900 ${className}`}>
      {state === 'loading' && <div className="skeleton absolute inset-0 rounded-none" aria-hidden="true" />}
      <img
        key={retry.attempt}
        src={imageUrl(item.image_id)}
        alt={alt ?? `Inspection photograph ${item.image_id}`}
        loading={eager ? 'eager' : 'lazy'}
        decoding="async"
        draggable={false}
        onLoad={() => setState('loaded')}
        onError={async () => { if (!(await retry.onError())) setState('error') }}
        className={`h-full w-full object-contain transition-opacity duration-300 ${state === 'loaded' ? 'opacity-100' : 'opacity-0'}`}
      />
    </div>
  )
}
