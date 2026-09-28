/**
 * Live-backend dashboard e2e: same data-testid assertions as dashboard.spec.ts,
 * but against a deployed stack (no page.route mocks). The SPA's GET /status
 * goes through the frontend container's nginx -> API_UPSTREAM -> api.
 *
 * Opt-in: E2E_LIVE_API=1. scripts/verify-ecs-local.sh runs it against the
 * ECS-emulated stack. State is seeded through the real ingest path
 * (POST /ingest/semgrep with INGEST_TOKEN); GitHub/Devin are the in-network
 * stub from compose.e2e-stubs.yml, which finishes one session with a PR
 * (-> completed) and fails the one whose rule id contains "stub-devin-error".
 *
 *   E2E_LIVE_API=1        enable
 *   E2E_API_URL           API base for seeding (default http://localhost:8000)
 *   E2E_INGEST_TOKEN      bearer token for /ingest/semgrep (falls back to INGEST_TOKEN)
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import type { TaskStatus, TaskView } from '../../frontend/src/api/types'

const LIVE = !!process.env.E2E_LIVE_API
const API_URL = process.env.E2E_API_URL ?? 'http://localhost:8000'
const INGEST_TOKEN = process.env.E2E_INGEST_TOKEN ?? process.env.INGEST_TOKEN ?? ''
const FAIL_MARKER = 'stub-devin-error'
// One Devin poll hop is 60s (app.orchestrator.base.POLL_INTERVAL_SECONDS).
const OUTCOME_TIMEOUT_MS = 150_000

interface Seeded {
  completes: number
  fails: number
  issueUrls: Record<number, string>
}

let seeded: Seeded | null = null

function sarif(runId: string) {
  const result = (ruleId: string, uri: string, line: number, text: string) => ({
    ruleId,
    level: 'error',
    message: { text },
    locations: [
      {
        physicalLocation: {
          artifactLocation: { uri },
          region: { startLine: line, snippet: { text: `demo_${runId} = ${line}` } },
        },
      },
    ],
    fingerprints: { 'matchBasedId/v1': `ecslocal-${runId}-${line}` },
  })
  return {
    version: '2.1.0',
    runs: [
      {
        tool: { driver: { name: 'Semgrep OSS', rules: [] } },
        results: [
          result('ecs-local.demo.hardcoded-credential', 'app/settings.py', 12, 'ECS-local e2e seed: completes with a PR'),
          result(`ecs-local.demo.${FAIL_MARKER}`, 'app/payments.py', 34, 'ECS-local e2e seed: Devin session errors'),
        ],
      },
    ],
  }
}

async function liveStatus(request: APIRequestContext): Promise<TaskView[]> {
  // Through the frontend's nginx proxy, i.e. the same path the SPA uses.
  const res = await request.get('/status')
  expect(res.ok()).toBeTruthy()
  return (await res.json()) as TaskView[]
}

function counts(rows: TaskView[]): Record<TaskStatus, number> {
  const c: Record<TaskStatus, number> = { running: 0, completed: 0, failed: 0 }
  for (const row of rows) c[row.status]++
  return c
}

async function expectSummary(page: Page, rows: TaskView[]) {
  const c = counts(rows)
  await expect(page.getByTestId('summary-running')).toHaveText(String(c.running))
  await expect(page.getByTestId('summary-completed')).toHaveText(String(c.completed))
  await expect(page.getByTestId('summary-failed')).toHaveText(String(c.failed))
}

async function openDashboard(page: Page) {
  const response = await page.goto('/')
  // Served by the deployed nginx container, not a Vite dev server.
  expect(response?.headers()['server'] ?? '').toContain('nginx')
  await expect(page.getByTestId('dashboard')).toBeVisible()
  await expect(page.getByTestId('last-updated')).toContainText('Updated')
}

const rowsLocator = (page: Page) => page.getByTestId('task-table').locator('tbody [data-testid^="task-row-"]')

test.describe('dashboard (live backend)', () => {
  test.skip(!LIVE, 'set E2E_LIVE_API=1 to run against a deployed stack')
  test.describe.configure({ mode: 'serial' })

  test('initial state mirrors real GET /status', async ({ page, request }, testInfo) => {
    await openDashboard(page)
    const rows = await liveStatus(request)
    await expectSummary(page, rows)
    await expect(rowsLocator(page)).toHaveCount(rows.length)
    if (rows.length === 0) {
      await expect(page.getByTestId('empty-state')).toBeVisible()
      await expect(page.getByTestId('empty-state')).toContainText('No issues tracked yet')
    } else {
      await expect(page.getByTestId('empty-state')).toHaveCount(0)
    }
    await page.screenshot({ path: testInfo.outputPath('live-initial.png'), fullPage: true })
  })

  test('auto-refresh picks up findings ingested via POST /ingest/semgrep', async ({ page, request }, testInfo) => {
    test.setTimeout(120_000)
    expect(INGEST_TOKEN, 'E2E_INGEST_TOKEN / INGEST_TOKEN must be set').not.toBe('')
    await openDashboard(page)
    const lastUpdated = page.getByTestId('last-updated')
    const before = (await lastUpdated.textContent()) ?? ''
    await page.screenshot({ path: testInfo.outputPath('live-before-ingest.png'), fullPage: true })

    const runId = Date.now().toString(36)
    const res = await request.post(`${API_URL}/ingest/semgrep`, {
      headers: { Authorization: `Bearer ${INGEST_TOKEN}` },
      data: sarif(runId),
      timeout: 60_000,
    })
    expect(res.status(), await res.text()).toBe(200)
    const body = (await res.json()) as { created: { number: number; html_url: string; fingerprint: string }[] }
    expect(body.created).toHaveLength(2)
    const [ok, bad] = body.created
    expect(bad.fingerprint).toContain('-34')
    seeded = {
      completes: ok.number,
      fails: bad.number,
      issueUrls: { [ok.number]: ok.html_url, [bad.number]: bad.html_url },
    }

    // No reload: the SPA polls /status; the ingest -> scan -> remediate chain
    // runs on ingest-worker and devin-worker.
    for (const n of [ok.number, bad.number]) {
      await expect(page.getByTestId(`task-row-${n}`)).toBeVisible({ timeout: 45_000 })
    }
    await expect(page.getByTestId('empty-state')).toHaveCount(0)
    await expect(lastUpdated).not.toHaveText(before, { timeout: 10_000 })
    await page.screenshot({ path: testInfo.outputPath('live-after-ingest.png'), fullPage: true })
  })

  test('populated table reflects worker outcomes', async ({ page, request }, testInfo) => {
    test.skip(!seeded, 'requires the ingest test')
    test.setTimeout(OUTCOME_TIMEOUT_MS + 60_000)
    const { completes, fails } = seeded!
    await openDashboard(page)

    await expect(page.getByTestId(`task-row-${completes}`)).toHaveAttribute('data-status', 'completed', {
      timeout: OUTCOME_TIMEOUT_MS,
    })
    await expect(page.getByTestId(`task-row-${fails}`)).toHaveAttribute('data-status', 'failed', {
      timeout: OUTCOME_TIMEOUT_MS,
    })

    const rows = await liveStatus(request)
    await expectSummary(page, rows)
    await expect(rowsLocator(page)).toHaveCount(rows.length)
    const ids = await rowsLocator(page).evaluateAll((els) => els.map((el) => el.getAttribute('data-testid')))
    expect(ids).toEqual(rows.map((t) => `task-row-${t.issue_number}`))

    for (const task of rows) {
      const row = page.getByTestId(`task-row-${task.issue_number}`)
      await expect(row).toHaveAttribute('data-status', task.status)
      const badge = row.getByTestId('status-badge')
      await expect(badge).toHaveText(task.status)
      await expect(badge).toHaveAttribute('data-status', task.status)
    }
    await page.screenshot({ path: testInfo.outputPath('live-populated.png'), fullPage: true })
  })

  test('links point at the real issue, session and PR URLs', async ({ page, request }, testInfo) => {
    test.skip(!seeded, 'requires the ingest test')
    const { completes, fails, issueUrls } = seeded!
    await openDashboard(page)
    const byNumber = new Map((await liveStatus(request)).map((t) => [t.issue_number, t]))
    const done = byNumber.get(completes)!
    const failed = byNumber.get(fails)!
    expect(done.status).toBe('completed')
    expect(failed.status).toBe('failed')
    await expectSummary(page, [...byNumber.values()])

    for (const task of [done, failed]) {
      expect(task.issue_url).toBe(issueUrls[task.issue_number])
      const issueLink = page.getByTestId(`task-row-${task.issue_number}`).getByTestId('issue-link')
      await expect(issueLink).toHaveAttribute('href', task.issue_url)
      await expect(issueLink).toHaveAttribute('target', '_blank')
      await expect(issueLink).toHaveText(`#${task.issue_number}`)

      // Both got a Devin session; only the one that errored has no PR.
      expect(task.session_url).toBeTruthy()
      const sessionLink = page.getByTestId(`task-row-${task.issue_number}`).getByTestId('session-link')
      await expect(sessionLink).toHaveAttribute('href', task.session_url!)
      await expect(sessionLink).toHaveAttribute('target', '_blank')
    }

    expect(done.pr_url).toBeTruthy()
    const prLink = page.getByTestId(`task-row-${completes}`).getByTestId('pr-link')
    await expect(prLink).toHaveAttribute('href', done.pr_url!)
    await expect(prLink).toHaveAttribute('target', '_blank')
    await expect(prLink).toHaveText('View PR')
    expect(failed.pr_url).toBeNull()
    await expect(page.getByTestId(`task-row-${fails}`).getByTestId('pr-link')).toHaveCount(0)

    await page.screenshot({ path: testInfo.outputPath('live-links.png'), fullPage: true })
  })
})
