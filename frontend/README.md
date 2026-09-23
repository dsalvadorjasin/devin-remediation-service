# Devin Remediation Dashboard (React SPA)

Vite + React + TypeScript single-page app that renders the remediation
dashboard from the read API. It is served separately from the FastAPI
service and consumes **only** the frozen read-API contract:

- `contracts/read-api.openapi.yaml` (OpenAPI source of truth)
- `src/api/types.ts` (TypeScript mirror imported by every component)

```bash
npm install
npm run dev      # http://localhost:5173, proxies /status + /healthz to API_PROXY_TARGET (default http://localhost:8000)
npm run build    # static bundle in dist/
npm run lint
```

## Structure

- `src/App.tsx` — page shell: header + last-updated label, error banner, summary, table
- `src/hooks/usePolledStatus.ts` — fetches `/status` immediately and every `POLL_MS`, aborts
  in-flight requests on unmount, keeps the last good rows on error
- `src/components/` — `SummaryCards`, `TaskTable`, `TaskRow`, `StatusBadge`
- `src/App.css` — plain CSS ported from the legacy `app/templates/index.html`

All `data-testid` hooks used by the e2e suite live in the components above
(`dashboard`, `summary-*`, `task-table`, `task-row-<n>`, `status-badge`,
`issue-link`, `session-link`, `pr-link`, `empty-state`, `last-updated`, `error-banner`).
