import '@testing-library/jest-dom/vitest'
import { cleanup, configure } from '@testing-library/react'
import { afterEach } from 'vitest'

// The first render in a file pays for transforming Recharts etc.; allow for slow cold starts.
configure({ asyncUtilTimeout: 5000 })

afterEach(() => {
  cleanup()
  try {
    window.localStorage.clear()
  } catch {
    /* ignore */
  }
})

// jsdom lacks ResizeObserver (used by Recharts ResponsiveContainer).
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}
window.scrollTo = () => {}
