import { colors } from '../theme/colors'

export default function Logo({ size = 36 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="shrink-0">
      <rect width="32" height="32" rx="9" fill={colors.accent.DEFAULT} />
      <path d="M6 19.5h20l-3.2 5.5H9.2z" fill={colors.surface[950]} />
      <path d="M15 6.5v11.5M15 7.5l7 9.5h-7" stroke={colors.surface[950]} strokeWidth="1.8" fill="none" strokeLinejoin="round" />
    </svg>
  )
}
