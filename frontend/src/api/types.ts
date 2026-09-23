/**
 * Read-API contract (frozen; mirrors contracts/read-api.openapi.yaml).
 * Every API access in the SPA must go through these types.
 * `poll_token` / `poll_lease_until` are internal and intentionally absent.
 */

export const TASK_STATUSES = ['running', 'completed', 'failed'] as const
export type TaskStatus = (typeof TASK_STATUSES)[number]

export interface TaskView {
  issue_number: number
  title: string
  issue_url: string
  session_id: string | null
  session_url: string | null
  status: TaskStatus
  pr_url: string | null
  /** ISO-8601 */
  created_at: string
  /** ISO-8601 */
  updated_at: string
}

/** GET /status -> TaskView[] sorted by issue_number ascending */
export type StatusResponse = TaskView[]

export interface HealthzResponse {
  ok: boolean
}

export const READ_API = {
  status: '/status',
  statusForIssue: (issueNumber: number) => `/status/${issueNumber}`,
  healthz: '/healthz',
} as const

/** Dashboard auto-refresh period (matches the legacy app/templates/index.html). */
export const POLL_MS = 5000
