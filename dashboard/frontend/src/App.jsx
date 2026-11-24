import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import AdminOnly from './components/AdminOnly'
import AuthGate from './components/AuthGate'
import { AuthProvider } from './context/AuthContext'
import { VesselProvider } from './context/VesselContext'
import AppLayout from './layouts/AppLayout'
import AdminPage from './pages/AdminPage'
import ConditionTrendsPage from './pages/ConditionTrendsPage'
import DashboardPage from './pages/DashboardPage'
import ImageComparisonPage from './pages/ImageComparisonPage'
import ImportPage from './pages/ImportPage'
import InspectionSearchPage from './pages/InspectionSearchPage'
import NotFoundPage from './pages/NotFoundPage'
import ReportsPage from './pages/ReportsPage'
import ReviewsPage from './pages/ReviewsPage'
import SettingsPage from './pages/SettingsPage'

/** Old links to the AI Condition Analysis page (/analysis?id=...) open the same assessment on Reviews. */
function ToReviews() {
  const { search } = useLocation()
  return <Navigate to={`/reviews${search}`} replace />
}

export default function App() {
  return (
    <AuthProvider>
    {/* SV-02: nothing below is mounted (and no data is requested) until a user has signed in. */}
    <AuthGate>
    <VesselProvider>
      <Routes>
        <Route element={<AppLayout />}>
          {/* Compare Photos is the main workflow; the former Inspection Result home screen is merged into it. */}
          <Route index element={<Navigate to="/compare" replace />} />
          <Route path="dashboard" element={<DashboardPage />} />
          <Route path="compare" element={<ImageComparisonPage />} />
          <Route path="reviews" element={<ReviewsPage />} />
          <Route path="analysis" element={<ToReviews />} />
          <Route path="reports" element={<ReportsPage />} />
          {/* Not in the menu; kept reachable by direct link. */}
          <Route path="search" element={<InspectionSearchPage />} />
          <Route path="trends" element={<ConditionTrendsPage />} />
          {/* Administration (menu shown to administrators only). */}
          <Route path="imports" element={<AdminOnly><ImportPage /></AdminOnly>} />
          <Route path="admin" element={<AdminOnly><AdminPage /></AdminOnly>} />
          <Route path="settings" element={<AdminOnly><SettingsPage /></AdminOnly>} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </VesselProvider>
    </AuthGate>
    </AuthProvider>
  )
}
