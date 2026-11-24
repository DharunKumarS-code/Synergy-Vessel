import {
  Activity, BrainCircuit, Database, Info, Lock, RefreshCw, Server, Settings as SettingsIcon, ShieldCheck,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { version as DASHBOARD_VERSION } from '../../package.json'
import { PROVIDER_LABELS } from '../components/compare/ConditionAnalysis'
import { ErrorState, LoadingState } from '../components/ui/StateViews'
import { ROLE_LABELS, useAuth } from '../context/AuthContext'
import { getAiStatus, getHealth, getVessels, toApiError } from '../services/api'
import { formatDate, formatNumber } from '../utils/format'

/**
 * Settings: a read-only view of the server's actual configuration, assembled from existing endpoints
 * (health, vessels, AI status, session). Nothing here changes configuration;
 * data folders, sign-in and AI providers are managed on the server.
 */

const APP_NAME = 'Synergy Vessel'
const AUTO_REFRESH_MS = 60000

/** Fetches on mount, on every `tick`, and keeps the last good value visible while refreshing. */
function useSnapshot(fetcher, tick) {
  const [state, setState] = useState({ data: null, error: null, loading: true })
  const ref = useRef(fetcher)
  ref.current = fetcher
  useEffect(() => {
    const controller = new AbortController()
    setState((s) => ({ ...s, loading: true }))
    ref.current(controller.signal)
      .then((data) => !controller.signal.aborted && setState({ data, error: null, loading: false }))
      .catch((err) => {
        const e = toApiError(err)
        if (controller.signal.aborted || e.kind === 'cancelled') return
        setState({ data: null, error: e, loading: false })
      })
    return () => controller.abort()
  }, [tick])
  return state
}

const TONES = {
  ok: 'border-ok/30 bg-ok/10 text-ok',
  warn: 'border-warn/30 bg-warn/10 text-warn',
  bad: 'border-bad/30 bg-bad/10 text-bad',
  accent: 'border-accent/30 bg-accent/10 text-accent',
  neutral: 'border-surface-600/70 bg-surface-800 text-ink-muted',
}

export function StatusBadge({ tone = 'neutral', children }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${TONES[tone]}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
      {children}
    </span>
  )
}

function Section({ id, title, icon: Icon, subtitle, wide = false, children }) {
  return (
    <section className={`card flex flex-col ${wide ? 'lg:col-span-2' : ''}`} aria-labelledby={`${id}-title`} data-testid={`settings-${id}`}>
      <div className="card-header">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-accent/10 text-accent ring-1 ring-accent/20">
            <Icon size={16} aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <h3 id={`${id}-title`} className="card-title">{title}</h3>
            {subtitle && <p className="card-subtitle">{subtitle}</p>}
          </div>
        </div>
        <span className="chip shrink-0">Read-only</span>
      </div>
      <div className="flex-1">{children}</div>
    </section>
  )
}

function Rows({ children }) {
  return <dl className="divide-y divide-surface-700/50 px-5 py-1">{children}</dl>
}

function Row({ label, children }) {
  return (
    <div className="flex flex-col gap-0.5 py-2.5 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
      <dt className="text-xs text-ink-faint">{label}</dt>
      <dd className="min-w-0 text-sm text-ink sm:text-right [overflow-wrap:anywhere]">{children}</dd>
    </div>
  )
}

const dateTime = (iso) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function inspectionPeriod(vessels) {
  const firsts = vessels.map((v) => v.first_inspection_date).filter(Boolean).sort()
  const lasts = vessels.map((v) => v.last_inspection_date).filter(Boolean).sort()
  return firsts.length && lasts.length ? `${formatDate(firsts[0])} – ${formatDate(lasts[lasts.length - 1])}` : '—'
}

/** Enabled / Disabled / Unavailable, from the server's own status. */
export function aiState(status) {
  if (!status) return { label: 'Unknown', tone: 'neutral' }
  if (status.available) return { label: 'Enabled', tone: 'ok' }
  if (status.provider === 'disabled') return { label: 'Disabled', tone: 'neutral' }
  return { label: 'Unavailable', tone: 'warn' }
}

const modeOf = (s) => (s?.simulated ? { label: 'Simulated (mock)', tone: 'warn' } : s?.live ? { label: 'Live', tone: 'accent' } : { label: 'Not applicable', tone: 'neutral' })

function SectionError({ error, what, onRetry }) {
  return <ErrorState compact error={error} title={`Unable to load ${what}`} onRetry={onRetry} />
}

export default function SettingsPage() {
  const [tick, setTick] = useState(0)
  const [checkedAt, setCheckedAt] = useState(() => new Date())
  const refresh = useCallback(() => {
    setTick((t) => t + 1)
    setCheckedAt(new Date())
  }, [])
  useEffect(() => {
    const timer = setInterval(refresh, AUTO_REFRESH_MS)
    return () => clearInterval(timer)
  }, [refresh])

  const health = useSnapshot(getHealth, tick)
  const vessels = useSnapshot(getVessels, tick)
  const ai = useSnapshot(getAiStatus, tick)
  const { user, checking } = useAuth()

  const h = health.data
  const online = h?.status === 'ok'
  const vesselList = Array.isArray(vessels.data) ? vessels.data : []
  const missing = h ? Math.max(0, (h.records ?? 0) - (h.resolved_images ?? 0)) : 0
  const anyFailed = [vessels, ai].some((s) => s.error)
  const system = health.loading && !h
    ? { label: 'Checking…', tone: 'neutral' }
    : !online ? { label: 'Unavailable', tone: 'bad' }
      : anyFailed || missing > 0 ? { label: 'Degraded', tone: 'warn' }
        : { label: 'Operational', tone: 'ok' }
  const connection = health.loading && !h && !health.error
    ? { label: 'Checking…', tone: 'neutral' }
    : online ? { label: 'Connected', tone: 'ok' } : { label: 'Unavailable', tone: 'bad' }
  const refreshing = [health, vessels, ai].some((s) => s.loading)

  const a = ai.data
  const aiNow = aiState(a)

  return (
    <div className="space-y-6">
      <section className="card p-6" aria-labelledby="settings-title">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex items-start gap-4">
            <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-accent/10 text-accent ring-1 ring-accent/20">
              <SettingsIcon size={20} aria-hidden="true" />
            </span>
            <div>
              <h2 id="settings-title" className="text-xl font-semibold tracking-tight text-ink">Settings</h2>
              <p className="mt-1 text-sm text-ink-muted">
                The current server configuration, read live from the backend. Everything on this page is informational;
                configuration is changed by an administrator on the server.
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <span aria-label="System status"><StatusBadge tone={system.tone}>{system.label}</StatusBadge></span>
            <button
              type="button"
              onClick={refresh}
              disabled={refreshing}
              className="focus-ring inline-flex items-center gap-1.5 rounded-lg border border-surface-600 bg-surface-800 px-3 py-1.5 text-xs font-medium text-ink-muted transition-colors hover:border-accent/50 hover:text-ink disabled:opacity-60"
            >
              <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} aria-hidden="true" /> Refresh
            </button>
          </div>
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-2" data-testid="settings-grid">
        {/* 1. Application information */}
        <Section id="application" title="Application Information" icon={Info} subtitle="Versions and data loading">
          <Rows>
            <Row label="Application">{APP_NAME}</Row>
            <Row label="Dashboard version">{DASHBOARD_VERSION}</Row>
            <Row label="Backend connection"><StatusBadge tone={connection.tone}>{connection.label}</StatusBadge></Row>
            <Row label="Data loading">
              {h ? (online && h.records > 0 ? <StatusBadge tone="ok">Loaded</StatusBadge> : <StatusBadge tone="warn">No records loaded</StatusBadge>)
                : health.error ? <StatusBadge tone="bad">Unknown</StatusBadge> : '…'}
            </Row>
            <Row label="Inspection weeks">{formatNumber(h?.weeks_loaded)}</Row>
            <Row label="Inspection records">{formatNumber(h?.records)}</Row>
          </Rows>
        </Section>

        {/* 2. Inspection data */}
        <Section id="data" title="Inspection Data" icon={Database} subtitle="Historical records are read-only">
          {health.error && !h ? (
            <SectionError error={health.error} what="inspection data" onRetry={refresh} />
          ) : !h && health.loading ? (
            <LoadingState label="Loading inspection data…" rows={4} />
          ) : (
            <Rows>
              <Row label="Data source">
                {missing > 0
                  ? <StatusBadge tone="warn">{formatNumber(missing)} photographs not found</StatusBadge>
                  : <StatusBadge tone="ok">All photographs found</StatusBadge>}
              </Row>
              <Row label="Vessels">{vessels.error ? 'Not available' : vesselList.map((v) => v.vessel_name).join(', ') || '—'}</Row>
              <Row label="Inspection period">{vessels.error ? 'Not available' : inspectionPeriod(vesselList)}</Row>
              <Row label="Inspection records">{formatNumber(h?.records)}</Row>
              <Row label="Photographs">{formatNumber(h?.resolved_images)}</Row>
              <Row label="Inspection weeks">{formatNumber(h?.weeks_loaded)}</Row>
            </Rows>
          )}
        </Section>

        {/* 3. AI configuration */}
        <Section id="ai" title="AI Configuration" icon={BrainCircuit} subtitle="Set on the server; not changeable here" wide>
          {ai.error && !a ? (
            <SectionError error={ai.error} what="the AI configuration" onRetry={refresh} />
          ) : !a && ai.loading ? (
            <LoadingState label="Loading AI configuration…" rows={4} />
          ) : (
            <div className="grid gap-x-6 md:grid-cols-2">
              <Rows>
                <Row label="Condition Analysis provider">{PROVIDER_LABELS[a?.provider] ?? a?.provider ?? '—'}</Row>
                <Row label="Model">{a?.model || '—'}</Row>
                <Row label="Condition Analysis status"><span aria-label="Condition Analysis status"><StatusBadge tone={aiNow.tone}>{aiNow.label}</StatusBadge></span></Row>
                <Row label="Mode">{a?.provider === 'disabled' ? '—' : <StatusBadge tone={modeOf(a).tone}>{modeOf(a).label}</StatusBadge>}</Row>
                <Row label="Human review">
                  {a?.review_statuses?.includes('PENDING_REVIEW') ? 'Required — every result starts as Pending review' : '—'}
                </Row>
              </Rows>
              <Rows>
                <Row label="Saved condition analyses">{formatNumber(a?.store?.stored_analyses)}</Row>
                <Row label="Prompt version">{a?.prompt_version || '—'}</Row>
              </Rows>
            </div>
          )}
          {a && (
            <div className="space-y-2 border-t border-surface-700/60 px-5 py-4 text-xs leading-relaxed text-ink-muted">
              {!a.available && a.message && <p role="note" aria-label="AI status message" className="text-warn">{a.message}</p>}
              <p role="note" aria-label="AI data use">
                {a.available && !a.simulated
                  ? <>When a signed-in user selects <strong className="text-ink">Analyse images</strong>, the two selected photographs are sent to the configured external AI provider ({PROVIDER_LABELS[a.provider] ?? a.provider}). Nothing is sent automatically, and results are AI-generated suggestions that require human review.</>
                  : a.available
                    ? 'The mock provider returns built-in simulated results; no photographs leave the server.'
                    : 'While Condition Analysis is not enabled, no photographs are sent to any AI provider.'}
              </p>
            </div>
          )}
        </Section>

        {/* 4. Security */}
        <Section id="security" title="Security" icon={ShieldCheck} subtitle="Checked by the server on every protected action">
          <Rows>
            <Row label="Authentication"><StatusBadge tone="ok">Enabled</StatusBadge></Row>
            <Row label="Signed in as">{checking ? '…' : user ? (user.display_name || user.username) : 'Not signed in'}</Row>
            <Row label="Role">{checking ? '…' : user ? (ROLE_LABELS[user.role] ?? user.role) : '—'}</Row>
            <Row label="Session">
              {checking ? <StatusBadge>Checking…</StatusBadge> : user ? <StatusBadge tone="ok">Active</StatusBadge> : <StatusBadge>Not signed in</StatusBadge>}
            </Row>
            <Row label="AI analysis">Requires sign-in (reviewer or administrator)</Row>
          </Rows>
          <div className="border-t border-surface-700/60 px-5 py-4">
            <p className="text-xs font-medium text-ink-muted">Protected functions</p>
            <ul className="mt-2 space-y-1 text-xs text-ink-faint" aria-label="Protected functions">
              <li>User accounts and the audit log — administrators only</li>
              <li>Excel imports, withdrawals and verification — administrators only</li>
              <li>Location decisions on imported rows — administrators only</li>
              <li>Running and reviewing AI condition analyses — signed-in users</li>
            </ul>
            <p className="mt-3 text-[11px] text-ink-faint">
              Sessions end when you sign out or after a period of inactivity. Session tokens are never displayed.
            </p>
          </div>
        </Section>

        {/* 5. System information */}
        <Section id="system" title="System Information" icon={Server} subtitle="Rechecked every minute">
          <Rows>
            <Row label="System status"><StatusBadge tone={system.tone}>{system.label}</StatusBadge></Row>
            <Row label="Backend health">
              {h ? <StatusBadge tone={online ? 'ok' : 'bad'}>{online ? 'Healthy' : 'Not healthy'}</StatusBadge>
                : health.error ? <StatusBadge tone="bad">Unreachable</StatusBadge> : '…'}
            </Row>
            <Row label="API connectivity">
              {health.error ? <span className="text-bad">{health.error.message}</span> : h ? 'Reachable' : '…'}
            </Row>
            <Row label="Last data load">{dateTime(h?.loaded_at)}</Row>
            <Row label="Last checked">{dateTime(checkedAt.toISOString())}</Row>
          </Rows>
          {anyFailed && online && (
            <p role="note" className="mx-5 mb-4 flex items-center gap-2 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
              <Activity size={13} aria-hidden="true" /> Some configuration details could not be loaded. Use Refresh to try again.
            </p>
          )}
        </Section>

        {/* Server-managed configuration */}
        <section className="card p-5 lg:col-span-2" aria-labelledby="server-managed-title">
          <div className="flex items-start gap-3">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-surface-800 text-ink-faint ring-1 ring-surface-600/70">
              <Lock size={15} aria-hidden="true" />
            </span>
            <div>
              <h3 id="server-managed-title" className="card-title">Managed on the server</h3>
              <p className="mt-1 text-xs leading-relaxed text-ink-muted">
                API keys, AI providers and live-analysis approval, data folders, sign-in configuration and service restarts
                cannot be changed from the dashboard. They are set by an administrator on the server, so that secrets never
                pass through the browser and a signed-in session cannot change what data is read or where photographs are sent.
              </p>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
