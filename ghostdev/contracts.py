from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Action(Strict):
    kind: Literal["launch_app", "focus_app", "click", "double_click", "type_text", "scroll", "hotkey", "wait", "list_files", "read_file", "search_code", "apply_patch", "run_check", "start_preview", "export_file", "finish"]
    observation_id: str
    expected_app: str
    summary: str = Field(max_length=300)
    postcondition: str = Field(max_length=300)
    app: str = ""
    path: str = ""
    x: int = Field(default=0, ge=0, le=10000)
    y: int = Field(default=0, ge=0, le=10000)
    text: str = Field(default="", max_length=8000)
    amount: int = Field(default=0, ge=-8, le=8)
    keys: list[str] = Field(default_factory=list, max_length=3)
    seconds: int = Field(default=1, ge=1, le=3)
    check_id: str = ""
    old: str = Field(default="", max_length=20000)
    new: str = Field(default="", max_length=20000)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


class TaskRequest(Strict):
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    instruction: str = Field(min_length=3, max_length=4000)
    project_id: str
    mode: Literal["review-first", "bounded-task"] = "review-first"
    task_type: Literal["desktop", "coding", "inspect"] = "desktop"
    expected_text: str = Field(default="", max_length=2000)


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "DISCARDED", "INTERRUPTED"}
LIVE = {"QUEUED", "RUNNING", "PAUSED", "AWAITING_APPROVAL", "VERIFYING", "APPLYING"}
TRANSITIONS = {
    "QUEUED": {"RUNNING", "CANCELLED", "INTERRUPTED"},
    "RUNNING": {"PAUSED", "AWAITING_APPROVAL", "VERIFYING", "FAILED", "CANCELLED", "INTERRUPTED"},
    "AWAITING_APPROVAL": {"RUNNING", "PAUSED", "CANCELLED", "FAILED", "INTERRUPTED"},
    "PAUSED": {"RUNNING", "CANCELLED", "INTERRUPTED", "FAILED"},
    "VERIFYING": {"COMPLETED", "READY_FOR_REVIEW", "FAILED", "CANCELLED", "INTERRUPTED", "PAUSED"},
    "READY_FOR_REVIEW": {"APPLYING", "DISCARDED"},
    "APPLYING": {"COMPLETED", "READY_FOR_REVIEW", "INTERRUPTED"},
}
