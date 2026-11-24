import { colors } from './src/theme/colors.js'

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors,
      fontFamily: {
        sans: ['Inter', 'Segoe UI', 'system-ui', '-apple-system', 'Roboto', 'sans-serif'],
      },
      boxShadow: {
        card: '0 1px 0 0 rgba(144,202,249,0.03) inset, 0 8px 24px -14px rgba(0,0,0,0.7)',
      },
    },
  },
  plugins: [],
}
