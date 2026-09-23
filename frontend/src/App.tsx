import './App.css'
import { POLL_MS } from './api/types'
import { SummaryCards } from './components/SummaryCards'
import { TaskTable } from './components/TaskTable'
import { usePolledStatus } from './hooks/usePolledStatus'

export default function App() {
  const { rows, lastUpdated, error } = usePolledStatus(POLL_MS)

  return (
    <main data-testid="dashboard">
      <header>
        <h1>
          Devin <span>Remediation</span> Dashboard
        </h1>
        <div className="controls">
          <span className="last-updated" data-testid="last-updated">
            {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : '—'}
          </span>
        </div>
      </header>

      {error && (
        <div className="error-banner" role="alert" data-testid="error-banner">
          Failed to refresh: {error.message}
          {lastUpdated && ' — showing last known data.'}
        </div>
      )}

      <SummaryCards rows={rows} />
      <TaskTable rows={rows} />
    </main>
  )
}
