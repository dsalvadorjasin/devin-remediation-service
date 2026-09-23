import { TASK_STATUSES, type TaskStatus, type TaskView } from '../api/types'

export function SummaryCards({ rows }: { rows: TaskView[] }) {
  const counts: Record<TaskStatus, number> = { running: 0, completed: 0, failed: 0 }
  for (const row of rows) {
    if (row.status in counts) counts[row.status]++
  }
  return (
    <div className="summary">
      {TASK_STATUSES.map((status) => (
        <div key={status} className={`card ${status}`}>
          <div className="count" data-testid={`summary-${status}`}>
            {counts[status]}
          </div>
          <div className="label">{status}</div>
        </div>
      ))}
    </div>
  )
}
