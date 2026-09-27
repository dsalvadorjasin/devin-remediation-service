# Demo: SonarQube via MCP vs. Devin native code scans

This repo is the target for a side-by-side demo of two ways to feed findings into a
Devin remediation loop:

- **Leg 1** — an external analyzer (SonarQube / SonarCloud) queried by Devin over MCP.
- **Leg 2** — Devin's own native code-scanning capability.

Both legs run against `main` **as is** — no seeded issues. SonarQube Cloud's Free plan
analyzes only the main branch (see
[Subscription plans](https://docs.sonarsource.com/sonarqube-cloud/administering-sonarcloud/managing-subscription/subscription-plans)),
and `main` already carries enough real, low-risk findings for a demo (at the time of
writing SonarCloud reports ~80 open issues: unpinned action SHAs and image tags in
`.github/workflows/` and `k8s/`, `logging.error` → `logging.exception`, undocumented
`HTTPException` responses, a duplicated string literal, a couple of cognitive-complexity
hotspots, etc.).

## Key framing: Devin is always the remediator

In **both** legs Devin does the fixing. Neither SonarQube nor a Devin code scan
produces patches — they produce **findings**. The only variable being compared is the
**detection source**; the triage-and-fix loop (a Devin session that reads the finding,
edits the code, runs lint/tests, opens a PR) is identical.

```
Leg 1:  SonarQube detects ──MCP──> Devin session triages + fixes ──> PR
Leg 2:  Devin native scan detects ──> Devin session triages + fixes ──> PR
```

SonarQube ships AI CodeFix-style suggestions in some editions. The demo deliberately
does **not** use them: SonarQube is used only as a findings source so the two legs are
apples-to-apples (same fixer, different detector).

## Prerequisites

These are configured on the Devin app / SonarQube side, **not** in this repo:

- **Native code scans** must be enabled for `dsalvadorjasin/devin-remediation-service`
  in the Devin app (Code Scans section). Leg 2 needs nothing else.
- **A reachable SonarQube or SonarCloud instance** that has analyzed `main` of this
  repo, plus a user token with permission to browse issues. Leg 1 needs these as Devin
  secrets (below). On the SonarQube Cloud Free plan only `main` (and PRs targeting
  `main`) are analyzed, which is why the demo targets `main` rather than a feature branch.
- Devin must have write access to the repo so it can push branches and open PRs.

## Leg 1 — External analysis via MCP (SonarQube)

### 1. Configure the SonarQube MCP server in Devin

Install the **SonarQube** plugin from the Devin marketplace
(`https://github.com/CognitionAI/devin-marketplace#plugins/sonarqube`). It runs the
official `mcp/sonarqube` server (SonarSource's `sonarqube-mcp-server`) and expects
these secrets in Devin:

| Secret            | Value                                                                  |
|-------------------|------------------------------------------------------------------------|
| `SONARQUBE_URL`   | `https://sonarcloud.io` for SonarCloud, or your self-hosted server URL |
| `SONARQUBE_TOKEN` | A user token with *Browse* permission on the project                   |
| `SONARQUBE_ORG`   | SonarCloud organization key (leave empty for self-hosted SonarQube)    |

Alternatively, add a custom MCP server in *Settings → MCP* pointing at the same
image with the same three environment variables.

Make sure the SonarQube project has a recent analysis of `main` (SonarCloud automatic
analysis re-analyzes `main` on every push). The project key for this repo on SonarCloud
is `dsalvadorjasin_devin-remediation-service`; it is used verbatim in the prompt below.

### 2. Automation prompt

Create a Devin automation (scheduled, or triggered manually for the demo) with the
repo `dsalvadorjasin/devin-remediation-service` and this prompt:

```
You are the remediation step of a SonarQube -> Devin pipeline. SonarQube only reports
findings; you produce the fixes.

1. Using the SonarQube MCP server (search_sonar_issues_in_projects), list OPEN issues
   for project dsalvadorjasin_devin-remediation-service on branch `main`. Restrict to
   files under `app/` (Python only) and ignore `frontend/`, `e2e/`, `alembic/versions/`,
   `k8s/`, Dockerfiles and `.github/workflows/`.
2. Triage: pick at most 5 findings that are real and safely auto-fixable with a local,
   behaviour-preserving edit (e.g. `logging.error` -> `logging.exception` inside an
   except block, duplicated string literals -> a constant, documenting HTTPException
   responses, small readability fixes). Skip refactors that change behaviour or public
   interfaces (e.g. cognitive-complexity rewrites) and anything that looks like a false
   positive — list those in your final summary with a one-line reason instead.
3. Check out `main` and fix the chosen findings with minimal, focused edits. Do not
   touch existing tests or GitHub workflows.
4. Run `uv run ruff check .` and `uv run pytest`; both must pass.
5. Open ONE pull request against `main` on a branch named
   `devin/sonar-fix-<short-description>`. In the PR description, list each SonarQube
   issue you fixed (rule key, e.g. python:S8572, file/line, one-line explanation) and
   state that the findings came from SonarQube via MCP.
6. If SonarQube reports zero open issues matching step 1, do nothing and say so.
```

## Leg 2 — Native code scans

No external service, MCP server, or secret is required — only that code scanning is
enabled for the repo in the Devin app.

Create a second Devin automation with the same repo and this prompt:

```
You are the remediation step of a Devin-code-scan -> Devin pipeline. The scan only
reports findings; you produce the fixes.

1. Run a Devin code scan (security + code quality) on
   dsalvadorjasin/devin-remediation-service, branch `main`, scoped to Python files
   under `app/`. If a scan for `main` already exists, refresh it for new commits
   instead of creating a duplicate.
2. Triage: pick at most 5 findings that are real and safely auto-fixable with a local,
   behaviour-preserving edit. Skip refactors that change behaviour or public
   interfaces and anything that looks like a false positive — list those in your
   final summary with a one-line reason instead.
3. Check out `main` and fix the chosen findings with minimal, focused edits. Do not
   touch existing tests or GitHub workflows.
4. Run `uv run ruff check .` and `uv run pytest`; both must pass.
5. Open ONE pull request against `main` on a branch named
   `devin/scan-fix-<short-description>`. In the PR description, list each finding you
   fixed with the file/line and a one-line explanation, and state that the findings
   came from a Devin native code scan.
6. If the scan reports zero findings matching step 1, do nothing and say so.
```

## Comparison

| | Leg 1: SonarQube + MCP | Leg 2: Devin native scan |
|---|---|---|
| Detection source | External SonarQube/SonarCloud analysis | Devin's built-in code scan |
| Infrastructure | SonarQube server or SonarCloud project, scanner in CI, branch analysis | None beyond the Devin app |
| Configuration in Devin | MCP server + `SONARQUBE_URL` / `SONARQUBE_TOKEN` / `SONARQUBE_ORG` secrets | Enable code scanning for the repo |
| Secrets to manage / rotate | SonarQube token | None |
| Dashboards & governance | SonarQube UI, quality gates, rule profiles, history, PR decoration | Devin scan results in the Devin app |
| Finding vocabulary | SonarQube rule keys (e.g. `python:S1481`) | Devin scan findings |
| Remediation loop | Devin session triages + fixes + opens PR | Identical |
| Best fit | Teams already standardized on SonarQube who want Devin to act on its backlog | Teams wanting zero external dependencies |

The remediation half of the loop is the same in both legs; the trade-off is entirely
about whether you want an external analyzer's ecosystem (dashboards, quality gates,
existing rule sets) at the cost of running and authenticating against it.

## Expected outcome

Each leg opens one small PR against `main` fixing a handful of real findings from its
detector. Comparing the two PRs side by side shows the same fixer producing similar
patches from two different detectors — and where the detectors disagree on what to
flag, which is itself part of the comparison. Both prompts cap the fix at 5 findings so
the PRs stay reviewable; re-run the automations to work through the backlog.

## Scope notes

- Both legs read `main` and open PRs against `main`; nothing is seeded and nothing in
  the repo is changed by the demo setup itself apart from this document.
- This demo is independent of the remediation service's own ingest pipeline
  (`app/api/ingest.py`, `POST /ingest/semgrep`); nothing is wired into it.
