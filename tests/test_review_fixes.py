"""Regression tests for defects found by the adversarial review of the reliability branch."""
import asyncio
import json
import time
import uuid

import httpx
import pytest
from pydantic import ValidationError

import ghostdev.worker as worker_module
from ghostdev.config import Settings
from ghostdev.contracts import Action
from ghostdev.db import LeaseRevoked
from ghostdev.desktop import MacDesktop, element_center, visible_box
from ghostdev.model import LocalModel
from ghostdev.prompt import COMMON, KIND_FIELDS, build_context, decoding_schema
from ghostdev.worker import Worker
from conftest import request
from test_local_model_reliability import action, chat_handler, observation
from test_model_worker import FakeDesktop, FixtureModel


def test_decoding_schema_forces_each_kinds_arguments():
    schema = decoding_schema()
    branches = {branch["properties"]["kind"]["const"]: branch for branch in schema["anyOf"]}
    assert set(branches) == set(Action.model_fields["kind"].annotation.__args__)
    for kind, branch in branches.items():
        assert branch["required"] == COMMON+KIND_FIELDS[kind] and branch["additionalProperties"] is False
        # Arguments come after summary/postcondition, so the decoder states its intent before choosing them.
        assert list(branch["properties"])[:5] == COMMON
    assert branches["click"]["required"][-1] == "target" and "x" not in branches["click"]["required"]
    assert branches["launch_app"]["required"][-2:] == ["app", "path"] and branches["type_text"]["required"][-1] == "text"


@pytest.mark.parametrize("kind", list(KIND_FIELDS))
def test_every_decoded_branch_is_a_valid_action(kind):
    samples = {"app": "editor", "path": "GhostDev Report.txt", "target": 3, "text": "note", "amount": -3, "keys": ["command", "s"], "seconds": 1, "check_id": "cart", "old": "a", "new": "b", "x": 5, "y": 6}
    branch = next(b for b in decoding_schema()["anyOf"] if b["properties"]["kind"]["const"] == kind)
    payload = {"kind": kind, "observation_id": "o", "expected_app": "editor", "summary": "s", "postcondition": "p", **{name: samples[name] for name in branch["properties"] if name not in COMMON}}
    assert Action.model_validate_json(json.dumps(payload)).kind == kind


def test_ollama_receives_per_kind_schema_and_prompt_lists_kind_fields(environment):
    settings = environment[0]
    seen = []
    def handle(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": json.dumps({"kind": "finish", "observation_id": "abc", "expected_app": "editor", "summary": "Verify", "postcondition": "Check"})}, "done_reason": "stop"})
    asyncio.run(LocalModel(settings, httpx.MockTransport(handle)).propose(request(), observation(id="abc"), None, [], {}))
    assert seen[0]["format"] == decoding_schema()
    guide = json.loads(seen[0]["messages"][-1]["content"])["action_format"]
    assert guide["by_kind"]["click"]["target"] == "integer -1..499" and guide["by_kind"]["launch_app"] == {"app": "string", "path": "string"}


def test_click_without_target_or_coordinates_is_rejected(environment):
    with pytest.raises(ValueError, match="needs a target"):
        MacDesktop(environment[0]).validate(action(), observation())


def test_off_viewport_strips_are_not_click_targets(environment):
    # Chrome reports an element scrolled out of view as a strip 1 pt high at the viewport edge.
    strip = observation([{"role": "AXButton", "title": "+", "bounds": [300, 76, 40, 1]}])
    assert visible_box([300, 76, 40, 1], 640, 400) is None
    with pytest.raises(ValueError, match="not visible"):
        element_center(strip, 0)


def test_newest_tool_result_keeps_its_budget_after_a_rejection():
    source = "x"*1184
    read = Action(kind="read_file", observation_id="o", expected_app="editor", summary="Read", postcondition="Seen", path="index.html").model_dump()
    history = [{"action": read, "result": {"path": "index.html", "text": source}}, {"rejected": "Patch context is missing or ambiguous"}]
    content, _ = build_context(request(task_type="coding"), observation(), history, {}, 8192)
    assert json.loads(content)["history"][0]["result"]["text"] == source


def test_context_minimum_is_8192():
    with pytest.raises(ValidationError):
        Settings(model_context_tokens=4096)


def test_denial_returns_to_running_and_replans(environment):
    settings, store, _ = environment
    async def scenario():
        calls = []
        replanned = asyncio.Event()
        class Model(FixtureModel):
            async def propose(self, task, observed, image, history, project):
                calls.append(list(history))
                if len(calls) == 1:
                    return Action(kind="launch_app", app="editor", path="GhostDev Report.txt", observation_id=observed["id"], expected_app=observed["app"], summary="Open report", postcondition="Editor open"), {}
                replanned.set()
                await asyncio.sleep(100)
        task = store.submit(request(mode="review-first"))
        store.claim()
        running = asyncio.create_task(Worker(settings, store, FakeDesktop(settings), Model()).run_task(task))
        for _ in range(100):
            with store.connect() as db:
                pending = db.execute("SELECT id FROM approvals WHERE status='pending'").fetchone()
            if pending:
                break
            await asyncio.sleep(0.05)
        with store.connect() as db:
            db.execute("UPDATE approvals SET status='denied' WHERE id=?", (pending[0],))
        await asyncio.wait_for(replanned.wait(), 3)
        assert store.task(task["id"])["state"] == "RUNNING"
        assert any("Owner denied" in str(entry) for entry in calls[1])
        store.transition(task["id"], "CANCELLED", revoke=True)
        await asyncio.wait_for(running, 3)
    asyncio.run(scenario())


def test_approval_cap_expires_and_pauses_instead_of_spinning(environment, monkeypatch):
    settings, store, _ = environment
    frozen = time.time()
    class Clock:
        """Wall clock stuck (stepped back) while monotonic time keeps running fast."""
        ticks = 0.0
        @staticmethod
        def monotonic():
            Clock.ticks += 30
            return Clock.ticks
        time = staticmethod(lambda: frozen)
    monkeypatch.setattr(worker_module, "time", Clock)
    async def scenario():
        task = store.submit(request(mode="review-first"))
        store.claim()
        observed = observation(id=uuid.uuid4().hex, timestamp=frozen)
        store.update(task["id"], observation=observed)
        proposed = MacDesktop(settings).resolve_target(action(observation_id=observed["id"], target=0), observed)
        with pytest.raises(LeaseRevoked):
            await Worker(settings, store, FakeDesktop(settings), FixtureModel()).approval(task["id"], 0, proposed)
        with store.connect() as db:
            assert db.execute("SELECT status FROM approvals").fetchone()[0] == "expired"
        assert store.task(task["id"])["state"] == "PAUSED"
    asyncio.run(scenario())


def test_approval_never_overrides_a_pause_that_landed_first(environment):
    _, store, _ = environment
    task = store.submit(request())
    store.claim()
    store.transition(task["id"], "AWAITING_APPROVAL")
    store.transition(task["id"], "PAUSED", revoke=True)
    with pytest.raises(LeaseRevoked):
        store.transition(task["id"], "RUNNING", expect=("AWAITING_APPROVAL", 0))
    assert store.task(task["id"])["state"] == "PAUSED"


def test_failed_inference_counts_toward_the_rejection_stop(environment):
    settings, store, _ = environment
    settings = settings.model_copy(update={"max_steps": 5, "task_timeout": 10})
    chats = []
    def handle(req):
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"]})
        chats.append(1)
        return httpx.Response(200, json={"message": {"content": "{"}, "done_reason": "length"})
    task = store.submit(request())
    store.claim()
    asyncio.run(Worker(settings, store, FakeDesktop(settings), LocalModel(settings, httpx.MockTransport(handle))).run_task(task))
    result = store.task(task["id"])
    assert result["state"] == "FAILED" and "Repeated invalid" in result["error"] and len(chats) == 3


def test_bench_names_missing_target_and_flags_vision(environment):
    from ghostdev import bench
    settings = environment[0]
    def bare(payload):
        current = json.loads(payload["messages"][-1]["content"])["observation"]
        return {"kind": "click", "observation_id": current["id"], "expected_app": "editor", "summary": "Click", "postcondition": "Focus"}
    run = asyncio.run(bench.run_bench(settings, 1, httpx.MockTransport(chat_handler(settings, bare))))["runs"][0]
    assert "no target element and no coordinates" in run["reason"]
    vision = settings.model_copy(update={"model_vision": True})
    def handle(req):
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "vision"]})
        return chat_handler(vision, bare)(req)
    report = asyncio.run(bench.run_bench(vision, 1, httpx.MockTransport(handle)))
    assert report["settings"]["image_sent"] is False and "text-only" in report["evidence_scope"]


def test_bench_cli_says_model_missing_rather_than_server_down(environment, tmp_path, monkeypatch, capsys):
    import sys
    from ghostdev import bench
    from ghostdev.cli import main
    settings = environment[0]
    config = tmp_path/"config.json"
    config.write_text(settings.model_copy(update={"projects": []}).model_dump_json())
    missing = httpx.MockTransport(lambda req: httpx.Response(404, json={"error": f"model '{settings.model_name}' not found"}))
    monkeypatch.setattr(bench, "LocalModel", lambda value, transport=None: LocalModel(value, missing))
    monkeypatch.setattr(sys, "argv", ["ghostdev", "--config", str(config), "bench", "--runs", "1"])
    with pytest.raises(SystemExit) as exited:
        main()
    message = capsys.readouterr().err
    assert exited.value.code == 1 and "HTTP 404" in message and "ollama pull" in message and "unreachable" not in message
