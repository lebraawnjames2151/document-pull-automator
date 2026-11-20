"""Browser front end: `docpull-web` starts a local page with a form instead of the command line.

Uses only the standard library web server. Runs one pull at a time (SEC allows ~10 requests
per second per user, so parallel pulls would just compete), and hands results back as a zip.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .edgar import AmbiguousCompany, Company, EdgarClient, EdgarError, resolve_company
from .pipeline import DEFAULT_FORMS, execute, plan_documents
from .render import Renderer

PAGE = Path(__file__).with_name("web_page.html")
RUN_LOCK = threading.Lock()  # one pull at a time
JOBS: dict[str, "Job"] = {}
KEEP_DAYS = 7


@dataclass
class Job:
    id: str
    company: str
    site: str
    years: float
    debt: bool
    earnings: bool
    user_agent: str
    work_dir: Path
    state: str = "queued"  # queued | running | done | error | choose
    log: list[str] = field(default_factory=list)
    error: str = ""
    candidates: list[tuple[int, str]] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    zip_path: Path | None = None

    def say(self, line: str) -> None:
        self.log.append(line.rstrip())


def run_job(job: Job) -> None:
    with RUN_LOCK:
        job.state = "running"
        renderer = None
        try:
            client = EdgarClient(job.user_agent)
            try:
                company = resolve_company(client, job.company)
            except AmbiguousCompany as e:
                job.candidates, job.state = e.candidates, "choose"
                return
            except EdgarError as e:
                if not job.site:
                    job.state = "error"
                    job.error = (f"{e}. If the company is not an SEC filer (e.g. listed only in Canada), "
                                 "add its investor website and run again.")
                    return
                company = Company(0, job.company)  # not on EDGAR: website only

            plan = plan_documents(client, company, job.years, DEFAULT_FORMS, earnings=job.earnings,
                                  debt=job.debt, site=job.site or "auto", log=job.say)
            job.say(f"Downloading {len(plan.tasks)} documents...")
            renderer = Renderer(client, "pdf")
            root = execute(plan, renderer, job.work_dir, log=job.say)
            archive = shutil.make_archive(str(job.work_dir / root.name), "zip", root_dir=job.work_dir,
                                          base_dir=root.name)
            job.zip_path = Path(archive)
            job.result = {
                "company": company.folder_name,
                "counts": sorted(Counter(t.category for t in plan.tasks).items()),
                "failed": [t.stem for t in plan.tasks if t.status.startswith("failed")],
                "warnings": plan.warnings,
                "notes": plan.notes,
                "missing": [m.__dict__ for m in plan.missing],
            }
            job.state = "done"
            job.say("Done.")
        except Exception as e:  # show any failure on the page rather than killing the server
            job.state, job.error = "error", f"{type(e).__name__}: {e}"
        finally:
            if renderer:
                renderer.close()


def cleanup(out_root: Path) -> None:
    """Delete finished pulls older than KEEP_DAYS."""
    if not out_root.exists():
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    for d in out_root.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def make_handler(out_root: Path, default_ua: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/config":
                return self._json({"needs_user_agent": not default_ua})
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "status"] and parts[2] in JOBS:
                job = JOBS[parts[2]]
                since = int(dict(p.split("=") for p in urlparse(self.path).query.split("&") if "=" in p)
                            .get("since", 0))
                return self._json({
                    "state": job.state, "log": job.log[since:], "next": len(job.log), "error": job.error,
                    "candidates": job.candidates, "result": job.result,
                })
            if len(parts) == 3 and parts[:2] == ["api", "download"] and parts[2] in JOBS:
                job = JOBS[parts[2]]
                if job.zip_path and job.zip_path.exists():
                    data = job.zip_path.read_bytes()
                    name = job.zip_path.name.replace('"', "")
                    return self._send(200, data, "application/zip",
                                      {"Content-Disposition": f'attachment; filename="{name}"'})
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            if urlparse(self.path).path != "/api/run":
                return self._json({"error": "not found"}, 404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                company = str(body.get("company", "")).strip()
                site = str(body.get("site", "")).strip()
                years = float(body.get("years", 3))
                ua = default_ua or str(body.get("user_agent", "")).strip()
            except (ValueError, TypeError):
                return self._json({"error": "Invalid request."}, 400)
            if not company:
                return self._json({"error": "Enter a company."}, 400)
            if site and not site.startswith(("http://", "https://")):
                site = "https://" + site
            if not 0.25 <= years <= 20:
                return self._json({"error": "Years must be between 0.25 and 20."}, 400)
            if "@" not in ua:
                return self._json({"error": "Enter your name and work email (the SEC requires it)."}, 400)
            job_id = uuid.uuid4().hex[:12]
            job = Job(job_id, company, site, years, bool(body.get("debt", True)),
                      bool(body.get("earnings", True)), ua, out_root / job_id)
            JOBS[job_id] = job
            if RUN_LOCK.locked():
                job.say("Another pull is running. This one will start when it finishes.")
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            self._json({"id": job_id})

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="docpull-web", description="Run docpull from a web page.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1",
                   help="127.0.0.1 (default) = this computer only. 0.0.0.0 = anyone on the network "
                        "(there is no login, so only do this on a trusted network).")
    p.add_argument("--out", type=Path, default=Path("output") / "web",
                   help="Where pulls are stored before download (default ./output/web)")
    p.add_argument("--no-browser", action="store_true", help="Do not open a browser tab")
    args = p.parse_args(argv)

    cleanup(args.out)
    args.out.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(args.out.resolve(), os.environ.get("SEC_USER_AGENT", "")))
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}/"
    print(f"docpull is running at {url}  (close this window or press Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
