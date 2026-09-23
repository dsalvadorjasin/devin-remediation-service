# e2e — Playwright suite for the Phase 6 dashboard SPA

Drives the React SPA in `../frontend` with Chromium. The read API (`GET /status`)
is **mocked** via `page.route` with fixtures from `fixtures/tasks.ts`, so no
FastAPI service, Postgres or Redis is needed.

## Run

```bash
cd e2e
npm ci
npx playwright install --with-deps chromium
npx playwright test          # or: npm test
npm run report               # open the HTML report
```

`playwright.config.ts` starts the SPA itself (`npm --prefix ../frontend run dev -- --port 5173`),
so run `npm ci` in `../frontend` first. An already-running dev server on :5173 is
reused outside CI.

## Artifacts

Every test records a screenshot and a video (`screenshot: 'on'`, `video: 'on'`);
traces are kept on failure.

- `e2e/test-results/<test>/` — named screenshots (`empty.png`, `populated.png`,
  `links.png`, `before-refresh.png`, `after-refresh.png`), `video.webm`, `trace.zip` on failure
- `e2e/playwright-report/` — HTML report (`npm run report`)

## Tests (`tests/dashboard.spec.ts`)

1. **initial empty state** — `/status -> []`: empty-state row, summary counts 0/0/0
2. **populated table** — three rows: counts 1/1/1, ascending issue order, status badges
3. **links** — issue/session/PR links (`href`, `target=_blank`, presence rules)
4. **auto-refresh** — `[]` then populated on the next poll; rows appear and
   `last-updated` changes without a reload

The suite asserts the shared `data-testid` DOM contract; against the Phase 6
scaffold most tests fail until Lane A lands the dashboard.
