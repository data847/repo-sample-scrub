"""Local web UI (stdlib only): pick repos from GitHub / GitLab / Bitbucket, run
the sample + scrub pipeline in a background queue, watch logs, download zips.

Binds to 127.0.0.1 by default (--bind 0.0.0.0 inside Docker). Credentials come from the environment / .env or are typed
into the UI; UI-entered ones live in server memory only (never written to disk,
logged or sent back to the browser). Requests must carry a loopback Host header and (for POST) a JSON
content type, which blocks DNS-rebinding and cross-site form posts.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from . import bridge, config, pipeline

STATIC = Path(__file__).parent / "static"
DOWNLOADABLE = {"RESULT.json", "MANIFEST.json", "VERDICT.json", "SAMPLE_MANIFEST.json"}
SAFE = re.compile(r"^[\w.\-]+$")


def _within_roots(p: Path) -> bool:
    roots = config.local_roots()
    return not roots or any(p == r or r in p.parents for r in roots)


def _check_root(p: Path, original: str) -> None:
    if not _within_roots(p):
        roots = ", ".join(str(r) for r in config.local_roots())
        raise bridge.BridgeError(
            f"{original} is outside the allowed local folder(s): {roots}. "
            "If this server runs in Docker, use the path as mounted inside the container.")


def local_repo(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise bridge.BridgeError(f"local path must be absolute: {path}")
    p = p.resolve()
    _check_root(p, path)
    if not (p / ".git").exists():
        raise bridge.BridgeError(f"not a git repository: {p}")
    return p


def scan_local(path: str) -> List[dict]:
    """The path itself if it is a repo, else the git repos directly inside it."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise bridge.BridgeError(f"path must be absolute: {path}")
    p = p.resolve()
    _check_root(p, path)
    if not p.is_dir():
        raise bridge.BridgeError(f"not a directory: {p}")
    # a child symlinked to somewhere outside the allowed roots must not be listed
    found = [p] if (p / ".git").exists() else sorted(
        c for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")
        and (c / ".git").exists() and _within_roots(c.resolve()))
    return [{"ref": f"local:{c}", "name": c.name, "branch": str(c)} for c in found]


class Job:
    def __init__(self, job_id: str, repos: List[str], opts: pipeline.Options) -> None:
        self.id, self.repos, self.opts = job_id, repos, opts
        self.status = "queued"
        self.results: Dict[str, dict] = {}
        self.log: List[str] = []
        self.created = time.time()

    def add(self, msg: str) -> None:
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        del self.log[:-3000]

    def view(self, with_log: bool = False) -> dict:
        d = {"id": self.id, "status": self.status, "repos": self.repos, "results": self.results,
             "pct": self.opts.pct, "fast": self.opts.fast, "created": self.created}
        if with_log:
            d["log"] = self.log
        return d


class Manager:
    """One worker: the normal-mode scrub loads a ~700 MB NER model, so jobs run serially."""

    def __init__(self, tokens: config.Tokens) -> None:
        self.tokens = tokens
        self.jobs: Dict[str, Job] = {}
        self.q: "queue.Queue[Job]" = queue.Queue()
        threading.Thread(target=self._work, daemon=True).start()

    def submit(self, repos: List[str], opts: pipeline.Options) -> Job:
        job = Job(time.strftime("job-%Y%m%d-%H%M%S"), repos, opts)
        while job.id in self.jobs:  # two submits within one second
            job.id += "x"
        self.jobs[job.id] = job
        self.q.put(job)
        return job

    def _work(self) -> None:
        while True:
            job = self.q.get()
            job.status = "running"
            failed = 0
            for text in job.repos:
                try:
                    if text.startswith("local:"):
                        ref, local = None, local_repo(text[len("local:"):])
                    else:
                        ref, local = bridge.parse_repo(text), None
                    res = pipeline.run_repo(ref, local, job.id, job.opts, self.tokens, job.add)
                    job.results[text] = {"ok": True, **res}
                except Exception as exc:
                    failed += 1
                    msg = self.tokens.redact(str(exc))
                    job.results[text] = {"ok": False, "error": msg}
                    job.add(f"FAILED {text}: {msg}")
            job.status = "failed" if failed == len(job.repos) else ("partial" if failed else "done")


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def host_allowed(header: str) -> bool:
    """Accept loopback hostnames on any port (a Docker port mapping may differ from
    the in-container port) and any hostname listed in SAMPLE_SCRUB_ALLOWED_HOSTS.
    Anything else is refused, which is what blocks DNS-rebinding attacks."""
    try:
        name = (urlsplit("//" + header).hostname or "").lower()
    except ValueError:
        return False
    return name in LOOPBACK or name in config.allowed_hostnames()


def make_handler(mgr: Manager, port: int):

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json", extra=None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode())

        def _host_ok(self) -> bool:
            if host_allowed(self.headers.get("Host", "")):
                return True
            self._json(403, {"error": "bad Host header"})
            return False

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/config":
                return self._json(200, {
                    "tokens": mgr.tokens.available(),
                    "bitbucket_email": bool(mgr.tokens.bitbucket_email),
                    "profiler": str(config.profiler_dir() or ""),
                    "pii_scrub": str(config.pii_scrub_dir() or ""),
                    "salt": bool(__import__("os").environ.get("REPO_SANITIZER_SALT")),
                    "outputs": str(config.outputs_root()),
                    "local_roots": [str(r) for r in config.local_roots()]})
            if path == "/api/jobs":
                return self._json(200, [j.view() for j in sorted(mgr.jobs.values(), key=lambda j: -j.created)])
            m = re.match(r"^/api/jobs/([\w.\-]+)$", path)
            if m:
                job = mgr.jobs.get(m.group(1))
                return self._json(200, job.view(True)) if job else self._json(404, {"error": "no such job"})
            m = re.match(r"^/download/([\w.\-]+)/([\w.\-]+)/([\w.\-]+)$", path)
            if m:
                return self._download(*m.groups())
            self._json(404, {"error": "not found"})

        def _download(self, job: str, slug: str, name: str) -> None:
            if not all(SAFE.match(x) and x not in (".", "..") for x in (job, slug, name)):
                return self._json(400, {"error": "bad path"})
            if not (name in DOWNLOADABLE or name.endswith("_sanitized.zip")):
                return self._json(404, {"error": "not downloadable"})
            f = (config.outputs_root() / job / slug / name).resolve()
            if config.outputs_root() not in f.parents or not f.is_file():
                return self._json(404, {"error": "not found"})
            ctype = "application/zip" if name.endswith(".zip") else "application/json"
            self._send(200, f.read_bytes(), ctype,
                       {"Content-Disposition": f'attachment; filename="{name}"'})

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"error": "JSON required"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
            try:
                if self.path == "/api/list":
                    platform = body.get("platform", "")
                    repos = bridge.list_repos(platform, str(body.get("owner", "")).strip(),
                                              mgr.tokens, str(body.get("host", "")).strip())
                    return self._json(200, [{"ref": f"{r.platform}:{r.full_name}", "name": r.full_name,
                                             "branch": r.default_branch} for r in repos])
                if self.path == "/api/scan":
                    return self._json(200, scan_local(str(body.get("path", "")).strip()))
                if self.path == "/api/credentials":
                    if body.get("clear"):
                        mgr.tokens.clear(body["clear"] if body["clear"] in bridge.PLATFORMS else None)
                    else:
                        mgr.tokens.set(**{k: str(body.get(k) or "") for k in
                                          ("github", "gitlab", "bitbucket", "bitbucket_email")})
                    return self._json(200, {"tokens": mgr.tokens.available(),
                                            "bitbucket_email": bool(mgr.tokens.bitbucket_email)})
                if self.path == "/api/jobs":
                    repos = [str(r) for r in body.get("repos", []) if str(r).strip()]
                    if not repos:
                        return self._json(400, {"error": "select at least one repo"})
                    for r in repos:  # fail fast on a bad local path instead of after queueing
                        if r.startswith("local:"):
                            local_repo(r[len("local:"):])
                    pct = float(body.get("pct", 5))
                    if not 0 < pct <= 100:
                        return self._json(400, {"error": "pct must be in (0, 100]"})
                    opts = pipeline.Options(
                        pct=pct, branch=(body.get("branch") or None),
                        min_commits=int(body.get("min_commits", 20)),
                        terms=[t.strip() for t in body.get("terms", []) if t.strip()],
                        fast=bool(body.get("fast")))
                    return self._json(200, mgr.submit(repos, opts).view())
            except bridge.BridgeError as exc:
                return self._json(400, {"error": mgr.tokens.redact(str(exc))})
            except (ValueError, TypeError) as exc:
                return self._json(400, {"error": f"bad request: {exc}"})
            self._json(404, {"error": "not found"})

    return H


def serve(port: int = 8795, tokens_file: Optional[str] = None, bind: str = "127.0.0.1") -> int:
    mgr = Manager(config.Tokens(tokens_file))
    srv = ThreadingHTTPServer((bind, port), make_handler(mgr, port))
    print(f"sample-scrub UI on http://{bind}:{port}  (outputs: {config.outputs_root()})", flush=True)
    if bind not in ("127.0.0.1", "localhost", "::1"):
        print("NOTE: bound beyond loopback and the UI has no login. Publish the port to the "
              "host's loopback only (docker -p 127.0.0.1:PORT:PORT) and never expose it to a network.",
              flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
