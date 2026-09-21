"""
Semgrep discovery source.

- `parse_sarif(sarif)` turns a SARIF 2.1.0 document (from `semgrep --sarif`
  or the Semgrep GitHub Action) into `Finding`s. Used by both the ingest
  endpoint (SARIF pushed to us) and `discover()` (we run Semgrep ourselves).
- `SemgrepDiscoverySource.discover()` clones/updates a checkout of
  `GITHUB_REPO` (Contents: read) and runs `semgrep scan --sarif` on it.
"""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from app.discovery.base import DiscoverySource, Finding

log = logging.getLogger(__name__)

DEFAULT_CONFIG = "p/default"
DEFAULT_CHECKOUT_DIR = "/tmp/remediation-checkout"

_LEVEL_TO_SEVERITY = {"error": "HIGH", "warning": "MEDIUM", "note": "LOW", "none": "INFO"}


def _severity(result: dict, rule: dict | None) -> str:
    props = (rule or {}).get("properties", {}) if rule else {}
    for key in ("security-severity", "severity"):
        if props.get(key):
            return str(props[key]).upper()
    level = result.get("level") or (rule or {}).get("defaultConfiguration", {}).get("level")
    return _LEVEL_TO_SEVERITY.get(level or "warning", "MEDIUM")


def parse_sarif(sarif: dict) -> list[Finding]:
    findings: list[Finding] = []
    for run in sarif.get("runs", []):
        rules = {
            r.get("id"): r
            for r in run.get("tool", {}).get("driver", {}).get("rules", [])
        }
        for result in run.get("results", []):
            rule_id = result.get("ruleId") or ""
            rule = rules.get(rule_id)
            locations = result.get("locations") or []
            phys = (locations[0].get("physicalLocation") if locations else None) or {}
            file_path = phys.get("artifactLocation", {}).get("uri", "") or ""
            region = phys.get("region", {}) or {}
            start_line = int(region.get("startLine") or 0)
            snippet = (region.get("snippet") or {}).get("text", "") or ""
            message = (result.get("message") or {}).get("text", "") or ""
            help_uri = (rule or {}).get("helpUri", "") if rule else ""
            fingerprints = result.get("fingerprints") or {}
            # Prefer SARIF-provided stable fingerprints (Semgrep emits
            # matchBasedId/v1); fall back to our own hash.
            fp = ""
            for key in ("matchBasedId/v1", "primaryLocationLineHash", *sorted(fingerprints)):
                if fingerprints.get(key):
                    fp = str(fingerprints[key])[:32]
                    break
            findings.append(
                Finding(
                    rule_id=rule_id,
                    message=message,
                    severity=_severity(result, rule),
                    file_path=file_path,
                    start_line=start_line,
                    snippet=snippet,
                    fingerprint=fp,
                    help_uri=help_uri,
                )
            )
    return findings


class SemgrepDiscoverySource(DiscoverySource):
    name = "semgrep"

    def __init__(
        self,
        repo: str | None = None,
        checkout_dir: str | None = None,
        config: str | None = None,
        token: str | None = None,
        max_findings: int | None = None,
    ):
        self.repo = repo or os.environ["GITHUB_REPO"]
        self.checkout_dir = Path(checkout_dir or os.getenv("SEMGREP_CHECKOUT_DIR", DEFAULT_CHECKOUT_DIR))
        self.config = config or os.getenv("SEMGREP_CONFIG", DEFAULT_CONFIG)
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN", "")
        self.max_findings = max_findings or int(os.getenv("SEMGREP_MAX_FINDINGS", "0")) or None

    def _clone_url(self) -> str:
        auth = f"x-access-token:{self.token}@" if self.token else ""
        return f"https://{auth}github.com/{self.repo}.git"

    def update_checkout(self) -> Path:
        if (self.checkout_dir / ".git").exists():
            log.info("Updating checkout of %s in %s", self.repo, self.checkout_dir)
            subprocess.run(["git", "-C", str(self.checkout_dir), "fetch", "--depth", "1", "origin"], check=True)
            subprocess.run(["git", "-C", str(self.checkout_dir), "reset", "--hard", "origin/HEAD"], check=True)
        else:
            if self.checkout_dir.exists():
                shutil.rmtree(self.checkout_dir)
            log.info("Cloning %s into %s", self.repo, self.checkout_dir)
            subprocess.run(
                ["git", "clone", "--depth", "1", self._clone_url(), str(self.checkout_dir)],
                check=True,
            )
        return self.checkout_dir

    def run_semgrep(self, target: Path) -> dict:
        semgrep = shutil.which("semgrep") or "semgrep"
        cmd = [
            semgrep,
            "scan",
            "--config",
            self.config,
            "--sarif",
            "--quiet",
            "--metrics=off",
            "--disable-version-check",
            str(target),
        ]
        log.info("Running: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(target))
        if proc.returncode not in (0, 1):  # 1 = findings present
            raise RuntimeError(f"semgrep failed ({proc.returncode}): {proc.stderr[-2000:]}")
        return json.loads(proc.stdout)

    def discover(self) -> list[Finding]:
        target = self.update_checkout()
        findings = parse_sarif(self.run_semgrep(target))
        prefix = str(target).rstrip("/") + "/"
        findings = [
            Finding(**{**f.to_dict(), "file_path": f.file_path.removeprefix(prefix).removeprefix("file://")})
            for f in findings
        ]
        if self.max_findings:
            findings = findings[: self.max_findings]
        log.info("Semgrep produced %d finding(s)", len(findings))
        return findings
