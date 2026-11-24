import { Bell, CalendarDays, LogIn, LogOut, Menu, UserRound } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ROLE_LABELS, useAuth } from '../context/AuthContext'
import { useVessel } from '../context/VesselContext'
import { formatDate, formatToday } from '../utils/format'
import ConnectionStatus from './ConnectionStatus'
import VesselSelector from './VesselSelector'

function useOutsideClose(open, setOpen) {
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const onDown = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false)
    const onKey = (e) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, setOpen])
  return ref
}

function Notifications() {
  const [open, setOpen] = useState(false)
  const ref = useOutsideClose(open, setOpen)
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="focus-ring grid h-9 w-9 place-items-center rounded-lg border border-surface-600 bg-surface-800 text-ink-muted transition-colors hover:border-accent/50 hover:text-ink"
        aria-label="Notifications"
        aria-expanded={open}
        aria-haspopup="true"
      >
        <Bell size={16} />
      </button>
      {open && (
        <div className="card absolute right-0 z-50 mt-2 w-72 bg-surface-850 p-0">
          <div className="card-header py-3">
            <p className="card-title">Notifications</p>
          </div>
          <p className="px-5 py-6 text-center text-xs text-ink-faint">No notifications yet.</p>
        </div>
      )}
    </div>
  )
}

function UserProfile() {
  const { user, checking, requireSignIn, signOut } = useAuth()
  if (checking) return null
  if (!user) {
    return (
      <button type="button" onClick={() => requireSignIn()}
        className="focus-ring inline-flex h-9 items-center gap-2 rounded-lg border border-surface-600 bg-surface-800 px-3 text-sm text-ink-muted hover:border-accent/50 hover:text-ink">
        <LogIn size={15} aria-hidden="true" /> Sign in
      </button>
    )
  }
  return (
    <div className="flex items-center gap-2.5 rounded-lg border border-transparent pl-1">
      <span className="grid h-9 w-9 place-items-center rounded-full bg-surface-750 text-accent ring-1 ring-accent/20">
        <UserRound size={17} aria-hidden="true" />
      </span>
      <div className="hidden leading-tight xl:block" aria-label="Signed-in user">
        <p className="text-sm font-medium text-ink">{user.display_name}</p>
        <p className="text-[11px] text-ink-faint">{ROLE_LABELS[user.role] || user.role} · {user.username}</p>
      </div>
      <button type="button" onClick={signOut} aria-label="Sign out"
        className="focus-ring grid h-9 w-9 place-items-center rounded-lg text-ink-muted hover:bg-surface-800 hover:text-ink">
        <LogOut size={15} aria-hidden="true" />
      </button>
    </div>
  )
}

export default function TopBar({ title, onMenu, healthStatus }) {
  const { selectedVessel } = useVessel()
  const [today, setToday] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setToday(new Date()), 60000)
    return () => clearInterval(id)
  }, [])

  return (
    <header className="sticky top-0 z-20 border-b border-surface-700/70 bg-surface-950/85 backdrop-blur supports-[backdrop-filter]:bg-surface-950/70">
      <div className="flex h-16 items-center gap-3 px-4 sm:px-6">
        <button
          type="button"
          onClick={onMenu}
          className="focus-ring -ml-1 rounded-md p-2 text-ink-muted hover:bg-surface-800 hover:text-ink lg:hidden"
          aria-label="Open navigation"
          aria-controls="app-sidebar"
        >
          <Menu size={20} />
        </button>

        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold tracking-tight text-ink">{title}</h1>
          <p className="hidden truncate text-xs text-ink-faint sm:block">
            {selectedVessel ? (
              <>
                {selectedVessel.vessel_name} · {selectedVessel.weeks?.length ?? 0} inspection weeks ·{' '}
                {formatDate(selectedVessel.first_inspection_date)} – {formatDate(selectedVessel.last_inspection_date)}
              </>
            ) : (
              'Vessel inspection monitoring'
            )}
          </p>
        </div>

        <div className="hidden items-center gap-2 text-xs text-ink-muted xl:flex">
          <CalendarDays size={15} className="text-ink-faint" aria-hidden="true" />
          <time dateTime={today.toISOString().slice(0, 10)}>{formatToday(today)}</time>
        </div>
        <span className="hidden h-6 w-px bg-surface-700 xl:block" aria-hidden="true" />
        <span className="md:hidden">
          <ConnectionStatus status={healthStatus} compact />
        </span>
        <VesselSelector />
        <Notifications />
        <UserProfile />
      </div>
    </header>
  )
}
