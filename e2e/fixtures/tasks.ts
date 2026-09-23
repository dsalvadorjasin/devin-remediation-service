import type { TaskView } from '../../frontend/src/api/types'

export type { TaskView }

export const RUNNING_TASK: TaskView = {
  issue_number: 42,
  title: 'Fix flaky login test',
  issue_url: 'https://github.com/example/repo/issues/42',
  session_id: 'devin-abcdef1234567890',
  session_url: 'https://app.devin.ai/sessions/abcdef1234567890',
  status: 'running',
  pr_url: null,
  created_at: '2026-09-20T10:00:00Z',
  updated_at: '2026-09-20T10:05:00Z',
}

export const COMPLETED_TASK: TaskView = {
  issue_number: 57,
  title: 'Upgrade dependency versions',
  issue_url: 'https://github.com/example/repo/issues/57',
  session_id: 'devin-1234567890abcdef',
  session_url: 'https://app.devin.ai/sessions/1234567890abcdef',
  status: 'completed',
  pr_url: 'https://github.com/example/repo/pull/58',
  created_at: '2026-09-19T08:00:00Z',
  updated_at: '2026-09-19T09:30:00Z',
}

export const FAILED_TASK: TaskView = {
  issue_number: 61,
  title: 'Refactor payment module',
  issue_url: 'https://github.com/example/repo/issues/61',
  session_id: null,
  session_url: null,
  status: 'failed',
  pr_url: null,
  created_at: '2026-09-21T12:00:00Z',
  updated_at: '2026-09-21T12:01:00Z',
}

/** Sorted by issue_number ascending, as the read API guarantees. */
export const POPULATED_TASKS: TaskView[] = [RUNNING_TASK, COMPLETED_TASK, FAILED_TASK]

export const EMPTY_TASKS: TaskView[] = []
