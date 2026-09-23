import type { TaskView } from '../api/types'
import { TaskRow } from './TaskRow'

const COLUMNS = ['#', 'Issue', 'Status', 'Devin Session', 'Pull Request', 'Started', 'Updated']

export function TaskTable({ rows }: { rows: TaskView[] }) {
  return (
    <table data-testid="task-table">
      <thead>
        <tr>
          {COLUMNS.map((col) => (
            <th key={col}>{col}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <tr>
            <td colSpan={COLUMNS.length} className="empty" data-testid="empty-state">
              No issues tracked yet.
            </td>
          </tr>
        ) : (
          rows.map((task) => <TaskRow key={task.issue_number} task={task} />)
        )}
      </tbody>
    </table>
  )
}
