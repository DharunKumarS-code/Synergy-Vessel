/**
 * Synergy Vessel colour system — dark slate with light-blue accent.
 * Primary brand colour: #90CAF9.
 * Single source of truth: used by tailwind.config.js and by JS (charts, logo).
 */
export const colors = {
  // Dark slate surfaces, darkest -> lightest
  surface: {
    950: '#0f172a', // page background
    900: '#131d33', // inset fields, tooltips (between background and cards)
    850: '#1e293b', // cards
    800: '#243145', // raised controls on cards
    750: '#2a374c',
    700: '#334155', // borders
    600: '#475569', // strong borders
    500: '#64748b',
    sidebar: '#172554', // sidebar
  },
  // Typography
  ink: {
    DEFAULT: '#f8fafc', // primary text
    muted: '#94a3b8', // secondary text
    faint: '#8291a7', // captions; keeps ~4.5:1 contrast on cards
  },
  // Light-blue accent
  accent: {
    DEFAULT: '#90caf9', // brand primary
    strong: '#64b5f6', // pressed / emphasis
    soft: '#bbdefb', // hover on solid accent
    deep: '#42a5f5',
  },
  // Status
  ok: '#4caf50',
  warn: '#ffb74d',
  bad: '#ef5350',
}

/** Colours for Recharts and other JS-drawn graphics. */
export const chart = {
  primary: colors.accent.DEFAULT,
  secondary: colors.surface[600],
  grid: colors.surface[700],
  axis: colors.surface[600],
  tick: colors.ink.faint,
  cursor: 'rgba(144, 202, 249, 0.06)',
}
