"""
Discovery sources turn "something is wrong in the target repo" into
normalized `Finding`s that the ingest path can file as `devin-remediate`
issues. Semgrep is the first source; others (CodeQL, dependency audits,
production alerts) implement the same interface.
"""

import hashlib
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

FINGERPRINT_MARKER = "semgrep-fingerprint"


@dataclass(frozen=True)
class Finding:
    rule_id: str
    message: str
    severity: str
    file_path: str
    start_line: int
    snippet: str = ""
    fingerprint: str = field(default="")
    help_uri: str = ""
    source: str = "semgrep"

    def __post_init__(self):
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", self.compute_fingerprint())

    def compute_fingerprint(self) -> str:
        """Stable across re-runs as long as rule, file, line-neighbourhood and
        code stay the same. Line is normalized to a 10-line bucket so small
        shifts above the finding don't create a duplicate issue."""
        bucket = self.start_line // 10
        raw = f"{self.rule_id}|{self.file_path}|{bucket}|{' '.join(self.snippet.split())}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    @property
    def title(self) -> str:
        return f"[{self.source}] {self.rule_id}: {self.file_path}:{self.start_line}"

    @property
    def marker(self) -> str:
        return f"<!-- {FINGERPRINT_MARKER}: {self.fingerprint} -->"

    def issue_body(self) -> str:
        lines = [
            f"**Rule:** `{self.rule_id}`",
            f"**Severity:** {self.severity}",
            f"**Location:** `{self.file_path}:{self.start_line}`",
        ]
        if self.help_uri:
            lines.append(f"**Reference:** {self.help_uri}")
        lines += ["", "### Message", "", self.message.strip()]
        if self.snippet:
            lines += ["", "### Code", "", "```", self.snippet.rstrip(), "```"]
        lines += [
            "",
            "---",
            f"_Discovered automatically by the {self.source} discovery source. "
            "Fix the underlying issue and open a PR referencing this issue._",
            "",
            self.marker,
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)


class DiscoverySource(ABC):
    name: str = "unknown"

    @abstractmethod
    def discover(self) -> list[Finding]:
        """Run the analysis and return normalized findings."""
