from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    root: Path
    writable: list[str] = ["*.txt", "*.md", "*.js", "*.html", "*.css", "*.json"]
    checks: dict[str, list[str]] = {}
    required_checks: list[str] = []
    image: str = "ghostdev-runner:local"
    entry_file: str = "GhostDev Report.txt"
    preview_file: str = "index.html"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state_dir: Path = Path(".state")
    model_backend: str = "ollama"
    model_url: str = "http://127.0.0.1:11434"
    model_name: str = "qwen3:4b"
    model_publisher: str = "Qwen"
    model_vision: bool = False
    model_think: bool | None = False
    model_context_tokens: int = Field(default=8192, ge=4096, le=32768)
    # Exact process/GPU evidence is supplied by the operator, never inferred from branding.
    hardware_evidence: str = ""
    origins: list[str] = ["http://127.0.0.1:8765", "http://localhost:8765"]
    secure_cookies: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    max_steps: int = Field(default=40, ge=1, le=80)
    task_timeout: int = Field(default=600, ge=10, le=1800)
    inference_timeout: int = Field(default=90, ge=1, le=300)
    observation_max_age: int = Field(default=60, ge=1, le=120)
    apps: dict[str, str] = {"editor": "com.apple.TextEdit", "browser": "com.google.Chrome"}
    projects: list[Project] = []
    web_dir: Path = Path("apps/mobile/dist")

    @field_validator("model_url")
    @classmethod
    def same_machine_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use a Mac-local model endpoint without credentials/query parameters")
        # Literal loopback only: the product no longer uses a separate inference machine.
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as exc:
            raise ValueError("Use 127.0.0.1 or ::1 for inference on this Mac") from exc
        if not address.is_loopback:
            raise ValueError("External inference hardware and hosted model endpoints are disabled")
        return value.rstrip("/")

    @field_validator("model_backend")
    @classmethod
    def backend(cls, value: str) -> str:
        if value not in {"ollama", "openai-compatible"}:
            raise ValueError("Only configured local Ollama or OpenAI-compatible servers")
        return value

    def project(self, project_id: str) -> Project:
        for item in self.projects:
            if item.id == project_id:
                return item
        raise ValueError("Project is not locally registered")

    def ready_model(self) -> None:
        if not self.model_name or self.model_name.startswith("SET_") or "cloud" in self.model_name.lower():
            raise ValueError("Configure a locally downloaded model on this Mac before submitting tasks")


def load_settings(path: str | None = None) -> Settings:
    source = Path(path or os.environ.get("GHOSTDEV_CONFIG", "config.json")).resolve()
    if not source.exists():
        raise ValueError("Run `ghostdev init` first; edit config.json before starting")
    settings = Settings.model_validate_json(source.read_text())
    for key in ("state_dir", "web_dir"):
        value = getattr(settings, key)
        if not value.is_absolute():
            setattr(settings, key, (source.parent / value).resolve())
    for project in settings.projects:
        if not project.root.is_absolute():
            project.root = (source.parent / project.root).resolve()
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.state_dir.chmod(0o700)
    return settings
