# Demo: SonarQube via MCP vs. Devin native code scans

This repo is the target for a side-by-side demo of two ways to feed findings into a
Devin remediation loop:

- **Leg 1** — an external analyzer (SonarQube / SonarCloud) queried by Devin over MCP.
- **Leg 2** — Devin's own native code-scanning capability.

Both legs run against the same branch, `demo/seeded-issues`, which adds
`app/demo_helpers.py` with a handful of small, deliberate, scan-detectable issues
(an unused import, `subprocess.run(..., shell=True)`, a bare `except:`, and a
hardcoded credential string). Nothing else in the repo differs from `main`.

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
- **A reachable SonarQube or SonarCloud instance** that has analyzed this repo
  (including the `demo/seeded-issues` branch), plus a user/analysis token with
  permission to browse issues. Leg 1 needs these as Devin secrets (below).
- Devin must have write access to the repo so it can push branches and open PRs.
- The branch `demo/seeded-issues` exists and is not merged. Re-create it from `main`
  (re-adding `app/demo_helpers.py`) if remediation PRs have already been merged into it.

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

Make sure the SonarQube project has a branch analysis for `demo/seeded-issues`
(run the scanner with `-Dsonar.branch.name=demo/seeded-issues`, or enable automatic
branch analysis on SonarCloud). In the automation prompt below, replace
`<SONAR_PROJECT_KEY>` with the project key shown in SonarQube.

### 2. Automation prompt

Create a Devin automation (scheduled, or triggered manually for the demo) with the
repo `dsalvadorjasin/devin-remediation-service` and this prompt:

```
You are the remediation step of a SonarQube -> Devin pipeline. SonarQube only reports
findings; you produce the fixes.

1. Using the SonarQube MCP server, list OPEN issues for project <SONAR_PROJECT_KEY>
   on branch `demo/seeded-issues`. Restrict to files under `app/` and ignore anything
   in `frontend/`, `e2e/`, `alembic/versions/`.
2. Triage: keep findings that are real and safely auto-fixable (unused imports,
   bare `except:`, `subprocess` with `shell=True`, hardcoded credentials/secrets,
   and similar). For anything that is a false positive or needs a design decision,
   do NOT change code — just list it in your final summary with a one-line reason.
3. Check out `demo/seeded-issues` and fix the kept findings with minimal, focused
   edits. Do not touch `app/api/ingest.py`, existing tests, or GitHub workflows.
   For hardcoded credentials, read the value from an environment variable instead
   and do not commit any real secret.
4. Run `uv run ruff check .` and `uv run pytest`; both must pass. If ruff still fails
   on `app/demo_helpers.py` for something the detector did not report, fix that too but
   list it in the PR under "Not reported by the detector" so coverage gaps are visible.
5. Open ONE pull request against `demo/seeded-issues` on a branch named
   `devin/sonar-fix-<short-description>`. In the PR description, list each SonarQube
   rule key you fixed (e.g. python:S1481) with the file/line and a one-line
   explanation, and state that the findings came from SonarQube via MCP.
6. If SonarQube reports zero open issues on the branch, do nothing and say so.
```

## Leg 2 — Native code scans

No external service, MCP server, or secret is required — only that code scanning is
enabled for the repo in the Devin app.

Create a second Devin automation with the same repo and this prompt:

```
You are the remediation step of a Devin-code-scan -> Devin pipeline. The scan only
reports findings; you produce the fixes.

1. Run a Devin code scan (security + code quality) on
   dsalvadorjasin/devin-remediation-service, branch `demo/seeded-issues`, scoped to
   files under `app/`. If a scan for this branch already exists, refresh it for new
   commits instead of creating a duplicate.
2. Triage the findings: keep those that are real and safely auto-fixable (unused
   imports, bare `except:`, `subprocess` with `shell=True`, hardcoded
   credentials/secrets, and similar). For anything that is a false positive or needs a
   design decision, do NOT change code — list it in your final summary with a one-line
   reason.
3. Check out `demo/seeded-issues` and fix the kept findings with minimal, focused
   edits. Do not touch `app/api/ingest.py`, existing tests, or GitHub workflows.
   For hardcoded credentials, read the value from an environment variable instead
   and do not commit any real secret.
4. Run `uv run ruff check .` and `uv run pytest`; both must pass. If ruff still fails
   on `app/demo_helpers.py` for something the detector did not report, fix that too but
   list it in the PR under "Not reported by the detector" so coverage gaps are visible.
5. Open ONE pull request against `demo/seeded-issues` on a branch named
   `devin/scan-fix-<short-description>`. In the PR description, list each finding you
   fixed with the file/line and a one-line explanation, and state that the findings
   came from a Devin native code scan.
6. If the scan reports zero findings on the branch, do nothing and say so.
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

## Lint baseline on the seeded branch

`app/demo_helpers.py` deliberately fails `uv run ruff check .` (F401 unused import, E722
bare `except`), so CI lint is red on `demo/seeded-issues` until a remediation PR lands.
Both prompts require green ruff before opening a PR; if a detector misses one of these,
the session fixes it anyway and calls it out as "Not reported by the detector" — a useful
data point for the comparison rather than a blocker.

## Expected outcome

Each leg opens one PR against `demo/seeded-issues` that removes the unused `os`
import, replaces the `shell=True` call with an argument list, narrows the bare
`except:` to the exceptions actually raised, and moves `DEMO_API_TOKEN` to an
environment variable. Comparing the two PRs side by side shows the same fixer
producing near-identical patches from two different detectors.

## Scope notes

- The seeded issues live only on `demo/seeded-issues`; `main` is unchanged apart
  from this document.
- This demo is independent of the remediation service's own ingest pipeline
  (`app/api/ingest.py`, `POST /ingest/semgrep`); nothing is wired into it.
