import { formatFilename } from '../../utils/format'

/**
 * A file name shown tidily (repeated/edge whitespace collapsed) that wraps instead of breaking the layout.
 * The exact stored name stays available as the tooltip; nothing is renamed.
 */
export default function FileName({ name, fallback = '—', className = '' }) {
  const shown = formatFilename(name)
  if (!shown) return <span className={className}>{fallback}</span>
  return (
    <span className={`[overflow-wrap:anywhere] ${className}`} title={name} data-original-name={name}>
      {shown}
    </span>
  )
}
