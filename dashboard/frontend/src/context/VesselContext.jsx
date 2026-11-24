import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { useApi } from '../hooks/useApi'
import { getVessels } from '../services/api'

const VesselContext = createContext(null)
const STORAGE_KEY = 'synergy.selectedVessel'

function readStored() {
  try {
    return window.localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

export function VesselProvider({ children }) {
  const { data, error, loading, reload } = useApi((signal) => getVessels(signal), [])
  const [selectedId, setSelectedId] = useState(readStored)

  const vessels = useMemo(() => (Array.isArray(data) ? data : []), [data])

  // Keep the selection valid, synchronously: fall back to the first vessel the API
  // returns, so consumers never see "vessels loaded but none selected".
  const selectedVessel = vessels.find((v) => v.vessel_id === selectedId) ?? vessels[0] ?? null
  const effectiveId = selectedVessel?.vessel_id ?? null

  useEffect(() => {
    if (!effectiveId) return
    try {
      window.localStorage.setItem(STORAGE_KEY, effectiveId)
    } catch {
      /* storage unavailable: selection just isn't remembered */
    }
  }, [effectiveId])

  const value = useMemo(
    () => ({
      vessels,
      selectedVessel,
      selectVessel: setSelectedId,
      loading,
      error,
      reload,
    }),
    [vessels, selectedVessel, loading, error, reload],
  )
  return <VesselContext.Provider value={value}>{children}</VesselContext.Provider>
}

export function useVessel() {
  const ctx = useContext(VesselContext)
  if (!ctx) throw new Error('useVessel must be used inside <VesselProvider>')
  return ctx
}
