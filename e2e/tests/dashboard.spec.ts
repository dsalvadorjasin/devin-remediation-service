import { expect, test, type Page } from '@playwright/test'
import {
  COMPLETED_TASK,
  EMPTY_TASKS,
  FAILED_TASK,
  POPULATED_TASKS,
  RUNNING_TASK,
  type TaskView,
} from '../fixtures/tasks'

const STATUS_ROUTE = '**/status'

async function mockStatus(page: Page, body: TaskView[]) {
  await page.route(STATUS_ROUTE, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }),
  )
}

async function expectSummary(page: Page, running: number, completed: number, failed: number) {
  await expect(page.getByTestId('summary-running')).toHaveText(String(running))
  await expect(page.getByTestId('summary-completed')).toHaveText(String(completed))
  await expect(page.getByTestId('summary-failed')).toHaveText(String(failed))
}

test.describe('dashboard', () => {
  test('initial empty state', async ({ page }, testInfo) => {
    await mockStatus(page, EMPTY_TASKS)
    await page.goto('/')

    await expect(page.getByTestId('dashboard')).toBeVisible()
    await expect(page.getByTestId('empty-state')).toBeVisible()
    await expect(page.getByTestId('empty-state')).toContainText('No issues tracked yet')
    await expectSummary(page, 0, 0, 0)
    await expect(page.getByTestId('task-table').locator('tbody [data-testid^="task-row-"]')).toHaveCount(0)

    await page.screenshot({ path: testInfo.outputPath('empty.png'), fullPage: true })
  })

  test('populated table', async ({ page }, testInfo) => {
    await mockStatus(page, POPULATED_TASKS)
    await page.goto('/')

    await expectSummary(page, 1, 1, 1)
    await expect(page.getByTestId('empty-state')).toHaveCount(0)

    const rows = page.getByTestId('task-table').locator('tbody [data-testid^="task-row-"]')
    await expect(rows).toHaveCount(POPULATED_TASKS.length)

    const ids = await rows.evaluateAll((els) => els.map((el) => el.getAttribute('data-testid')))
    expect(ids).toEqual(POPULATED_TASKS.map((t) => `task-row-${t.issue_number}`))

    for (const task of POPULATED_TASKS) {
      const row = page.getByTestId(`task-row-${task.issue_number}`)
      await expect(row).toHaveAttribute('data-status', task.status)
      const badge = row.getByTestId('status-badge')
      await expect(badge).toHaveText(task.status)
      await expect(badge).toHaveAttribute('data-status', task.status)
    }

    await page.screenshot({ path: testInfo.outputPath('populated.png'), fullPage: true })
  })

  test('links', async ({ page }, testInfo) => {
    await mockStatus(page, POPULATED_TASKS)
    await page.goto('/')
    await expectSummary(page, 1, 1, 1)

    for (const task of POPULATED_TASKS) {
      const row = page.getByTestId(`task-row-${task.issue_number}`)
      const issueLink = row.getByTestId('issue-link')
      await expect(issueLink).toHaveAttribute('href', task.issue_url)
      await expect(issueLink).toHaveAttribute('target', '_blank')
      await expect(issueLink).toHaveText(`#${task.issue_number}`)
    }

    for (const task of [RUNNING_TASK, COMPLETED_TASK]) {
      const sessionLink = page.getByTestId(`task-row-${task.issue_number}`).getByTestId('session-link')
      await expect(sessionLink).toHaveAttribute('href', task.session_url!)
      await expect(sessionLink).toHaveAttribute('target', '_blank')
    }
    await expect(page.getByTestId(`task-row-${FAILED_TASK.issue_number}`).getByTestId('session-link')).toHaveCount(0)

    const prLink = page.getByTestId(`task-row-${COMPLETED_TASK.issue_number}`).getByTestId('pr-link')
    await expect(prLink).toHaveAttribute('href', COMPLETED_TASK.pr_url!)
    await expect(prLink).toHaveAttribute('target', '_blank')
    await expect(prLink).toHaveText('View PR')
    for (const task of [RUNNING_TASK, FAILED_TASK]) {
      await expect(page.getByTestId(`task-row-${task.issue_number}`).getByTestId('pr-link')).toHaveCount(0)
    }

    await page.screenshot({ path: testInfo.outputPath('links.png'), fullPage: true })
  })

  test('auto-refresh', async ({ page }, testInfo) => {
    // Serve [] until the empty state has been asserted, then populated data on
    // every subsequent poll. (A call counter is not reliable: React StrictMode
    // in dev double-invokes effects, producing two initial fetches.)
    let populated = false
    let calls = 0
    await page.route(STATUS_ROUTE, (route) => {
      calls += 1
      const body = populated ? POPULATED_TASKS : EMPTY_TASKS
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    })
    await page.goto('/')

    await expect(page.getByTestId('empty-state')).toBeVisible()
    await expectSummary(page, 0, 0, 0)
    const lastUpdated = page.getByTestId('last-updated')
    await expect(lastUpdated).toContainText('Updated')
    const before = (await lastUpdated.textContent()) ?? ''
    await page.screenshot({ path: testInfo.outputPath('before-refresh.png'), fullPage: true })

    const callsBefore = calls
    populated = true

    // No reload: the SPA polls every POLL_MS (5s); allow slack for the next tick.
    const rows = page.getByTestId('task-table').locator('tbody [data-testid^="task-row-"]')
    await expect(rows).toHaveCount(POPULATED_TASKS.length, { timeout: 10_000 })
    await expect(page.getByTestId('empty-state')).toHaveCount(0)
    await expectSummary(page, 1, 1, 1)
    await expect(lastUpdated).not.toHaveText(before, { timeout: 10_000 })
    expect(calls).toBeGreaterThan(callsBefore)

    await page.screenshot({ path: testInfo.outputPath('after-refresh.png'), fullPage: true })
  })
})
