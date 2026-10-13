from __future__ import annotations

import asyncio
import collections
import json
import secrets
import shutil
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field

from .auth import Auth
from .config import Settings
from .contracts import Strict, TaskRequest, TERMINAL
from .db import Store
from .desktop import MacDesktop
from .model import LocalModel
from .workspace import Workspace


class Claim(Strict):
    secret: str = Field(min_length=20, max_length=100)

class Exchange(Strict):
    pairing_id: str
    exchange: str

class ApprovalResolution(Strict):
    digest: str
    approve: bool

class Apply(Strict):
    diff_hash: str


def create_app(settings: Settings, store: Store | None = None) -> FastAPI:
    if not settings.secure_cookies and (settings.host not in {"127.0.0.1", "localhost"} or any(not origin.startswith(("http://127.0.0.1:", "http://localhost:")) for origin in settings.origins)):
        raise ValueError("Insecure cookies are permitted only for loopback development")
    app = FastAPI(title="GhostDev", docs_url=None, redoc_url=None, openapi_url=None)
    store = store or Store(settings.state_dir / "ghostdev.sqlite3")
    auth = Auth(store)
    attempts = collections.defaultdict(collections.deque)
    app.state.store, app.state.settings = store, settings

    def origin(value: str | None):
        if value not in settings.origins:
            raise HTTPException(403, "Origin is not approved")

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if int(request.headers.get("content-length", "0") or 0) > 20000:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        # Bound chunked uploads too, before JSON parsing.
        if request.method in {"POST", "PUT", "PATCH"}:
            body = await request.body()
            if len(body) > 20000:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    def authenticated(request: Request):
        try:
            session = auth.session(request.cookies.get("ghostdev_session", ""))
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
        if request.method not in {"GET", "HEAD"}:
            origin(request.headers.get("origin"))
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                raise HTTPException(403, "Invalid CSRF token")
        return session

    def task_or_404(task_id: str):
        try:
            return store.task(task_id)
        except KeyError as exc:
            raise HTTPException(404, "Task not found") from exc

    def workspace(task_id: str):
        task = task_or_404(task_id)
        return Workspace(settings.state_dir/"tasks"/task_id, settings.project(task["project_id"]))

    def transition(task_id: str, state: str, revoke=False):
        task_or_404(task_id)
        try:
            store.transition(task_id, state, revoke=revoke)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return store.task(task_id)

    def pairing_rate(request: Request):
        origin(request.headers.get("origin"))
        key = request.client.host if request.client else "unknown"
        queue = attempts[key]
        now = time.monotonic()
        while queue and queue[0] < now-60:
            queue.popleft()
        if len(queue) >= 12:
            raise HTTPException(429, "Too many pairing attempts; wait a minute")
        queue.append(now)

    @app.get("/health")
    def health():
        return {"ok": True, "service": "GhostDev"}

    @app.post("/pairing/claim")
    def claim(payload: Claim, request: Request):
        pairing_rate(request)
        try:
            return auth.claim(payload.secret)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/pairing/exchange")
    def exchange(payload: Exchange, request: Request, response: Response):
        origin(request.headers.get("origin"))
        try:
            result = auth.exchange(payload.pairing_id, payload.exchange)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if result is None:
            return {"pending": True}
        token, csrf = result
        response.set_cookie("ghostdev_session", token, httponly=True, secure=settings.secure_cookies, samesite="strict", max_age=604800, path="/")
        return {"pending": False, "csrf": csrf}

    @app.get("/session")
    def session(current=Depends(authenticated)):
        return {"paired": True, "csrf": current["csrf"]}

    @app.post("/session/revoke")
    def revoke(request: Request, response: Response, _=Depends(authenticated)):
        auth.revoke(request.cookies.get("ghostdev_session", ""))
        response.delete_cookie("ghostdev_session", path="/")
        return {"revoked": True}

    @app.get("/settings")
    def status(_=Depends(authenticated)):
        return {"machine": "MacBook", "model": settings.model_name, "publisher": settings.model_publisher, "backend": settings.model_backend, "model_vision": settings.model_vision, "local_inference": "Configured on this MacBook; verify the local model service with doctor", "desktop": MacDesktop(settings).doctor(), "mode": "Native Mac desktop, review-first recommended", "voice": "Disabled; local speech not implemented", "agent": "Custom structured AX/vision loop", "max_steps": settings.max_steps}

    @app.get("/projects")
    def projects(_=Depends(authenticated)):
        return [{"id": item.id, "name": item.name, "registered": True, "required_checks": item.required_checks, "entry_file": item.entry_file} for item in settings.projects]

    @app.get("/desktops")
    def desktops(_=Depends(authenticated)):
        return [{"id": "macbook", "apps": list(settings.apps), **MacDesktop(settings).doctor()}]

    @app.post("/tasks")
    def submit(payload: TaskRequest, _=Depends(authenticated)):
        try:
            settings.project(payload.project_id)
            settings.ready_model()
            if payload.task_type == "desktop" and not payload.expected_text.strip():
                raise ValueError("Desktop demo needs exact expected note text for independent verification")
            return store.submit(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/tasks")
    def tasks(_=Depends(authenticated)):
        with store.connect() as db:
            rows = db.execute("SELECT id FROM tasks ORDER BY created DESC LIMIT 30").fetchall()
        return [store.task(row[0]) for row in rows]

    @app.get("/tasks/{task_id}")
    def task(task_id: str, _=Depends(authenticated)):
        return task_or_404(task_id)

    @app.get("/tasks/{task_id}/events")
    def events(task_id: str, after_seq: int = 0, _=Depends(authenticated)):
        task_or_404(task_id)
        return store.events(task_id, max(0, after_seq))

    @app.post("/tasks/{task_id}/pause")
    def pause(task_id: str, _=Depends(authenticated)):
        return transition(task_id, "PAUSED", revoke=True)

    @app.post("/tasks/{task_id}/resume")
    def resume(task_id: str, _=Depends(authenticated)):
        # Generation increments again, invalidating any pending result/action from before pause.
        return transition(task_id, "RUNNING", revoke=True)

    @app.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str, _=Depends(authenticated)):
        current = task_or_404(task_id)
        if current["state"] in TERMINAL:
            return current
        return transition(task_id, "CANCELLED", revoke=True)

    @app.post("/tasks/{task_id}/approvals/{approval_id}/resolve")
    def resolve(task_id: str, approval_id: str, payload: ApprovalResolution, _=Depends(authenticated)):
        current = task_or_404(task_id)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM approvals WHERE id=? AND task_id=?", (approval_id, task_id)).fetchone()
            if not row or row["status"] != "pending" or current["state"] != "AWAITING_APPROVAL" or row["generation"] != current["generation"] or not secrets.compare_digest(payload.digest, row["digest"]):
                raise HTTPException(409, "Approval no longer matches the exact action and execution lease")
            if time.time()-current.get("observation", {}).get("timestamp", 0) > settings.observation_max_age:
                raise HTTPException(409, "Observation expired; pause and resume to replan")
            db.execute("UPDATE approvals SET status=? WHERE id=?", ("approved" if payload.approve else "denied", approval_id))
        return {"resolved": True}

    @app.post("/desktop/takeover")
    def takeover(_=Depends(authenticated)):
        with store.connect() as db:
            rows = db.execute("SELECT id FROM tasks WHERE state IN ('RUNNING','VERIFYING','AWAITING_APPROVAL')").fetchall()
        for row in rows:
            transition(row[0], "PAUSED", revoke=True)
            store.event(row[0], "desktop_control_changed", {"owner": "human"})
        return {"owner": "human"}

    @app.get("/tasks/{task_id}/diff")
    def diff(task_id: str, _=Depends(authenticated)):
        ws = workspace(task_id)
        if not ws.meta_path.exists():
            return {"patch": "", "files": [], "sha256": ""}
        return ws.diff()

    @app.post("/tasks/{task_id}/apply")
    def apply(task_id: str, payload: Apply, _=Depends(authenticated)):
        current = task_or_404(task_id)
        if current["state"] != "READY_FOR_REVIEW" or payload.diff_hash != current.get("diff_hash"):
            raise HTTPException(409, "Only the exact verified reviewed proposal can be applied")
        transition(task_id, "APPLYING")
        try:
            result = workspace(task_id).apply(payload.diff_hash)
        except (ValueError, OSError) as exc:
            transition(task_id, "READY_FOR_REVIEW")
            raise HTTPException(409, str(exc)) from exc
        transition(task_id, "COMPLETED")
        return result

    @app.post("/tasks/{task_id}/discard")
    def discard(task_id: str, _=Depends(authenticated)):
        result = transition(task_id, "DISCARDED", revoke=True)
        ws = workspace(task_id)
        # Preserve event/check/screenshot evidence, remove only isolated project contents.
        if ws.root.exists():
            shutil.rmtree(ws.root)
        return result

    @app.get("/artifacts/{artifact_id}")
    def artifact(artifact_id: str, _=Depends(authenticated)):
        with store.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Artifact not found")
        path = Path(row["path"])
        if not path.is_relative_to(settings.state_dir) or path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Artifact unavailable")
        return FileResponse(path, media_type=row["media_type"], filename=row["name"] if row["media_type"] != "image/png" else None)

    @app.get("/tasks/{task_id}/screen")
    def screen(task_id: str, _=Depends(authenticated)):
        observation = task_or_404(task_id).get("observation", {})
        if not observation.get("artifact_id"):
            raise HTTPException(404, "No permitted screen capture yet")
        return artifact(observation["artifact_id"])

    @app.websocket("/tasks/{task_id}/stream")
    async def stream(websocket: WebSocket, task_id: str, after_seq: int = 0):
        try:
            origin(websocket.headers.get("origin"))
            token = websocket.cookies.get("ghostdev_session", "")
            auth.session(token)
            task_or_404(task_id)
        except (HTTPException, ValueError):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        sequence = max(0, after_seq)
        try:
            while True:
                # Session revocation takes effect during an existing stream too.
                auth.session(token)
                for item in store.events(task_id, sequence):
                    await websocket.send_json(item)
                    sequence = item["seq"]
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=0.25)
                except TimeoutError:
                    pass
        except (WebSocketDisconnect, ValueError, RuntimeError):
            await websocket.close(code=1008)

    @app.get("/{path:path}")
    def frontend(path: str):
        root = settings.web_dir.resolve()
        candidate = (root/path).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(404)
        if candidate.is_file():
            return FileResponse(candidate)
        if path.startswith(("_expo/", "assets/")):
            raise HTTPException(404)
        if (root/"index.html").exists():
            return FileResponse(root/"index.html")
        return JSONResponse({"service": "GhostDev", "message": "Build the phone UI: cd apps/mobile && npm ci && npm run build"})

    return app
