import { MapPin } from 'lucide-react'
import { useApi } from '../../hooks/useApi'
import { getLocations } from '../../services/api'
import { ErrorState, LoadingState } from '../ui/StateViews'

/** Chips for every named Official Location of the vessel (GET /api/locations). */
export default function BrowseLocations({ vesselId, onPick }) {
  const { data, loading, error, reload } = useApi((s) => getLocations({ vessel: vesselId }, s), [vesselId], {
    enabled: Boolean(vesselId),
  })
  const named = (data ?? []).filter((l) => !l.is_placeholder)
  return (
    <section className="card" aria-labelledby="browse-title">
      <div className="card-header">
        <div>
          <h2 id="browse-title" className="card-title">Browse inspection locations</h2>
          <p className="card-subtitle">Pick an Official Location, or type any part of a location name above</p>
        </div>
      </div>
      {loading ? (
        <LoadingState rows={3} label="Loading locations" />
      ) : error ? (
        <ErrorState error={error} title="Locations unavailable" onRetry={reload} compact />
      ) : (
        <div className="flex flex-wrap gap-2 p-5">
          {named.map((l) => (
            <button
              key={l.location}
              type="button"
              onClick={() => onPick(l.location)}
              className="chip focus-ring py-1 transition-colors hover:border-accent/50 hover:text-ink"
            >
              <MapPin size={11} aria-hidden="true" />
              {l.location}
              <span className="tabular-nums text-ink-faint">{l.record_count}</span>
            </button>
          ))}
        </div>
      )}
    </section>
  )
}
