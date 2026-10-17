from __future__ import annotations

import io
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image

from .config import Settings
from .contracts import Action


GUI_KINDS = {"launch_app", "focus_app", "click", "double_click", "type_text", "scroll", "hotkey", "wait"}
TARGETED_KINDS = {"click", "double_click", "scroll"}
ALLOWED_KEYS = {("command", "s"), ("command", "a"), ("left",), ("right",), ("up",), ("down",), ("enter",), ("backspace",), ("tab",)}


def input_point(observation: dict, x: int, y: int) -> tuple[float, float]:
    bounds = observation["bounds"]
    return (bounds["X"]+x*bounds["Width"]/observation["image_width"], bounds["Y"]+y*bounds["Height"]/observation["image_height"])


def visible_box(bounds, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Part of an image-relative [x, y, w, h] element box inside the captured window image."""
    if not (isinstance(bounds, list) and len(bounds) == 4 and all(isinstance(v, int) for v in bounds)):
        return None
    left, top = max(0, bounds[0]), max(0, bounds[1])
    right, bottom = min(width, bounds[0]+bounds[2]), min(height, bounds[1]+bounds[3])
    if right-left < 2 or bottom-top < 2:
        return None
    return left, top, right, bottom


def element_center(observation: dict, index: int) -> tuple[int, int]:
    elements = observation.get("elements", [])
    if not 0 <= index < len(elements):
        raise ValueError("Target element is not in the current observation")
    box = visible_box(elements[index].get("bounds"), observation["image_width"], observation["image_height"])
    if box is None:
        raise ValueError("Target element is not visible in the captured window")
    return (box[0]+box[2])//2, (box[1]+box[3])//2


def scroll_point(action: Action, observation: dict) -> tuple[int, int]:
    # Without a target or explicit point, scroll the middle of the approved window.
    if action.target >= 0 or action.x or action.y:
        return action.x, action.y
    return observation["image_width"]//2, observation["image_height"]//2


class DesktopUnavailable(RuntimeError):
    pass


class MacDesktop:
    """Native Mac app/window capture and Quartz events. Host GUI is not sandboxed."""
    def __init__(self, settings: Settings):
        self.settings = settings
        self.allowed_titles: set[str] = {"GhostDev Cart", "GhostDev Report"}
        self.allowed_preview_url = ""
        self.image: bytes | None = None
        if sys.platform == "darwin":
            import AppKit
            import Quartz
            import ApplicationServices
            self.A, self.Q, self.AX = AppKit, Quartz, ApplicationServices

    def begin_task(self):
        self.allowed_titles = {"GhostDev Cart", "GhostDev Report"}
        self.allowed_preview_url = ""

    def doctor(self) -> dict:
        if sys.platform != "darwin":
            return {"ready": False, "os": sys.platform, "reason": "Native Mac desktop control must run on your MacBook", "host_gui_sandboxed": False}
        code, displays, count = self.Q.CGGetActiveDisplayList(16, None, None)
        return {"ready": bool(code == 0 and count == 1 and self.AX.AXIsProcessTrusted() and self.Q.CGPreflightScreenCaptureAccess()), "accessibility": bool(self.AX.AXIsProcessTrusted()), "screen_recording": bool(self.Q.CGPreflightScreenCaptureAccess()), "active_displays": int(count), "single_display_required": True, "host_gui_sandboxed": False, "os": "macOS"}

    def _attr(self, element, name):
        code, value = self.AX.AXUIElementCopyAttributeValue(element, name, None)
        return value if code == 0 else None

    def _active(self) -> tuple[str, int]:
        running = self.A.NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(running.bundleIdentifier() or ""), int(running.processIdentifier())

    def _window(self, pid: int) -> dict | None:
        options = self.Q.kCGWindowListOptionOnScreenOnly | self.Q.kCGWindowListExcludeDesktopElements
        for window in self.Q.CGWindowListCopyWindowInfo(options, self.Q.kCGNullWindowID):
            if window.get("kCGWindowOwnerPID") == pid and window.get("kCGWindowLayer") == 0 and window.get("kCGWindowBounds", {}).get("Width", 0) > 100:
                return dict(window)
        return None

    def _tree(self, pid: int, bounds: dict, image_size: tuple[int, int]) -> tuple[list, str]:
        app = self.AX.AXUIElementCreateApplication(pid)
        window = self._attr(app, "AXFocusedWindow")
        focus = self._attr(app, "AXFocusedUIElement")
        focus_role = str(self._attr(focus, "AXRole") or "") if focus else ""
        nodes = []
        def visit(element, depth=0):
            if not element or depth > 10 or len(nodes) >= 140:
                return
            role = str(self._attr(element, "AXRole") or "")
            secure = role == "AXSecureTextField" or self._attr(element, "AXSubrole") == "AXSecureTextField"
            node = {"role": role, "title": str(self._attr(element, "AXTitle") or self._attr(element, "AXDescription") or "")[:200]}
            url = self._attr(element, "AXURL")
            if url:
                node["url"] = str(url)[:500]
            if not secure:
                value = self._attr(element, "AXValue")
                if isinstance(value, str):
                    node["value"] = value[:2000]
            try:
                p, s = self._attr(element, "AXPosition"), self._attr(element, "AXSize")
                okp, point = self.AX.AXValueGetValue(p, self.AX.kAXValueCGPointType, None)
                oks, size = self.AX.AXValueGetValue(s, self.AX.kAXValueCGSizeType, None)
                # Chrome reports a wholly off-viewport element as a 1-pt strip at the viewport edge; it is not a click target.
                if okp and oks and size.width >= 2 and size.height >= 2:
                    node["bounds"] = [round((point.x-bounds["X"])*image_size[0]/bounds["Width"]), round((point.y-bounds["Y"])*image_size[1]/bounds["Height"]), round(size.width*image_size[0]/bounds["Width"]), round(size.height*image_size[1]/bounds["Height"])]
            except (TypeError, AttributeError):
                pass
            nodes.append(node)
            for child in list(self._attr(element, "AXChildren") or [])[:40]:
                visit(child, depth+1)
        visit(window)
        return nodes, focus_role

    def observe(self, workspace: Path) -> dict:
        if not self.doctor()["ready"]:
            raise DesktopUnavailable("Enable Accessibility and Screen Recording for the Mac worker's Terminal/Python, then restart it")
        bundle, pid = self._active()
        window = self._window(pid)
        app_id = next((key for key, value in self.settings.apps.items() if value == bundle), "unapproved")
        title = str(window.get("kCGWindowName", "")) if window else ""
        bounds = window.get("kCGWindowBounds", {}) if window else {}
        title_allowed = any(name in title for name in self.allowed_titles)
        allowed = app_id != "unapproved" and window is not None and title_allowed
        document = ""
        if allowed and app_id == "editor":
            ax_window = self._attr(self.AX.AXUIElementCreateApplication(pid), "AXFocusedWindow")
            document = str(self._attr(ax_window, "AXDocument") or "")
            parsed = urlparse(document)
            path = Path(unquote(parsed.path))
            allowed = parsed.scheme == "file" and not parsed.netloc and path.is_file() and not path.is_symlink() and path.resolve().is_relative_to((workspace/"project").resolve())
        if allowed and app_id == "browser":
            probe, _ = self._tree(pid, bounds, (int(bounds["Width"]), int(bounds["Height"])))
            allowed = bool(self.allowed_preview_url) and any(item.get("role") == "AXWebArea" and item.get("url", "").startswith(self.allowed_preview_url) for item in probe)
        observation = {"id": uuid.uuid4().hex, "timestamp": time.time(), "app": app_id, "capture_allowed": allowed, "image_width": 0, "image_height": 0, "bounds": {}, "elements": [], "focus_role": "", "signature": "redacted:" + app_id}
        self.image = None
        if not allowed:
            observation["message"] = "Outside the registered demo window; capture and input disabled. Launch an approved task resource."
            return observation
        # Capture only the approved application WINDOW, never the entire personal desktop.
        screenshot = workspace / ".window.png"
        result = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", str(window["kCGWindowNumber"]), str(screenshot)], capture_output=True, timeout=5)
        if result.returncode or not screenshot.exists():
            raise DesktopUnavailable("Window capture failed; check local OS permission")
        with Image.open(screenshot) as original:
            image = original.convert("RGB")
            image.thumbnail((1280, 1000))
            output = io.BytesIO()
            image.save(output, format="PNG")
        screenshot.unlink(missing_ok=True)
        self.image = output.getvalue()
        nodes, focus_role = self._tree(pid, bounds, image.size)
        structure = hashlib.sha256(json.dumps(nodes, sort_keys=True).encode()).hexdigest()
        observation.update({"title": title, "pid": pid, "document": document, "window_id": int(window["kCGWindowNumber"]), "image_width": image.width, "image_height": image.height, "bounds": dict(bounds), "elements": nodes, "focus_role": focus_role, "signature": f"{bundle}:{pid}:{document}:{window['kCGWindowNumber']}:{dict(bounds)}:{focus_role}:{structure}"})
        return observation

    def resolve_target(self, action: Action, observation: dict) -> Action:
        """Replace a target element index with concrete coordinates BEFORE approval, so the owner approves what executes."""
        if action.target < 0:
            return action
        if action.kind not in TARGETED_KINDS:
            raise ValueError("target applies only to click, double_click, and scroll")
        x, y = element_center(observation, action.target)
        return action.model_copy(update={"x": x, "y": y})

    def validate(self, action: Action, observation: dict):
        if action.observation_id != observation["id"]:
            raise ValueError("Action references a stale observation")
        if action.expected_app != observation["app"]:
            raise ValueError("Foreground app differs from expected app")
        if time.time()-observation["timestamp"] > self.settings.observation_max_age:
            raise ValueError("Screenshot is too old; observe again")
        if action.kind in GUI_KINDS - {"launch_app", "focus_app", "wait"} and not observation["capture_allowed"]:
            raise ValueError("Input outside approved windows is disabled")
        if action.kind in {"launch_app", "focus_app"} and action.app not in self.settings.apps:
            raise ValueError("App is not registered")
        if action.target >= 0:
            if action.kind not in TARGETED_KINDS:
                raise ValueError("target applies only to click, double_click, and scroll")
            if (action.x, action.y) != element_center(observation, action.target):
                raise ValueError("Coordinates do not match the target element; re-observe")
        if action.kind in {"click", "double_click"} and action.target < 0 and action.x == 0 and action.y == 0:
            raise ValueError("A click needs a target element (or explicit x/y); none was given")
        if action.kind in {"click", "double_click"} and not (0 <= action.x < observation["image_width"] and 0 <= action.y < observation["image_height"]):
            raise ValueError("Coordinates outside current window screenshot")
        if action.kind == "scroll":
            x, y = scroll_point(action, observation)
            if not (0 <= x < observation["image_width"] and 0 <= y < observation["image_height"]):
                raise ValueError("Scroll point outside current window screenshot")
        if action.kind == "type_text":
            if observation["app"] != "editor":
                raise ValueError("MVP typing is restricted to the prepared editor; browser address fields are disabled")
            if observation["focus_role"] != "AXTextArea":
                raise ValueError("Typing requires an editable non-secret field")
            if any(ord(c) < 32 and c not in "\n\t" for c in action.text):
                raise ValueError("Control characters are disabled")
        if action.kind == "hotkey":
            if tuple(action.keys) not in ALLOWED_KEYS:
                raise ValueError("Shortcut is not permitted")
            if action.keys == ["command", "s"] and observation["app"] != "editor":
                raise ValueError("Save shortcut is only allowed in the prepared editor")

    def execute(self, action: Action, observation: dict, workspace: Path, guard, path_resolver):
        guard()
        self.validate(action, observation)
        # A second observation detects app/window/geometry changes after inference or approval.
        fresh = self.observe(workspace)
        if fresh["signature"] != observation["signature"]:
            raise ValueError("Focus or window geometry changed; discard action and re-observe")
        guard()
        if action.kind == "launch_app":
            bundle = self.settings.apps[action.app]
            if action.app == "editor":
                target = path_resolver(action.path)
                self.allowed_titles.add(target.name)
                subprocess.run(["/usr/bin/open", "-b", bundle, str(target)], check=True, timeout=5)
            else:
                # Only a trusted local preview URL created by the controller.
                if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+/", action.path):
                    raise ValueError("Browser launch needs the controller's registered preview URL")
                profile = self.settings.state_dir / "browser-profile"
                self.allowed_preview_url = action.path
                subprocess.run(["/usr/bin/open", "-n", "-a", "Google Chrome", "--args", "--user-data-dir="+str(profile), "--no-first-run", "--disable-sync", "--disable-background-networking", "--force-renderer-accessibility", action.path], check=True, timeout=5)
        elif action.kind == "focus_app":
            bundle = self.settings.apps[action.app]
            if not re.fullmatch(r"[a-zA-Z0-9.-]+", bundle):
                raise ValueError("Invalid registered bundle ID")
            subprocess.run(["/usr/bin/osascript", "-e", 'tell application id "'+bundle+'" to activate'], check=True, timeout=5)
        elif action.kind in {"click", "double_click"}:
            point = input_point(observation, action.x, action.y)
            self._pointer(point)
            for click in range(2 if action.kind == "double_click" else 1):
                guard()
                for event_type in (self.Q.kCGEventLeftMouseDown, self.Q.kCGEventLeftMouseUp):
                    event = self.Q.CGEventCreateMouseEvent(None, event_type, point, self.Q.kCGMouseButtonLeft)
                    self.Q.CGEventSetIntegerValueField(event, self.Q.kCGMouseEventClickState, click+1)
                    self.Q.CGEventPost(self.Q.kCGHIDEventTap, event)
        elif action.kind == "type_text":
            # Unicode events do not expose or replace the user's clipboard. Lease/focus checked per chunk.
            for index in range(0, len(action.text), 8):
                guard()
                bundle, pid = self._active()
                current_window = self._window(pid)
                if bundle != self.settings.apps[observation["app"]] or not current_window or current_window["kCGWindowNumber"] != observation["window_id"]:
                    raise ValueError("Foreground changed during typing; outcome may be partial")
                ax_window = self._attr(self.AX.AXUIElementCreateApplication(pid), "AXFocusedWindow")
                if str(self._attr(ax_window, "AXDocument") or "") != observation["document"]:
                    raise ValueError("Editor document changed during typing; outcome may be partial")
                chunk = action.text[index:index+8]
                for down in (True, False):
                    event = self.Q.CGEventCreateKeyboardEvent(None, 0, down)
                    self.Q.CGEventKeyboardSetUnicodeString(event, len(chunk.encode("utf-16-le"))//2, chunk)
                    self.Q.CGEventPost(self.Q.kCGHIDEventTap, event)
        elif action.kind == "scroll":
            # Wheel events go to whatever window is under the pointer, so place it inside the approved window first.
            point = input_point(observation, *scroll_point(action, observation))
            self._pointer(point)
            event = self.Q.CGEventCreateScrollWheelEvent(None, self.Q.kCGScrollEventUnitLine, 1, action.amount)
            self.Q.CGEventSetLocation(event, point)
            self.Q.CGEventPost(self.Q.kCGHIDEventTap, event)
        elif action.kind == "hotkey":
            mapping = {"s": 1, "a": 0, "left": 123, "right": 124, "up": 126, "down": 125, "enter": 36, "backspace": 51, "tab": 48}
            for down in (True, False):
                event = self.Q.CGEventCreateKeyboardEvent(None, mapping[action.keys[-1]], down)
                if "command" in action.keys:
                    self.Q.CGEventSetFlags(event, self.Q.kCGEventFlagMaskCommand)
                self.Q.CGEventPost(self.Q.kCGHIDEventTap, event)
        elif action.kind == "wait":
            for _ in range(action.seconds * 10):
                guard()
                time.sleep(0.1)
        return {"dispatched": True, "effect_confirmed": False, "note": "Next observation records visible effects; dispatch is not proof of success"}

    def _pointer(self, point: tuple[float, float]):
        event = self.Q.CGEventCreateMouseEvent(None, self.Q.kCGEventMouseMoved, point, self.Q.kCGMouseButtonLeft)
        self.Q.CGEventPost(self.Q.kCGHIDEventTap, event)
        time.sleep(0.05)
