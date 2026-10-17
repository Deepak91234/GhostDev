from __future__ import annotations

import base64
import json
import time

import httpx

from .config import Settings
from .contracts import Action
from .prompt import OUTPUT_TOKENS, SYSTEM, build_context, decoding_schema


def processor_split(model: dict) -> str:
    """Same CPU/GPU split that `ollama ps` prints, from /api/ps sizes."""
    size, vram = model.get("size") or 0, model.get("size_vram") or 0
    if not size:
        return "unknown"
    gpu = round(100*vram/size)
    if gpu >= 100:
        return "100% GPU"
    if gpu <= 0:
        return "100% CPU"
    return f"{100-gpu}%/{gpu}% CPU/GPU"


def ollama_metrics(result: dict) -> dict:
    metric = {}
    for key, name in (("prompt_eval_count", "prompt_tokens"), ("eval_count", "output_tokens")):
        if isinstance(result.get(key), int):
            metric[name] = result[key]
    for key, name in (("total_duration", "total_seconds"), ("load_duration", "load_seconds"), ("prompt_eval_duration", "prompt_eval_seconds"), ("eval_duration", "eval_seconds")):
        if isinstance(result.get(key), (int, float)):
            metric[name] = round(result[key]/1e9, 3)
    if metric.get("output_tokens") and metric.get("eval_seconds"):
        metric["output_tokens_per_second"] = round(metric["output_tokens"]/metric["eval_seconds"], 1)
    return metric


class LocalModel:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport

    async def _request(self, method: str, path: str, **kwargs):
        # No ambient HTTP proxies and no redirect-following; local-only validation happens in Settings.
        async with httpx.AsyncClient(base_url=self.settings.model_url, timeout=self.settings.inference_timeout, trust_env=False, follow_redirects=False, transport=self.transport) as client:
            async with client.stream(method, path, **kwargs) as response:
                response.raise_for_status()
                if 300 <= response.status_code < 400:
                    raise ValueError("Model redirects are disabled")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 1_000_000:
                        raise ValueError("Model response exceeds the size limit")
                return json.loads(content)

    async def doctor(self) -> dict:
        self.settings.ready_model()
        if self.settings.model_backend == "ollama":
            info = await self._request("POST", "/api/show", json={"model": self.settings.model_name})
            capabilities = info.get("capabilities", [])
            if self.settings.model_vision and "vision" not in capabilities:
                raise ValueError("Configured model has not advertised vision; use verified AX/text mode or a supported VLM")
            # Reject cloud-remote models even if Ollama itself is on localhost.
            if info.get("remote_host") or info.get("remote_model"):
                raise ValueError("Ollama remote/cloud models are disabled")
            identity = {"model": self.settings.model_name, "capabilities": capabilities, "details": info.get("details", {})}
        else:
            info = await self._request("GET", "/models")
            if self.settings.model_name not in [item.get("id") for item in info.get("data", [])]:
                raise ValueError("Configured model is not in this local server's /models list")
            identity = {"model": self.settings.model_name, "capabilities": "must verify with live request"}
        identity.update({"publisher": self.settings.model_publisher, "location": "this Mac", "hardware_verified": False, "operator_evidence": self.settings.hardware_evidence, "note": "Loopback server is configured on this Mac; verify local processing with ollama ps and an offline rehearsal"})
        return identity

    async def runtime(self) -> dict:
        if self.settings.model_backend != "ollama":
            return {"backend": self.settings.model_backend, "note": "Check this server's own process and GPU tools"}
        version = await self._request("GET", "/api/version")
        loaded = await self._request("GET", "/api/ps")
        models = [{"name": item.get("name"), "size": item.get("size"), "size_vram": item.get("size_vram"), "processor": processor_split(item), "context_length": item.get("context_length"), "quantization": (item.get("details") or {}).get("quantization_level")} for item in loaded.get("models", [])]
        return {"ollama_version": version.get("version"), "loaded_models": models}

    async def propose(self, task: dict, observation: dict, image_bytes: bytes | None, history: list[dict], project: dict) -> tuple[Action, dict]:
        self.settings.ready_model()
        num_ctx = self.settings.model_context_tokens
        content, budget = build_context(task, observation, history, project, num_ctx)
        started = time.monotonic()
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
        if self.settings.model_backend == "ollama":
            if self.settings.model_vision and image_bytes:
                messages[-1]["images"] = [base64.b64encode(image_bytes).decode()]
            payload = {"model": self.settings.model_name, "messages": messages, "stream": False, "format": decoding_schema(), "options": {"temperature": 0, "num_predict": OUTPUT_TOKENS, "num_ctx": num_ctx}}
            if self.settings.model_think is not None:
                payload["think"] = self.settings.model_think
            result = await self._request("POST", "/api/chat", json=payload)
            raw = result.get("message", {}).get("content", "")
            returned_model = result.get("model", self.settings.model_name)
            stop_reason = result.get("done_reason")
            usage = ollama_metrics(result)
        else:
            if self.settings.model_vision and image_bytes:
                messages[-1]["content"] = [{"type": "text", "text": content}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image_bytes).decode()}}]
            result = await self._request("POST", "/chat/completions", json={"model": self.settings.model_name, "messages": messages, "temperature": 0, "max_tokens": OUTPUT_TOKENS, "response_format": {"type": "json_schema", "json_schema": {"name": "desktop_action", "strict": True, "schema": Action.model_json_schema()}}})
            raw = result["choices"][0]["message"]["content"]
            returned_model = result.get("model", self.settings.model_name)
            stop_reason = result["choices"][0].get("finish_reason")
            reported = result.get("usage") or {}
            usage = {name: reported[key] for key, name in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "output_tokens")) if isinstance(reported.get(key), int)}
        if stop_reason == "length":
            raise ValueError("Model output hit its token limit before completing one JSON action")
        # Server counts can only under-report (prompt caching), so reaching num_ctx means something was cut.
        if usage.get("prompt_tokens", 0)+usage.get("output_tokens", 0) >= num_ctx:
            raise ValueError("Model context was exhausted; the prompt may have been truncated")
        action = Action.model_validate_json(raw)
        return action, {"model": returned_model, "elapsed_seconds": round(time.monotonic()-started, 3), "backend": self.settings.model_backend, "hardware_verified": False, **budget, **usage}
