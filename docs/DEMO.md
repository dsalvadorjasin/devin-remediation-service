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

- **Native code scans** must be enabled for the org in the Devin app. Leg 2 needs
  nothing else.
- **A reachable SonarQube or SonarCloud instance** that has analyzed `main` of this
  repo, plus a user token with permission to browse issues. Leg 1 needs these as Devin
  secrets (below). On the SonarQube Cloud Free plan only `main` (and PRs targeting
  `main`) are analyzed, which is why the demo targets `main` rather than a feature branch.
- Devin must have write access to the repo so it can push branches and open PRs, and
  GitHub **Issues** must be enabled on the repo (the automations are issue-triggered).

## How the automations are wired (both legs)

Both legs use the same trigger shape so the only difference is where the findings come
from:

```
human adds label to a GitHub issue ──> Devin Automation fires ──> Devin session
    reads findings (SonarQube MCP | Devin code scan) ──> triages, fixes <= 5 ──> PR
    ──> waits for CI ──> posts one summary comment (PR URL, CI, Fixed, Skipped) on the issue
```

| | Leg 1 | Leg 2 |
|---|---|---|
| Issue label that triggers a run | `sonar-remediate` | `devin-scan-remediate` |
| Findings source inside the session | SonarQube MCP server (`search_sonar_issues_in_projects`) | Devin's built-in code-scan tool (`list_findings`) |
| Remediation branch prefix | `devin/sonar-fix-` | `devin/scan-fix-` |
| Guardrails | 10 ACU per session, 3 runs/hour, 1 concurrent run, skips if a `devin/sonar-fix-*` PR is already open | Same, for `devin/scan-fix-*` |
| Keeping findings fresh | SonarCloud automatic analysis re-analyzes `main` on every push | A second automation re-scans new commits on every push to `main` |

Why an issue label and not something more automatic: SonarQube Cloud's Free plan does
not support webhooks, so an analysis-complete event cannot start Devin directly. A
labelled issue is the same manual "go" for both legs and gives the session a natural
place to report back. Automations need **GitHub events from public repositories**
enabled in the Devin app (*Settings → Connections → GitHub*) because this repo is public.

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

Make sure the SonarQube project has a recent analysis of `main` (SonarCloud automatic
analysis re-analyzes `main` on every push). On SonarCloud this repo's project key is
`dsalvadorjasin_devin-remediation-service`; a self-hosted server will have its own.

### 2. Automation

- **Trigger:** `github:issues`, action `labeled`, label `sonar-remediate`, repo
  `dsalvadorjasin/devin-remediation-service`; reply `post_response` (the session's final
  message is posted as an issue comment).
- **Tools:** the SonarQube MCP server, granted to the automation.
- **Limits:** 10 ACU per session, max 3 runs per hour, 1 concurrent run, no queue.
- **Prompt (summary):**
  1. Query the SonarQube MCP for open/confirmed issues on `main` of the project;
     honour any scope the issue body gives.
  2. Stop if an open `devin/sonar-fix-*` PR already exists.
  3. Triage bugs/vulnerabilities first, then code smells; pick at most 5 small, local,
     behaviour-preserving fixes. Skip false positives and anything that changes
     behaviour or needs a design decision.
     Before counting a finding, open the file on current `main` and confirm the
     reported code is still there; findings already fixed by an earlier PR are listed
     under Skipped as "already fixed on main" (neither detector marks findings
     resolved immediately after a fix merges).
  4. Do not modify existing tests, workflow logic (pinning SHAs is fine) or
     `app/api/ingest.py`.
  5. `uv run ruff check .` and `uv run pytest` must pass.
  6. Open one PR against `main` referencing the issue; list fixed and not-fixed findings
     by rule key / file:line; state that SonarQube was the detector and Devin the fixer.
  7. Send no interim messages; after CI finishes, send exactly one final message with the
     PR URL, CI status, a Fixed list and a Skipped list (this becomes the issue comment).

## Leg 2 — Native code scans

No external service, MCP server, or secret is required — only that code scanning is
enabled for the org in the Devin app.

### 1. Baseline scan and scan profile

A one-off **code-quality** scan of `main` was created (the closest analogue to
SonarQube's bug / code-smell rules). Its configuration lives in a reusable, org-owned
**scan profile** in Devin — not in this repo and not in the automation — that defines:

- **What to look for:** duplicated string literals, dead/unused imports and variables,
  exception-handling anti-patterns (bare `except`, `logging.error` inside `except`,
  swallowed exceptions), missing docstrings on public symbols, overly complex/long
  functions, mutable default arguments, unpinned GitHub Action refs. Prefer small,
  local, behaviour-preserving findings; do not report style nits already enforced by
  ruff.
- **Excluded paths:** `tests/**`, `app/api/ingest.py`, `**/*.lock`, `uv.lock`,
  `**/migrations/versions/**`.
- **Triage:** one finding per rule + file (no per-occurrence duplicates); correctness
  bugs before smells; bugs/exception-handling = medium/high, smells = low.
- **Communication:** Slack DM summary to the requester when a run finishes.

The baseline run reported 7 open findings on `main`. The profile is the Devin analogue
of a SonarQube quality profile: edit it to change what future runs flag; the scan and the
automations below keep referencing it.

### 2. Automations

**Remediation** — same shape as Leg 1:

- **Trigger:** `github:issues`, action `labeled`, label `devin-scan-remediate`, same
  repo; reply `post_response`.
- **Tools:** none — the session uses Devin's built-in code-scan tool to read the scan's
  open findings.
- **Run as:** the automation's creator. The organization identity does not hold the
  code-scan view permission, so a run-as-organization session gets HTTP 403 when
  listing findings.
- **Limits:** 10 ACU per session, max 3 runs per hour, 1 concurrent run, no queue.
- **Prompt (summary):** identical to Leg 1 except step 1 reads the open findings of the
  baseline scan with `list_findings` (never the scan's own auto-remediation action, so
  the fixer stays a plain Devin session in both legs), the branch prefix is
  `devin/scan-fix-`, and findings are listed by title / file:line.

**Re-scan on push** — keeps findings current, analogous to SonarCloud's automatic
analysis:

- **Trigger:** `github:push` with `ref` = `refs/heads/main` on the same repo.
- **Action:** `scan_new_commits` on the baseline scan (a diff run over the commits since
  the last completed run; findings accumulate on the same scan, using the profile above).
- **Limits:** max 2 runs per hour, 1 concurrent run, queue depth 1. No session is
  started, so no ACU cap is needed.

## Comparison

| | Leg 1: SonarQube + MCP | Leg 2: Devin native scan |
|---|---|---|
| Detection source | External SonarQube/SonarCloud analysis | Devin's built-in code-quality scan |
| Infrastructure | SonarQube server or SonarCloud project, scanner/automatic analysis, branch analysis | None beyond the Devin app |
| Configuration in Devin | MCP server + `SONARQUBE_URL` / `SONARQUBE_TOKEN` / `SONARQUBE_ORG` secrets | Enable code scanning; one scan profile |
| Secrets to manage / rotate | SonarQube token | None |
| Keeping findings fresh | SonarCloud re-analyzes `main` on push (Free plan: `main` only) | Push-to-`main` automation re-scans new commits |
| Event to start remediation | Issue label (Free plan has no webhooks) | Issue label (same, for symmetry) |
| Dashboards & governance | SonarQube UI, quality gates, rule profiles, history, PR decoration | Scan findings and profile in the Devin app; Slack summary per run |
| Finding vocabulary | SonarQube rule keys (e.g. `python:S1481`) | Devin finding titles + categories (e.g. `magic-values`, `convention-violation`) |
| Remediation loop | Devin session triages + fixes + opens PR | Identical |
| Best fit | Teams already standardized on SonarQube who want Devin to act on its backlog | Teams wanting zero external dependencies |

The remediation half of the loop is the same in both legs; the trade-off is entirely
about whether you want an external analyzer's ecosystem (dashboards, quality gates,
existing rule sets) at the cost of running and authenticating against it.

## Expected outcome

Labelling an issue in each leg opens one small PR against `main` fixing a handful of
real findings from that detector, and the session's summary (PR URL, CI status, Fixed /
Skipped lists) lands as a comment on the issue. Comparing the two PRs side by side shows
the same fixer producing similar patches from two different detectors — and where the
detectors disagree on what to flag, which is itself part of the comparison. Both prompts
cap the fix at 5 findings so the PRs stay reviewable; re-label an issue to work through
the backlog.

First runs, for reference:

| Leg | Trigger issue | Remediation PR |
|---|---|---|
| 1 — SonarQube via MCP | [#17](https://github.com/dsalvadorjasin/devin-remediation-service/issues/17) | [#18](https://github.com/dsalvadorjasin/devin-remediation-service/pull/18) (5 findings fixed) |
| 2 — Devin native scan | [#19](https://github.com/dsalvadorjasin/devin-remediation-service/issues/19) | [#20](https://github.com/dsalvadorjasin/devin-remediation-service/pull/20) (5 of 7 findings fixed) |

## Scope notes

- Both legs read `main` and open PRs against `main`; nothing is seeded and nothing in
  the repo is changed by the demo setup itself apart from this document.
- This demo is independent of the remediation service's own ingest pipeline
  (`app/api/ingest.py`, `POST /ingest/semgrep`); nothing is wired into it.
