import '@testing-library/jest-dom/vitest'
import { cleanup, configure } from '@testing-library/react'
import { afterEach } from 'vitest'

// The first render in a file pays for transforming Recharts etc.; allow for slow cold starts.
configure({ asyncUtilTimeout: 15000 })

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

// SV-08: tests written before the viewpoint mapping never mock its API. Without this, the Compare page would send
// real requests from jsdom. Here those requests (and only those) answer "no mapping generated yet", which is the
// state the older tests assume. SV-08 tests mock getViewpoints / getViewpointGroups / ... directly instead.
import axios from 'axios'
import { http } from '../services/api'

const realAdapter = axios.getAdapter(http.defaults.adapter)
const NO_MAPPING = { available: false, revision: null, problem: 'No viewpoint mapping has been generated yet.' }
http.defaults.adapter = (config) => {
  if (String(config.url ?? '').startsWith('/api/inspections/viewpoints')) {
    const data = config.url.includes('/groups') ? { mapping: NO_MAPPING, groups: [] }
      : { location: config.params?.location ?? '', mapping: NO_MAPPING, weeks: [], viewpoints: [], unlinked: [] }
    return Promise.resolve({ data, status: 200, statusText: 'OK', headers: {}, config, request: {} })
  }
  return realAdapter(config)
}
