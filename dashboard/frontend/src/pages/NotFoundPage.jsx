import { Link } from 'react-router-dom'

export default function NotFoundPage() {
  return (
    <section className="card mx-auto max-w-lg p-10 text-center">
      <p className="text-4xl font-semibold text-ink-faint">404</p>
      <p className="mt-2 text-sm text-ink-muted">This page does not exist.</p>
      <Link to="/dashboard" className="focus-ring mt-6 inline-block rounded-lg bg-surface-800 px-4 py-2 text-sm text-ink hover:bg-surface-700">
        Go to the Dashboard
      </Link>
    </section>
  )
}
