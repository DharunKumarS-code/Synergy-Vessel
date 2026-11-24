import { ClipboardCheck, Columns2, FileSpreadsheet, FileText, LayoutDashboard, LineChart, Search, Settings, ShieldCheck } from 'lucide-react'

/**
 * Pages of the application.
 *   section 'manager' -> the everyday menu (shown to everyone)
 *   section 'admin'   -> the Administration menu (shown only to signed-in administrators; the pages are also
 *                        guarded in App.jsx and every protected action is re-checked by the server)
 *   section 'hidden'  -> still reachable by direct link (kept for future use), not listed in any menu
 * `label` is also the page title shown in the top bar.
 */
export const NAV_ITEMS = [
  { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard, section: 'manager',
    description: 'What needs attention: assessments awaiting review and the latest condition results.' },
  { to: '/compare', label: 'Compare Photos', icon: Columns2, section: 'manager',
    description: 'Choose a vessel and location, compare the earlier and later photographs, analyse them and review the result.' },
  { to: '/reviews', label: 'Reviews', icon: ClipboardCheck, section: 'manager',
    description: 'AI condition assessments awaiting confirmation by a reviewer.' },
  { to: '/reports', label: 'Reports', icon: FileText, section: 'manager',
    description: 'Download the inspection report (PDF) or the inspection spreadsheet.' },

  { to: '/imports', label: 'Excel Import', icon: FileSpreadsheet, section: 'admin',
    description: 'Import new Image Register workbooks, preview them first and review the import history.' },
  { to: '/admin', label: 'Administration', icon: ShieldCheck, section: 'admin',
    description: 'User accounts, roles and the audit log.' },
  { to: '/settings', label: 'Settings', icon: Settings, section: 'admin',
    description: 'Current application, data, AI and security configuration (read-only).' },

  { to: '/search', label: 'Inspection Search', icon: Search, section: 'hidden',
    description: 'Search a location, browse its inspection dates and view the photographs date-wise.' },
  { to: '/trends', label: 'Condition Trends', icon: LineChart, section: 'hidden',
    description: 'Saved AI condition analyses over time, by location and review status.' },
]

export const MANAGER_NAV = NAV_ITEMS.filter((n) => n.section === 'manager')
export const ADMIN_NAV = NAV_ITEMS.filter((n) => n.section === 'admin')
