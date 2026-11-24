import { AlertTriangle, ChevronDown, Ship } from 'lucide-react'
import { useVessel } from '../context/VesselContext'

export default function VesselSelector() {
  const { vessels, selectedVessel, selectVessel, loading, error, reload } = useVessel()

  if (loading && !vessels.length) {
    return <div className="skeleton h-9 w-44" role="status" aria-label="Loading vessels" />
  }
  if (error) {
    return (
      <button
        type="button"
        onClick={reload}
        className="focus-ring inline-flex items-center gap-2 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad"
        title="Retry loading vessels"
      >
        <AlertTriangle size={14} aria-hidden="true" /> Vessels unavailable — retry
      </button>
    )
  }
  if (!vessels.length) {
    return <span className="text-xs text-ink-faint">No vessels available</span>
  }

  return (
    <label className="relative inline-flex items-center">
      <span className="sr-only">Selected vessel</span>
      <Ship size={16} className="pointer-events-none absolute left-3 text-accent" aria-hidden="true" />
      <select
        value={selectedVessel?.vessel_id ?? ''}
        onChange={(e) => selectVessel(e.target.value)}
        className="focus-ring h-9 max-w-[14rem] cursor-pointer appearance-none truncate rounded-lg border border-surface-600 bg-surface-800 pl-9 pr-8 text-sm font-medium text-ink transition-colors hover:border-accent/50"
      >
        {vessels.map((v) => (
          <option key={v.vessel_id} value={v.vessel_id}>
            {v.vessel_name}
          </option>
        ))}
      </select>
      <ChevronDown size={14} className="pointer-events-none absolute right-2.5 text-ink-faint" aria-hidden="true" />
    </label>
  )
}
