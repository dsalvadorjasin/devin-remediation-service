import type { TaskStatus } from '../api/types'

export function StatusBadge({ status }: { status: TaskStatus }) {
  return (
    <span className={`badge badge-${status}`} data-testid="status-badge" data-status={status}>
      {status}
    </span>
  )
}
