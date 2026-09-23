import type { TaskView } from '../api/types'
import { StatusBadge } from './StatusBadge'

const EMPTY = '—'

function formatDate(iso: string | null): string {
  if (!iso) return EMPTY
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return EMPTY
  return d.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })
}

export function TaskRow({ task }: { task: TaskView }) {
  const running = task.status === 'running'
  return (
    <tr data-testid={`task-row-${task.issue_number}`} data-status={task.status}>
      <td>
        <a href={task.issue_url} target="_blank" rel="noreferrer" data-testid="issue-link">
          #{task.issue_number}
        </a>
      </td>
      <td>{task.title}</td>
      <td>
        <StatusBadge status={task.status} />
      </td>
      <td>
        {task.session_url ? (
          <a href={task.session_url} target="_blank" rel="noreferrer" data-testid="session-link">
            {running && <span className="pulse" aria-hidden="true" />}
            {task.session_id ? `${task.session_id.slice(0, 8)}…` : 'View'}
          </a>
        ) : (
          EMPTY
        )}
      </td>
      <td>
        {task.pr_url ? (
          <a href={task.pr_url} target="_blank" rel="noreferrer" data-testid="pr-link">
            View PR
          </a>
        ) : (
          EMPTY
        )}
      </td>
      <td>{formatDate(task.created_at)}</td>
      <td>{formatDate(task.updated_at)}</td>
    </tr>
  )
}
