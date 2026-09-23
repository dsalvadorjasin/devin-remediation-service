import { useEffect, useState } from 'react'
import { fetchStatus } from './api/client'
import type { TaskView } from './api/types'
import { POLL_MS } from './api/types'

/** Scaffold placeholder: Lane A replaces this with the full dashboard. */
export default function App() {
  const [rows, setRows] = useState<TaskView[]>([])

  useEffect(() => {
    let cancelled = false
    const tick = () => fetchStatus().then((r) => !cancelled && setRows(r)).catch(console.error)
    tick()
    const id = setInterval(tick, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  return (
    <main data-testid="dashboard">
      <h1>Devin Remediation Dashboard</h1>
      <p data-testid="row-count">{rows.length} tasks</p>
    </main>
  )
}
