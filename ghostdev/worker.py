from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import time
import uuid
from pathlib import Path

from .config import Settings
from .contracts import TERMINAL
from .db import LeaseRevoked, Store
from .desktop import GUI_KINDS, MacDesktop
from .model import LocalModel
from .tools import CodeTools
from .workspace import Workspace


class Worker:
    def __init__(self, settings: Settings, store: Store, desktop=None, model=None):
        self.settings, self.store = settings, store
        self.desktop = desktop or MacDesktop(settings)
        self.model = model or LocalModel(settings)

    async def guarded(self, awaitable, task_id: str, generation: int, deadline: float):
        future = asyncio.ensure_future(awaitable)
        try:
            while not future.done():
                self.store.lease(task_id, generation)
                if time.monotonic() > deadline:
                    raise TimeoutError("Task deadline exceeded")
                await asyncio.wait({future}, timeout=0.1)
            self.store.lease(task_id, generation)
            return await future
        finally:
            if not future.done():
                future.cancel()
                try:
                    await future
                except asyncio.CancelledError:
                    pass

    async def observation(self, task_id: str, workspace: Workspace) -> dict:
        observation = await asyncio.to_thread(self.desktop.observe, workspace.directory)
        image = self.desktop.image
        if image:
            target = workspace.directory / "screens"
            target.mkdir(exist_ok=True)
            path = target / (observation["id"]+".png")
            path.write_bytes(image)
            observation["image_sha256"] = hashlib.sha256(image).hexdigest()
            artifact = self.store.artifact(task_id, path, "image/png", "desktop.png")
            observation["artifact_id"] = artifact
        self.store.update(task_id, observation=observation)
        # AX text is model context, not duplicated into the event journal.
        brief = {key: value for key, value in observation.items() if key != "elements"}
        self.store.event(task_id, "observation_ready", brief)
        return observation

    @staticmethod
    def target_summary(action, observation: dict) -> dict | None:
        # What the owner sees when approving an element-targeted action.
        if action.target < 0:
            return None
        element = observation["elements"][action.target]
        return {"index": action.target, "role": element.get("role", ""), "label": str(element.get("title") or element.get("value") or "")[:120], "x": action.x, "y": action.y}

    async def approval(self, task_id: str, generation: int, action, target: dict | None = None):
        approval_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO approvals(id,task_id,generation,digest,action) VALUES(?,?,?,?,?)", (approval_id, task_id, generation, action.digest(), action.model_dump_json()))
        self.store.transition(task_id, "AWAITING_APPROVAL")
        self.store.event(task_id, "approval_required", {"id": approval_id, "digest": action.digest(), "action": action.model_dump(), "target": target, "expires_at": time.time()+self.settings.observation_max_age})
        # Observation expiry below normally ends the wait first; this monotonic cap guards against wall-clock jumps.
        limit = time.monotonic()+self.settings.observation_max_age+5
        while time.monotonic() < limit:
            task = self.store.task(task_id)
            if task["generation"] != generation or task["state"] != "AWAITING_APPROVAL":
                raise LeaseRevoked("Approval lease revoked")
            with self.store.connect() as db:
                row = db.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row[0] == "denied":
                raise ValueError("Owner denied the proposed action")
            if row[0] == "approved":
                self.store.transition(task_id, "RUNNING")
                return
            if time.time()-task["observation"]["timestamp"] > self.settings.observation_max_age:
                with self.store.connect() as db:
                    db.execute("UPDATE approvals SET status='expired' WHERE id=?", (approval_id,))
                self.store.transition(task_id, "PAUSED", revoke=True)
                self.store.event(task_id, "action_rejected", {"reason": "Approval expired; resume to observe and replan"})
                raise LeaseRevoked("Approval expired")
            await asyncio.sleep(0.15)
        raise TimeoutError("Approval wait exceeded the observation lifetime")

    async def run_task(self, initial: dict):
        task_id = initial["id"]
        project = self.settings.project(initial["project_id"])
        if initial["task_type"] == "desktop":
            project = project.model_copy(update={"preview_file": "report.html"})
        workspace = Workspace(self.settings.state_dir / "tasks" / task_id, project)
        tools = CodeTools(self.store, workspace, task_id)
        history, gui_evidence = [], []
        deadline = time.monotonic()+self.settings.task_timeout
        attempted = 0
        no_progress = 0
        try:
            workspace.prepare(initial["task_type"] != "inspect")
            self.desktop.begin_task()
            # Server metadata checks must pass before ANY desktop input.
            while True:
                pending = self.store.task(task_id)
                if pending["state"] in TERMINAL:
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError("Task deadline exceeded during preflight")
                if pending["state"] == "PAUSED":
                    await asyncio.sleep(0.15)
                    continue
                generation = pending["generation"]
                try:
                    identity = await self.guarded(self.model.doctor(), task_id, generation, deadline)
                    break
                except LeaseRevoked:
                    continue
            self.store.update(task_id, model_identity=identity, workspace=str(workspace.root))
            self.store.event(task_id, "task_started", {"model": identity, "route": "custom structured accessibility/vision loop"})
            if initial["task_type"] == "coding":
                for check_id in project.required_checks:
                    while True:
                        pending = self.store.task(task_id)
                        if pending["state"] in TERMINAL:
                            return
                        if time.monotonic() > deadline:
                            raise TimeoutError("Task deadline exceeded during baseline check")
                        if pending["state"] == "PAUSED":
                            await asyncio.sleep(0.15)
                            continue
                        generation = pending["generation"]
                        try:
                            before = await asyncio.to_thread(tools.run_check, check_id, lambda: self.store.lease(task_id, generation))
                            break
                        except LeaseRevoked:
                            continue
                    self.store.event(task_id, "baseline_check", before)
                self.store.update(task_id, checks={})
            while attempted < self.settings.max_steps and time.monotonic() < deadline:
                current = self.store.task(task_id)
                if current["state"] in TERMINAL:
                    return
                if current["state"] == "PAUSED":
                    await asyncio.sleep(0.2)
                    continue
                generation = current["generation"]
                guard = lambda: self.store.lease(task_id, generation)
                try:
                    guard()
                    observation = await self.observation(task_id, workspace)
                    guard()
                    public_project = {"id": project.id, "files": list(workspace.manifest), "entry_file": project.entry_file, "preview_file": project.preview_file, "checks": project.required_checks, "apps": list(self.settings.apps)}
                    action, metric = await self.guarded(self.model.propose(current, observation, self.desktop.image, history, public_project), task_id, generation, deadline)
                    attempted += 1
                    action = self.desktop.resolve_target(action, observation)
                    self.desktop.validate(action, observation)
                    if current["task_type"] == "inspect" and action.kind in GUI_KINDS | {"apply_patch", "start_preview"}:
                        raise ValueError("Inspection tasks cannot mutate code or desktop")
                    action_id = uuid.uuid4().hex
                    target = self.target_summary(action, observation)
                    self.store.event(task_id, "action_proposed", {"id": action_id, "action": action.model_dump(), "target": target, "inference": metric})
                    if action.kind == "finish":
                        await self.verify(task_id, workspace, observation, gui_evidence, guard)
                        return
                    needs_approval = current["mode"] == "review-first" and action.kind in GUI_KINDS - {"wait"}
                    if needs_approval:
                        waiting = time.monotonic()
                        try:
                            await self.approval(task_id, generation, action, target)
                        finally:
                            # The owner's decision time is not agent time. Each wait ends by observation expiry, so the extension stays bounded.
                            deadline += time.monotonic()-waiting
                    guard()
                    if action.kind in GUI_KINDS:
                        if action.kind == "launch_app" and action.app == "browser":
                            if not tools.preview or action.path != tools.preview.url:
                                raise ValueError("Browser URL was not registered by this task's preview tool")
                        result = await asyncio.to_thread(self.desktop.execute, action, observation, workspace.directory, guard, workspace.path)
                        gui_evidence.append({"id": action_id, "kind": action.kind, "app": action.app or observation["app"], "observation_id": observation["id"]})
                        self.store.update(task_id, gui_evidence=gui_evidence)
                    else:
                        result = await asyncio.to_thread(tools.execute, action, guard)
                    self.store.event(task_id, "action_executed", {"id": action_id, "kind": action.kind, "result": result})
                    history.append({"action": action.model_dump(), "result": result})
                    guard()
                    after = await self.observation(task_id, workspace)
                    self.store.event(task_id, "action_observed", {"id": action_id, "observation_id": after["id"], "postcondition_requested": action.postcondition, "note": "Recorded observation; final acceptance is checked independently"})
                    if action.kind in GUI_KINDS - {"wait", "focus_app"}:
                        unchanged = after.get("image_sha256") == observation.get("image_sha256") and after.get("signature") == observation.get("signature")
                        no_progress = no_progress+1 if unchanged else 0
                        if no_progress >= 3:
                            self.store.transition(task_id, "PAUSED", revoke=True)
                            self.store.event(task_id, "action_rejected", {"reason": "Repeated unchanged observations; inspect and resume"})
                            no_progress = 0
                except LeaseRevoked:
                    history.append({"interruption": "Previous action or inference was revoked; observe and replan, never replay"})
                    continue
                except (ValueError, TimeoutError) as exc:
                    self.store.event(task_id, "action_rejected", {"reason": str(exc)[:500], "outcome": "Input may be partial if dispatch started; re-observe before any retry"})
                    history.append({"rejected": str(exc)[:500]})
                    if attempted >= 3 and all("rejected" in item for item in history[-3:]):
                        raise RuntimeError("Repeated invalid or stale actions; task stopped") from exc
            raise TimeoutError("Step or time budget exhausted")
        except BaseException as exc:
            state = self.store.task(task_id)["state"]
            if state not in TERMINAL and state != "READY_FOR_REVIEW":
                self.store.update(task_id, error=str(exc)[:600])
                self.store.transition(task_id, "FAILED", revoke=True)
                self.store.event(task_id, "task_failed", {"reason": str(exc)[:600]})
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
        finally:
            if tools.preview:
                await asyncio.to_thread(tools.preview.close)

    async def verify(self, task_id: str, workspace: Workspace, observation: dict, evidence: list, guard):
        task = self.store.task(task_id)
        self.store.transition(task_id, "VERIFYING")
        guard()
        if task["task_type"] == "inspect":
            self.store.update(task_id, summary="Inspection finished; see recorded tool output and exported artifacts")
            self.store.transition(task_id, "COMPLETED")
            return
        kinds = {item["kind"] for item in evidence}
        apps = {item["app"] for item in evidence}
        if not {"click", "type_text", "scroll"}.issubset(kinds) or not {"editor", "browser"}.issubset(apps):
            raise RuntimeError("Desktop acceptance requires actual clicking/typing/scrolling across editor and browser")
        if observation["app"] != "browser" or not observation.get("artifact_id"):
            raise RuntimeError("Final registered browser screenshot is missing")
        diff = workspace.diff()
        if task["task_type"] == "coding":
            checks = task.get("checks", {})
            if not workspace.project.required_checks:
                raise RuntimeError("No protected required checks registered")
            for check_id in workspace.project.required_checks:
                check = checks.get(check_id, {})
                if not check.get("passed") or check.get("source_hash") != diff["sha256"]:
                    raise RuntimeError("Required check missing, failed, or stale after edits")
            if not diff["files"]:
                raise RuntimeError("Coding task produced no patch")
            next_state = "READY_FOR_REVIEW"
        else:
            expected = task["expected_text"]
            saved = workspace.path(workspace.project.entry_file).read_text()
            visible_text = "\n".join(str(item.get("value", ""))+str(item.get("title", "")) for item in observation["elements"])
            if not expected or expected not in saved or expected not in visible_text:
                raise RuntimeError("Expected note must be saved AND independently visible in the final browser accessibility observation")
            next_state = "COMPLETED"
        self.store.update(task_id, diff_hash=diff["sha256"], changed_files=diff["files"], final_screenshot=observation["artifact_id"], summary="Verified task output. Original project is unchanged until an exact reviewed coding diff is applied.")
        self.store.event(task_id, "verification_finished", {"diff_hash": diff["sha256"], "changed_files": diff["files"], "final_screenshot": observation["artifact_id"], "gui_actions": len(evidence)})
        self.store.transition(task_id, next_state)

    async def run(self):
        lock_path = self.settings.state_dir / "worker.lock"
        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another worker already owns the desktop") from exc
            self.store.interrupted()
            while True:
                task = self.store.claim()
                if task:
                    await self.run_task(task)
                else:
                    await asyncio.sleep(0.25)
