"""
In-network stand-in for api.github.com and api.devin.ai used by the
ECS-local live e2e run (compose.e2e-stubs.yml).

The app's GitHub/Devin clients have hard-coded https:// base URLs, so instead
of changing application code the stub container is given those hostnames as
network aliases, serves TLS with a throwaway CA, and the app containers trust
that CA via SSL_CERT_FILE. Nothing leaves the Docker network.

Behaviour (just enough of each API for the remediation loop):
- GitHub: issues are created/listed in memory; timeline is empty; open PRs are
  the ones "opened" by finished stub Devin sessions; comments/labels/hooks are
  accepted and discarded.
- Devin: create_session returns a stub session; the first poll finishes it.
  Issues whose title contains FAIL_MARKER end in status "error" (-> failed),
  all others in "exit" with a PR (-> completed).

Every request is recorded (time, host, method, path, status; never headers)
and served at GET /__stub/requests.
"""

import html
import itertools
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

CA_DIR = Path(os.getenv("STUB_CA_DIR", "/stub-ca"))
PORT = int(os.getenv("STUB_PORT", "443"))
HOSTNAMES = ["api.github.com", "github.com", "api.devin.ai", "app.devin.ai"]
FAIL_MARKER = "stub-devin-error"
FIRST_ISSUE_NUMBER = 4201
NOT_FOUND = "Not Found"

_lock = threading.RLock()
_issues: dict[tuple[str, int], dict] = {}
_pulls: dict[str, list[dict]] = {}
_sessions: dict[str, dict] = {}
_numbers: dict[str, itertools.count] = {}
_requests: list[dict] = []

REPO = r"/repos/(?P<owner>[^/]+)/(?P<name>[^/]+)"
SESSIONS = r"/v3/organizations/(?P<org>[^/]+)/sessions"
ISSUE = r"/issues/(?P<n>\d+)(?P<sub>/timeline|/comments|/labels)?"


def _next_number(repo: str) -> int:
    counter = _numbers.setdefault(repo, itertools.count(FIRST_ISSUE_NUMBER))
    return next(counter)


def _generate_certs(work: Path) -> tuple[Path, Path]:
    """Write the CA cert to CA_DIR and a server cert/key to `work`."""
    CA_DIR.mkdir(parents=True, exist_ok=True)
    (CA_DIR / "ready").unlink(missing_ok=True)
    ca_key, ca_pem = work / "ca.key", CA_DIR / "ca.pem"
    key, csr, crt = work / "server.key", work / "server.csr", work / "server.pem"
    ext = work / "san.ext"
    ext.write_text(
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=" + ",".join(f"DNS:{h}" for h in HOSTNAMES) + "\n"
    )

    def run(*args: str) -> None:
        subprocess.run(["openssl", *args], check=True, capture_output=True)

    run(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-subj",
        "/CN=ecs-local upstream stub CA",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_pem),
    )
    run(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=api.github.com",
        "-keyout",
        str(key),
        "-out",
        str(csr),
    )
    run(
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca_pem),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-days",
        "2",
        "-extfile",
        str(ext),
        "-out",
        str(crt),
    )
    return crt, key


def _html_url(repo: str, kind: str, number: int) -> str:
    return f"https://github.com/{quote(repo, safe='/')}/{kind}/{number}"


def _issue_view(repo: str, issue: dict) -> dict:
    return {
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "labels": [{"name": name} for name in issue["labels"]],
        "html_url": _html_url(repo, "issues", issue["number"]),
    }


def _finish_session(session: dict) -> None:
    session["polls"] += 1
    if session["status"] != "new":
        return
    issue = _issues.get((session["repo"], session["issue_number"]))
    title = issue["title"] if issue else ""
    if FAIL_MARKER in title:
        session["status"] = "error"
        return
    number = session["issue_number"]
    pr_number = number + 1000
    pr_url = _html_url(session["repo"], "pull", pr_number)
    session["status"] = "exit"
    session["pull_requests"] = [{"pr_url": pr_url}]
    _pulls.setdefault(session["repo"], []).append(
        {
            "number": pr_number,
            "title": f"Fix #{number}: {title}",
            "body": f"Closes #{number}",
            "state": "open",
            "html_url": pr_url,
        }
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "ecs-local-upstream-stub/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{fmt % args}\n")

    def _send(self, status: int, payload=None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with _lock:
            _requests.append(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "host": quote((self.headers.get("Host") or "").split(":")[0]),
                    "method": quote(self.command),
                    "path": quote(urlsplit(self.path).path),
                    "status": status,
                }
            )

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw or b"{}")

    def _dispatch(self) -> None:
        url = urlsplit(self.path)
        path, query = url.path.rstrip("/") or "/", parse_qs(url.query)
        host = (self.headers.get("Host") or "").split(":")[0]
        if path == "/__stub/requests":
            with _lock:
                return self._send(200, list(_requests))
        if path == "/__stub/health":
            return self._send(200, {"ok": True})
        if not self.headers.get("Authorization"):
            return self._send(401, {"message": "Requires authentication"})
        with _lock:
            if host == "api.github.com":
                return self._github(path, query)
            if host == "api.devin.ai":
                return self._devin(path)
        return self._send(404, {"message": "stub has no route for this host"})

    def _github(self, path: str, query: dict) -> None:
        m = re.fullmatch(REPO + r"(?P<rest>/.*)?", path)
        if not m:
            return self._send(404, {"message": NOT_FOUND})
        repo, rest = f"{m['owner']}/{m['name']}", m["rest"] or ""
        first_page = (query.get("page") or ["1"])[0] == "1"
        route = (self.command, rest)
        if route == ("GET", "/issues"):
            return self._send(200, _list_issues(repo, query) if first_page else [])
        if route == ("POST", "/issues"):
            return self._create_issue(repo)
        if route == ("GET", "/pulls"):
            return self._send(200, list(_pulls.get(repo, [])) if first_page else [])
        if route == ("GET", "/hooks"):
            return self._send(200, [])
        if route == ("POST", "/hooks"):
            return self._send(201, {"id": 1})
        if self.command == "DELETE" and re.fullmatch(r"/hooks/\d+", rest):
            return self._send(204)
        m = re.fullmatch(ISSUE, rest)
        if m:
            return self._issue_route(repo, int(m["n"]), m["sub"])
        return self._send(404, {"message": NOT_FOUND})

    def _create_issue(self, repo: str) -> None:
        data = self._json_body()
        number = _next_number(repo)
        _issues[(repo, number)] = {
            "number": number,
            "title": data.get("title", ""),
            "body": data.get("body", ""),
            "labels": list(data.get("labels") or []),
            "state": "open",
        }
        # The app only reads number/html_url from the create response.
        return self._send(
            201,
            {"number": number, "state": "open", "html_url": _html_url(repo, "issues", number)},
        )

    def _issue_route(self, repo: str, number: int, sub: str | None) -> None:
        issue = _issues.get((repo, number))
        if issue is None:
            return self._send(404, {"message": NOT_FOUND})
        route = (self.command, sub)
        if route == ("GET", None):
            return self._send(200, _issue_view(repo, issue))
        if route == ("GET", "/timeline"):
            return self._send(200, [])
        if route == ("POST", "/comments"):
            self._json_body()
            return self._send(201, {"id": secrets.randbelow(10**9)})
        if route == ("POST", "/labels"):
            issue["labels"] += list(self._json_body().get("labels") or [])
            return self._send(200, [{"name": html.escape(n)} for n in issue["labels"]])
        return self._send(404, {"message": NOT_FOUND})

    def _devin(self, path: str) -> None:
        if re.fullmatch(SESSIONS, path) and self.command == "POST":
            prompt = self._json_body().get("prompt", "")
            number = re.search(r"fix issue #(\d+)", prompt)
            repo = re.search(r"GitHub repository (\S+?)\.\n", prompt)
            sid = secrets.token_hex(8)
            session = {
                "session_id": f"devin-stub{sid}",
                "url": f"https://app.devin.ai/sessions/stub{sid}",
                "status": "new",
                "issue_number": int(number[1]) if number else 0,
                "repo": repo[1] if repo else "",
                "pull_requests": [],
                "polls": 0,
            }
            _sessions[session["session_id"]] = session
            return self._send(201, _public_session(session))
        m = re.fullmatch(SESSIONS + r"/(?P<sid>[^/]+)", path)
        if m and self.command == "GET":
            session = _sessions.get(m["sid"])
            if session is None:
                return self._send(404, {"detail": "session not found"})
            _finish_session(session)
            return self._send(200, _public_session(session))
        return self._send(404, {"detail": NOT_FOUND})

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()


def _list_issues(repo: str, query: dict) -> list[dict]:
    state = (query.get("state") or ["open"])[0]
    wanted = set(filter(None, (query.get("labels") or [""])[0].split(",")))
    return [
        _issue_view(repo, issue)
        for (r, _), issue in sorted(_issues.items())
        if r == repo and state in ("all", issue["state"]) and wanted <= set(issue["labels"])
    ]


def _public_session(session: dict) -> dict:
    return {k: session[k] for k in ("session_id", "url", "status", "pull_requests")}


class TLSThreadingHTTPServer(ThreadingHTTPServer):
    """The listening socket is TLS-wrapped with the handshake deferred to the
    per-connection thread, so one slow or idle client can never block
    accept() for everyone else."""

    daemon_threads = True

    def finish_request(self, request, client_address) -> None:
        request.settimeout(30)
        request.do_handshake()
        super().finish_request(request, client_address)


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="upstream-stub-"))
    cert, key = _generate_certs(work)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    httpd = TLSThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    (CA_DIR / "ready").write_text("ok\n")
    print(f"upstream stub listening on :{PORT} for {', '.join(HOSTNAMES)}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
