import { useEffect, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import Sidebar from '../components/Sidebar'
import TopBar from '../components/TopBar'
import { useAuth } from '../context/AuthContext'
import { useHealth } from '../hooks/useHealth'
import { NAV_ITEMS } from './navigation'

export default function AppLayout() {
  const [navOpen, setNavOpen] = useState(false)
  const health = useHealth()
  const { notice, dismissNotice, requireSignIn } = useAuth()
  const { pathname } = useLocation()
  const current = NAV_ITEMS.find((n) => (n.end ? pathname === n.to : pathname.startsWith(n.to)))
  const title = current?.label ?? 'Synergy Vessel'

  useEffect(() => {
    document.title = `${title} · Synergy Vessel`
  }, [title])

  return (
    <div className="flex min-h-screen">
      <Sidebar open={navOpen} onClose={() => setNavOpen(false)} health={health} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar title={title} onMenu={() => setNavOpen(true)} healthStatus={health.status} />
        {health.status === 'offline' && (
          <div role="alert" className="border-b border-bad/30 bg-bad/10 px-6 py-2 text-xs text-bad">
            The inspection service is not reachable. Data may be unavailable until the connection is restored.
          </div>
        )}
        {notice && (
          <div role="status" aria-label="Session notice" className="flex flex-wrap items-center gap-3 border-b border-warn/30 bg-warn/10 px-6 py-2 text-xs text-warn">
            <span>{notice}</span>
            <button type="button" className="focus-ring underline" onClick={() => { dismissNotice(); requireSignIn() }}>Sign in</button>
          </div>
        )}
        <main className="mx-auto w-full max-w-[1600px] flex-1 px-4 py-6 sm:px-6 lg:py-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
