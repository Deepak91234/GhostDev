"""On-Mac model benchmark for one fixed editor decision.

Measures the configured local model's latency, token throughput, JSON validity and whether its
click lands on the right element. It sends no desktop input, so it is not evidence of native
input, screen capture, or task completion.
"""
from __future__ import annotations

import datetime
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid

import httpx

from .config import Settings
from .desktop import element_center
from .model import LocalModel

SCOPE = "Model-only benchmark on a fixed editor observation. No desktop input was sent; this is not evidence of native input, capture, or task completion."
REPORT_TEXT = "GhostDev — local desktop demonstration\n\nSummary\nThis note belongs to the prepared GhostDev task workspace.\nAdd the requested note here, save, and open the preview.\n"
TASK = {"instruction": "Click the report's text area so the note can be typed next. Do not type yet.", "task_type": "desktop", "expected_text": "Local AI keeps my work on my own machines"}
PROJECT = {"id": "bench", "files": ["GhostDev Report.txt"], "entry_file": "GhostDev Report.txt", "preview_file": "report.html", "checks": [], "apps": ["editor", "browser"]}
TEXT_AREA = 6


def observation() -> dict:
    # A TextEdit plain-text window as the Accessibility walk reports it, in image coordinates.
    elements = [
        {"role": "AXWindow", "title": "GhostDev Report.txt", "bounds": [0, 0, 1280, 972]},
        {"role": "AXButton", "title": "close button", "bounds": [18, 14, 18, 18]},
        {"role": "AXButton", "title": "minimize button", "bounds": [44, 14, 18, 18]},
        {"role": "AXButton", "title": "full screen button", "bounds": [70, 14, 18, 18]},
        {"role": "AXStaticText", "value": "GhostDev Report.txt", "bounds": [560, 12, 160, 22]},
        {"role": "AXScrollArea", "title": "", "bounds": [0, 44, 1280, 928]},
        {"role": "AXTextArea", "title": "", "value": REPORT_TEXT, "bounds": [0, 44, 1262, 928]},
        {"role": "AXScrollBar", "title": "", "bounds": [1262, 44, 18, 928]},
        {"role": "AXValueIndicator", "title": "", "bounds": [1264, 46, 14, 300]},
    ]
    return {"id": uuid.uuid4().hex, "timestamp": time.time(), "app": "editor", "capture_allowed": True, "title": "GhostDev Report.txt", "document": "file:///bench/project/GhostDev%20Report.txt", "image_width": 1280, "image_height": 972, "bounds": {"X": 120, "Y": 80, "Width": 1000, "Height": 760}, "elements": elements, "focus_role": "AXTextArea", "signature": "bench"}


def grade(action, current: dict) -> dict:
    if action.observation_id != current["id"] or action.expected_app != current["app"]:
        return {"valid": True, "grounded": False, "reason": "Action is not bound to the current observation and app"}
    if action.kind != "click":
        return {"valid": True, "grounded": False, "reason": f"Proposed {action.kind}; a click on the text area was expected"}
    try:
        x, y = element_center(current, action.target) if action.target >= 0 else (action.x, action.y)
    except ValueError as exc:
        return {"valid": True, "grounded": False, "reason": str(exc)}
    left, top, width, height = current["elements"][TEXT_AREA]["bounds"]
    inside = left <= x < left+width and top <= y < top+height
    return {"valid": True, "grounded": inside, "point": [x, y], "used_target": action.target >= 0, **({} if inside else {"reason": "Click lands outside the text area"})}


def host() -> dict:
    info = {"platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count(), "python": platform.python_version()}
    if sys.platform == "darwin":
        info["macos"] = platform.mac_ver()[0]
        for key, name in (("machdep.cpu.brand_string", "cpu"), ("hw.model", "hardware_model"), ("hw.memsize", "memory_bytes")):
            try:
                value = subprocess.run(["/usr/sbin/sysctl", "-n", key], capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                continue
            if value:
                info[name] = int(value) if name == "memory_bytes" and value.isdigit() else value
    elif hasattr(os, "sysconf"):
        try:
            info["memory_bytes"] = os.sysconf("SC_PAGE_SIZE")*os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            pass
    return info


def summarize(runs: list[dict]) -> dict:
    seconds = [run["wall_seconds"] for run in runs]
    rates = [run["inference"]["output_tokens_per_second"] for run in runs if run.get("inference", {}).get("output_tokens_per_second")]
    # The first request usually includes loading weights; report warm latency separately.
    warm = seconds[1:] or seconds
    return {"runs": len(runs), "valid_json": sum(run["valid"] for run in runs), "grounded": sum(run["grounded"] for run in runs), "first_seconds": seconds[0] if seconds else None, "warm_median_seconds": round(statistics.median(warm), 3) if warm else None, "max_seconds": max(seconds) if seconds else None, "median_output_tokens_per_second": round(statistics.median(rates), 1) if rates else None}


async def run_bench(settings: Settings, runs: int, transport=None) -> dict:
    model = LocalModel(settings, transport)
    report = {"evidence_scope": SCOPE, "started": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "host": host(), "model": await model.doctor(), "settings": {"model_context_tokens": settings.model_context_tokens, "model_think": settings.model_think, "model_vision": settings.model_vision}, "runs": []}
    for index in range(runs):
        current = observation()
        started = time.monotonic()
        try:
            action, metric = await model.propose(TASK, current, None, [], PROJECT)
            outcome = {**grade(action, current), "action": action.model_dump(include={"kind", "target", "x", "y", "expected_app", "summary"}), "inference": metric}
        except (ValueError, httpx.HTTPError) as exc:
            outcome = {"valid": False, "grounded": False, "reason": str(exc)[:300], "inference": {}}
        report["runs"].append({"run": index+1, "wall_seconds": round(time.monotonic()-started, 3), **outcome})
    # Read after the runs so the configured model is loaded and its CPU/GPU split is visible.
    report["runtime"] = await model.runtime()
    report["summary"] = summarize(report["runs"])
    return report
