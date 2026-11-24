import { X } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { ADMIN_NAV, MANAGER_NAV } from '../layouts/navigation'
import ConnectionStatus from './ConnectionStatus'
import Logo from './Logo'

function NavItem({ item: { to, label, icon: Icon }, onClose }) {
  return (
    <NavLink
      to={to}
      onClick={onClose}
      className={({ isActive }) =>
        `focus-ring group flex items-center gap-3 rounded-lg px-3 py-2.5 text-[15px] transition-colors ${
          isActive
            ? 'bg-accent/10 font-semibold text-ink shadow-[inset_2px_0_0_0_theme(colors.accent.DEFAULT)]'
            : 'font-medium text-ink-muted hover:bg-surface-800 hover:text-ink'
        }`
      }
    >
      {({ isActive }) => (
        <>
          <Icon size={18} className={isActive ? 'text-accent' : 'text-ink-faint group-hover:text-ink-muted'} aria-hidden="true" />
          <span className="flex-1 truncate">{label}</span>
        </>
      )}
    </NavLink>
  )
}

export default function Sidebar({ open, onClose, health }) {
  const { isAdmin } = useAuth()
  return (
    <>
      {/* backdrop for tablet / small screens */}
      <div
        className={`fixed inset-0 z-30 bg-surface-950/70 backdrop-blur-sm transition-opacity lg:hidden ${
          open ? 'opacity-100' : 'pointer-events-none opacity-0'
        }`}
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        id="app-sidebar"
        className={`fixed inset-y-0 left-0 z-40 flex w-72 flex-col border-r border-surface-700/70 bg-surface-sidebar transition-transform duration-200 lg:sticky lg:top-0 lg:h-screen lg:translate-x-0 ${
          open ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex h-16 items-center gap-3 border-b border-surface-700/70 px-5">
          <Logo />
          <div className="min-w-0 flex-1">
            <p className="truncate text-[15px] font-semibold tracking-tight text-ink">Synergy Vessel</p>
            <p className="truncate text-[11px] text-ink-faint">Inspection Monitoring</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="focus-ring rounded-md p-1.5 text-ink-faint hover:bg-surface-800 hover:text-ink lg:hidden"
            aria-label="Close navigation"
          >
            <X size={18} />
          </button>
        </div>

        <nav aria-label="Main navigation" className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
          {MANAGER_NAV.map((item) => <NavItem key={item.to} item={item} onClose={onClose} />)}
          {isAdmin && (
            <div role="group" aria-label="Administration menu">
              <p className="px-3 pb-2 pt-6 text-[11px] font-medium uppercase tracking-[0.14em] text-ink-faint">Administration</p>
              <div className="space-y-1">
                {ADMIN_NAV.map((item) => <NavItem key={item.to} item={item} onClose={onClose} />)}
              </div>
            </div>
          )}
        </nav>

        <div className="border-t border-surface-700/70 px-5 py-4">
          <ConnectionStatus status={health.status} />
          {health.health?.loaded_at && (
            <p className="mt-1 text-[11px] text-ink-faint">
              Data loaded {new Date(health.health.loaded_at).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' })}
            </p>
          )}
        </div>
      </aside>
    </>
  )
}
