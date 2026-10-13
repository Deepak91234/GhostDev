from __future__ import annotations

import html
import json
import mimetypes
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .contracts import Action
from .db import LeaseRevoked, Store
from .workspace import Workspace, sha


class Preview:
    def __init__(self, workspace: Workspace):
        root = workspace.root
        project = workspace.project
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    relative = unquote(urlparse(self.path).path).lstrip("/") or project.preview_file
                    if relative == "report.html":
                        note = workspace.path(project.entry_file).read_text()
                        data = ("<!doctype html><title>GhostDev Report</title><style>body{background:#101720;color:#ecf2f0;font:20px system-ui;padding:40px}pre{white-space:pre-wrap}section{height:800px}</style><h1>GhostDev Report</h1><section>Scroll to the saved report below</section><h2>Summary</h2><pre>"+html.escape(note)+"</pre>").encode()
                        mime = "text/html"
                    else:
                        path = workspace.path(relative)
                        data = path.read_bytes()
                        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'; sandbox allow-scripts")
                    self.end_headers()
                    self.wfile.write(data)
                except (ValueError, OSError):
                    self.send_error(404)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CodeTools:
    def __init__(self, store: Store, workspace: Workspace, task_id: str):
        self.store, self.workspace, self.task_id = store, workspace, task_id
        self.preview: Preview | None = None

    def run_check(self, check_id: str, guard) -> dict:
        project = self.workspace.project
        if check_id not in project.checks:
            raise ValueError("Check ID is not registered")
        if not shutil.which("docker"):
            raise RuntimeError("Docker is required for code checks; host-shell fallback is disabled")
        command = project.checks[check_id]
        if not command or any(not isinstance(arg, str) for arg in command):
            raise ValueError("Registered check must be an argv list")
        harness = Path(__file__).resolve().parent.parent / "verification"
        name = "ghostdev-" + self.task_id[:16]
        args = ["docker", "run", "--rm", "--name", name, "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m", "--cpus=1", "--user=65534:65534", "--tmpfs=/tmp:rw,noexec,nosuid,size=32m", "--mount", "type=bind,src="+str(self.workspace.root)+",dst=/work,readonly", "--mount", "type=bind,src="+str(harness)+",dst=/verification,readonly", "--workdir=/work", project.image, *command]
        started = time.monotonic()
        log_path = self.workspace.directory / ("check-"+sha(check_id.encode())[:8]+".log")
        timed_out = False
        with log_path.open("wb") as log:
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    guard()
                    if time.monotonic()-started > 60 or log_path.stat().st_size > 150_000:
                        timed_out = True
                        break
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
        output = log_path.read_text(errors="replace")[:12000]
        # Harness must produce its fixed acceptance ID and test count, not only exit 0.
        report = None
        for line in output.splitlines():
            try:
                candidate = json.loads(line)
                if candidate.get("harness") == "ghostdev-cart-v1":
                    report = candidate
            except (ValueError, AttributeError):
                continue
        passed = process.returncode == 0 and not timed_out and bool(report and report.get("tests", 0) >= 3 and report.get("passed") == report.get("tests"))
        result = {"check_id": check_id, "argv": command, "exit_code": process.returncode, "timeout": timed_out, "elapsed_seconds": round(time.monotonic()-started, 3), "output": output, "truncated": log_path.stat().st_size > 12000, "passed": passed, "report": report, "source_hash": self.workspace.diff()["sha256"]}
        self.store.event(self.task_id, "check_result", result)
        task = self.store.task(self.task_id)
        checks = task.get("checks", {})
        checks[check_id] = result
        self.store.update(self.task_id, checks=checks)
        return result

    def execute(self, action: Action, guard) -> dict:
        guard()
        ws = self.workspace
        if action.kind == "list_files":
            return {"files": sorted(ws.manifest)}
        if action.kind == "read_file":
            return {"path": action.path, "text": ws.path(action.path).read_text()[:16000]}
        if action.kind == "search_code":
            matches = []
            for relative in ws.manifest:
                for number, line in enumerate(ws.path(relative).read_text().splitlines(), 1):
                    if action.text in line:
                        matches.append({"path": relative, "line": number, "text": line[:300]})
                    if len(matches) >= 50:
                        return {"matches": matches, "truncated": True}
            return {"matches": matches}
        if action.kind == "apply_patch":
            result = ws.patch(action.path, action.old, action.new)
            self.store.update(self.task_id, checks={})
            return result
        if action.kind == "run_check":
            return self.run_check(action.check_id, guard)
        if action.kind == "start_preview":
            if not self.preview:
                self.preview = Preview(ws)
            return {"preview_url": self.preview.url, "note": "Use launch_app browser with exactly this URL; its page must be observed"}
        if action.kind == "export_file":
            path = ws.path(action.path)
            data = path.read_bytes()
            artifact_dir = ws.directory / "exports"
            artifact_dir.mkdir(exist_ok=True)
            export = artifact_dir / sha(data)
            export.write_bytes(data)
            artifact_id = self.store.artifact(self.task_id, export, "application/octet-stream", path.name)
            self.store.event(self.task_id, "artifact_ready", {"id": artifact_id, "name": path.name})
            return {"artifact_id": artifact_id, "bytes": len(data)}
        raise ValueError("Unsupported code tool")
