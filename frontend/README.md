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
