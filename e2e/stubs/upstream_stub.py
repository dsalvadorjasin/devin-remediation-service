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

import itertools
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CA_DIR = Path(os.getenv("STUB_CA_DIR", "/stub-ca"))
WORK_DIR = Path(os.getenv("STUB_WORK_DIR", "/tmp/upstream-stub"))
PORT = int(os.getenv("STUB_PORT", "443"))
HOSTNAMES = ["api.github.com", "github.com", "api.devin.ai", "app.devin.ai"]
FAIL_MARKER = "stub-devin-error"
FIRST_ISSUE_NUMBER = 4201

_lock = threading.RLock()
_issues: dict[tuple[str, int], dict] = {}
_pulls: dict[str, list[dict]] = {}
_sessions: dict[str, dict] = {}
_numbers: dict[str, itertools.count] = {}
_requests: list[dict] = []

REPO = r"/repos/(?P<owner>[^/]+)/(?P<name>[^/]+)"
SESSIONS = r"/v3/organizations/(?P<org>[^/]+)/sessions"


def _next_number(repo: str) -> int:
    counter = _numbers.setdefault(repo, itertools.count(FIRST_ISSUE_NUMBER))
    return next(counter)


def _generate_certs() -> None:
    CA_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    (CA_DIR / "ready").unlink(missing_ok=True)
    ca_key, ca_pem = WORK_DIR / "ca.key", CA_DIR / "ca.pem"
    key, csr, crt = WORK_DIR / "server.key", WORK_DIR / "server.csr", WORK_DIR / "server.pem"
    ext = WORK_DIR / "san.ext"
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


def _issue_view(repo: str, issue: dict) -> dict:
    return {
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "labels": [{"name": name} for name in issue["labels"]],
        "html_url": f"https://github.com/{repo}/issues/{issue['number']}",
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
    pr_url = f"https://github.com/{session['repo']}/pull/{pr_number}"
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
                    "host": (self.headers.get("Host") or "").split(":")[0],
                    "method": self.command,
                    "path": urlsplit(self.path).path,
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
        return self._send(404, {"message": f"stub has no route for {host}"})

    def _github(self, path: str, query: dict) -> None:
        m = re.fullmatch(REPO + r"(?P<rest>/.*)?", path)
        if not m:
            return self._send(404, {"message": "Not Found"})
        repo, rest = f"{m['owner']}/{m['name']}", m["rest"] or ""
        page = int((query.get("page") or ["1"])[0])
        if rest == "/issues" and self.command == "GET":
            state = (query.get("state") or ["open"])[0]
            wanted = set(filter(None, (query.get("labels") or [""])[0].split(",")))
            rows = [
                _issue_view(repo, i)
                for (r, _), i in sorted(_issues.items())
                if r == repo
                and (state == "all" or i["state"] == state)
                and wanted <= set(i["labels"])
            ]
            return self._send(200, rows if page == 1 else [])
        if rest == "/issues" and self.command == "POST":
            data = self._json_body()
            number = _next_number(repo)
            issue = {
                "number": number,
                "title": data.get("title", ""),
                "body": data.get("body", ""),
                "labels": list(data.get("labels") or []),
                "state": "open",
            }
            _issues[(repo, number)] = issue
            return self._send(201, _issue_view(repo, issue))
        if rest == "/pulls" and self.command == "GET":
            return self._send(200, list(_pulls.get(repo, [])) if page == 1 else [])
        if rest == "/hooks":
            return self._send(
                200 if self.command == "GET" else 201, [] if self.command == "GET" else {"id": 1}
            )
        if re.fullmatch(r"/hooks/\d+", rest) and self.command == "DELETE":
            return self._send(204)
        m = re.fullmatch(r"/issues/(?P<n>\d+)(?P<sub>/timeline|/comments|/labels)?", rest)
        if m:
            issue = _issues.get((repo, int(m["n"])))
            if issue is None:
                return self._send(404, {"message": "Not Found"})
            sub = m["sub"]
            if sub is None and self.command == "GET":
                return self._send(200, _issue_view(repo, issue))
            if sub == "/timeline" and self.command == "GET":
                return self._send(200, [])
            if sub == "/comments" and self.command == "POST":
                self._json_body()
                return self._send(201, {"id": secrets.randbelow(10**9)})
            if sub == "/labels" and self.command == "POST":
                issue["labels"] += list(self._json_body().get("labels") or [])
                return self._send(200, [{"name": n} for n in issue["labels"]])
        return self._send(404, {"message": "Not Found"})

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
        return self._send(404, {"detail": "Not Found"})

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()


def _public_session(session: dict) -> dict:
    return {k: session[k] for k in ("session_id", "url", "status", "pull_requests")}


class TLSThreadingHTTPServer(ThreadingHTTPServer):
    """TLS handshake runs in the per-connection thread, so one slow or idle
    client can never block accept() for everyone else."""

    daemon_threads = True

    def __init__(self, address, handler, ctx: ssl.SSLContext) -> None:
        super().__init__(address, handler)
        self.ctx = ctx

    def finish_request(self, request, client_address) -> None:
        request.settimeout(30)
        super().finish_request(self.ctx.wrap_socket(request, server_side=True), client_address)


def main() -> None:
    _generate_certs()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(WORK_DIR / "server.pem", WORK_DIR / "server.key")
    httpd = TLSThreadingHTTPServer(("0.0.0.0", PORT), Handler, ctx)
    (CA_DIR / "ready").write_text("ok\n")
    print(f"upstream stub listening on :{PORT} for {', '.join(HOSTNAMES)}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
