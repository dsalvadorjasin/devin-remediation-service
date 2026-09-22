"""
Discovery sources turn "something is wrong in the target repo" into
normalized `Finding`s that the ingest path can file as `devin-remediate`
issues. Semgrep is the first source; others (CodeQL, dependency audits,
production alerts) implement the same interface.
"""

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

FINGERPRINT_MARKER = "semgrep-fingerprint"
# Upper bound for SARIF documents accepted by /ingest/semgrep.
MAX_SARIF_BYTES = 50 * 1024 * 1024
MAX_MESSAGE_CHARS = 4000
MAX_SNIPPET_CHARS = 4000

_MENTION = re.compile(r"(?<![\w`])@(?=\w)")


def inline_code(text: str) -> str:
    """Wrap untrusted text in inline code with a fence longer than any run of
    backticks it contains, so it can't break out of the code span."""
    text = " ".join(text.split())
    if "`" not in text:
        return f"`{text}`"
    fence = "`" * (max(len(m) for m in re.findall(r"`+", text)) + 1)
    return f"{fence} {text} {fence}"


def code_block(text: str, limit: int = MAX_SNIPPET_CHARS) -> list[str]:
    text = text.rstrip()[:limit]
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
    return [fence, text, fence]


def plain_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Untrusted prose: cap length and defuse @mentions (they'd notify real
    users from a trusted bot account)."""
    return _MENTION.sub("@\u200b", text.strip()[:limit])


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
        title = f"[{self.source}] {self.rule_id}: {self.file_path}:{self.start_line}"
        return " ".join(title.split())[:200]

    @property
    def marker(self) -> str:
        return f"<!-- {FINGERPRINT_MARKER}: {self.fingerprint} -->"

    def issue_body(self) -> str:
        lines = [
            f"**Rule:** {inline_code(self.rule_id)}",
            f"**Severity:** {inline_code(self.severity)}",
            f"**Location:** {inline_code(f'{self.file_path}:{self.start_line}')}",
        ]
        if self.help_uri and self.help_uri.startswith(("http://", "https://")):
            lines.append(f"**Reference:** {inline_code(self.help_uri)}")
        lines += ["", "### Message", "", plain_text(self.message)]
        if self.snippet:
            lines += ["", "### Code", "", *code_block(self.snippet)]
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
