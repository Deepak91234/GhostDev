import asyncio
import json
import stat
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from ghostdev import bench
from ghostdev.cli import main
from ghostdev.contracts import Action
from ghostdev.db import LeaseRevoked
from ghostdev.desktop import MacDesktop, scroll_point
from ghostdev.model import LocalModel, processor_split
from ghostdev.prompt import OUTPUT_TOKENS, MARGIN_TOKENS, build_context, clip, estimate_tokens
from ghostdev.worker import Worker
from conftest import request
from test_model_worker import FakeDesktop, FixtureModel


def observation(elements=None, **overrides):
    return {"id": "current", "app": "editor", "timestamp": time.time(), "capture_allowed": True, "image_width": 640, "image_height": 400, "bounds": {"X": 0, "Y": 0, "Width": 640, "Height": 400}, "focus_role": "AXTextArea", "elements": elements if elements is not None else [{"role": "AXTextArea", "value": "Report", "bounds": [100, 50, 200, 100]}], **overrides}


def action(**fields):
    return Action(**{"kind": "click", "observation_id": "current", "expected_app": "editor", "summary": "Click", "postcondition": "Focused", **fields})


def coding_history():
    read = Action(kind="read_file", observation_id="old", expected_app="editor", summary="Read", postcondition="Seen", path="index.html").model_dump()
    check = {"check_id": "cart", "argv": ["node", "/verification/cart-check.cjs"], "exit_code": 1, "output": "FAIL quantity=3: expected 30; received 10\n"*300+'{"harness":"ghostdev-cart-v1","tests":3,"passed":1}', "passed": False, "source_hash": "f"*64}
    return [{"action": read, "result": {"path": "index.html", "text": "x"*16000}}, {"action": read, "result": check}]*3


def large_observation():
    elements = [{"role": "AXStaticText", "value": f"Preview line {i} with ordinary page text", "bounds": [10, 2*i, 400, 18]} for i in range(139)]
    elements.append({"role": "AXTextArea", "value": "Report", "bounds": [100, 50, 200, 100]})
    return observation(elements, signature="s"*300, document="file:///Users/demo/.state/tasks/abc/project/GhostDev%20Report.txt")


def test_worst_case_coding_prompt_fits_context_without_losing_task():
    task = {"instruction": "Fix calculateTotal "+"carefully "*100, "task_type": "coding", "expected_text": "Exact note"}
    content, stats = build_context(task, large_observation(), coding_history(), {"id": "cart", "files": ["cart.js"]}, 8192)
    assert stats["estimated_prompt_tokens"] <= 8192-OUTPUT_TOKENS-MARGIN_TOKENS
    assert stats["context_stage"] > 0 and stats["elements_sent"] < stats["elements_total"]
    context = json.loads(content)
    assert context["instruction"] == task["instruction"] and context["expected_text"] == "Exact note"
    # The check's final summary line survives clipping; controller-only fields never reach the model.
    assert '"passed":1}' in context["history"][-1]["result"]["output"]
    assert "argv" not in context["history"][-1]["result"] and "source_hash" not in content
    assert context["observation"]["document"] == "GhostDev Report.txt" and "signature" not in context["observation"]
    # The only interactive element is kept even though it is last in tree order.
    assert any(item["role"] == "AXTextArea" and item["i"] == 139 for item in context["observation"]["elements"])


def test_small_context_sends_everything_uncut_when_it_fits():
    content, stats = build_context(request(), observation(), [], {"id": "cart"}, 8192)
    assert stats["context_stage"] == 0 and stats["elements_sent"] == stats["elements_total"] == 1
    assert json.loads(content)["observation"]["elements"][0] == {"i": 0, "role": "AXTextArea", "value": "Report", "bounds": [100, 50, 200, 100]}


def test_context_that_cannot_fit_is_refused_not_truncated():
    with pytest.raises(ValueError, match="cannot fit"):
        build_context(request(instruction="x"*4000, expected_text="y"*2000), observation(), [], {}, 4096)


def test_clip_keeps_head_and_tail():
    clipped = clip("HEAD"+"-"*1000+"TAIL", 60)
    assert clipped.startswith("HEAD") and clipped.endswith("TAIL") and "omitted" in clipped
    assert estimate_tokens("ab€") == 2
    # Digits are one token each in Qwen-style tokenizers.
    assert estimate_tokens("[1262, 44, 18, 928]") >= 11


def test_target_resolves_to_visible_center_before_approval(environment):
    desktop = MacDesktop(environment[0])
    resolved = desktop.resolve_target(action(target=0, x=5, y=5), observation())
    assert (resolved.x, resolved.y) == (200, 100)
    desktop.validate(resolved, observation())
    # A partly off-window element is clicked in its visible part.
    partial = observation([{"role": "AXButton", "title": "Plus", "bounds": [600, 380, 100, 100]}])
    assert (desktop.resolve_target(action(target=0), partial).x, desktop.resolve_target(action(target=0), partial).y) == (620, 390)


@pytest.mark.parametrize("elements,target,kind,message", [
    ([{"role": "AXButton", "title": "Hidden", "bounds": [700, 500, 20, 20]}], 0, "click", "not visible"),
    ([{"role": "AXButton", "title": "No bounds"}], 0, "click", "not visible"),
    (None, 3, "click", "not in the current observation"),
    (None, 0, "type_text", "only to click"),
])
def test_bad_targets_are_rejected(environment, elements, target, kind, message):
    with pytest.raises(ValueError, match=message):
        MacDesktop(environment[0]).resolve_target(action(kind=kind, target=target, text="x"), observation(elements))


def test_coordinates_must_still_match_the_approved_target(environment):
    desktop = MacDesktop(environment[0])
    resolved = desktop.resolve_target(action(target=0), observation())
    with pytest.raises(ValueError, match="do not match"):
        desktop.validate(resolved.model_copy(update={"x": 10}), observation())
    moved = observation([{"role": "AXTextArea", "value": "Report", "bounds": [300, 50, 200, 100]}])
    with pytest.raises(ValueError, match="do not match"):
        desktop.validate(resolved, moved)


def test_scroll_is_routed_into_the_approved_window(environment):
    desktop = MacDesktop(environment[0])
    assert scroll_point(action(kind="scroll", amount=-3), observation()) == (320, 200)
    targeted = desktop.resolve_target(action(kind="scroll", amount=-3, target=0), observation())
    assert scroll_point(targeted, observation()) == (200, 100)
    with pytest.raises(ValueError, match="Scroll point outside"):
        desktop.validate(action(kind="scroll", amount=-3, x=900, y=10), observation())


def chat_handler(settings, content, **extra):
    def handle(req):
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"]})
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.30.1"})
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": settings.model_name, "size": 4_000_000_000, "size_vram": 4_000_000_000, "context_length": 8192, "details": {"quantization_level": "Q4_K_M"}}]})
        body = content(json.loads(req.content)) if callable(content) else content
        return httpx.Response(200, json={"model": settings.model_name, "message": {"content": json.dumps(body)}, "done_reason": "stop", "prompt_eval_count": 1500, "eval_count": 60, "total_duration": 2_500_000_000, "load_duration": 100_000_000, "prompt_eval_duration": 1_200_000_000, "eval_duration": 1_000_000_000, **extra})
    return handle


def finish(observation_id="abc"):
    return {"kind": "finish", "observation_id": observation_id, "expected_app": "editor", "summary": "Verify", "postcondition": "Check output"}


def test_server_token_and_latency_metrics_are_recorded(environment):
    settings = environment[0]
    model = LocalModel(settings, httpx.MockTransport(chat_handler(settings, finish())))
    _, metric = asyncio.run(model.propose(request(), observation(id="abc"), None, [], {}))
    assert metric["prompt_tokens"] == 1500 and metric["output_tokens"] == 60 and metric["output_tokens_per_second"] == 60.0
    assert metric["total_seconds"] == 2.5 and metric["load_seconds"] == 0.1 and metric["context_stage"] == 0


@pytest.mark.parametrize("extra,message", [({"done_reason": "length"}, "token limit"), ({"prompt_eval_count": 8150, "eval_count": 42}, "exhausted")])
def test_truncated_generation_or_context_is_rejected(environment, extra, message):
    settings = environment[0]
    model = LocalModel(settings, httpx.MockTransport(chat_handler(settings, finish(), **extra)))
    with pytest.raises(ValueError, match=message):
        asyncio.run(model.propose(request(), observation(id="abc"), None, [], {}))


def test_openai_compatible_length_stop_is_rejected(environment):
    settings = environment[0].model_copy(update={"model_backend": "openai-compatible", "model_url": "http://127.0.0.1:18000/v1"})
    response = {"choices": [{"message": {"content": "{\"kind\":"}, "finish_reason": "length"}], "usage": {"prompt_tokens": 900, "completion_tokens": 2048}}
    model = LocalModel(settings, httpx.MockTransport(lambda req: httpx.Response(200, json=response)))
    with pytest.raises(ValueError, match="token limit"):
        asyncio.run(model.propose(request(), observation(id="abc"), None, [], {}))


def test_processor_split_matches_ollama_ps():
    assert processor_split({"size": 10, "size_vram": 10}) == "100% GPU"
    assert processor_split({"size": 10, "size_vram": 0}) == "100% CPU"
    assert processor_split({"size": 100, "size_vram": 52}) == "48%/52% CPU/GPU"
    assert processor_split({}) == "unknown"


class TargetedDesktop(FakeDesktop):
    """TEST ONLY: the fake adapter with element bounds, so targets can be resolved."""
    def observe(self, workspace):
        result = super().observe(workspace)
        result["elements"] = [{"role": "AXStaticText", "value": self.note}, {"role": "AXTextArea", "value": self.note, "bounds": [40, 60, 400, 300]}]
        return result


def test_worker_records_resolved_target_with_the_proposal(environment):
    settings, store, _ = environment
    class Targeting(FixtureModel):
        async def propose(self, task, observation, image, history, project):
            if self.index == 1:
                self.index += 1
                return Action(kind="click", target=1, observation_id=observation["id"], expected_app=observation["app"], summary="Click the report text", postcondition="Text area focused"), {}
            return await super().propose(task, observation, image, history, project)
    task = store.submit(request())
    store.claim()
    asyncio.run(Worker(settings, store, TargetedDesktop(settings), Targeting()).run_task(task))
    assert store.task(task["id"])["state"] == "COMPLETED", store.task(task["id"]).get("error")
    click = next(e["payload"] for e in store.events(task["id"]) if e["type"] == "action_proposed" and e["payload"]["action"]["kind"] == "click")
    assert click["target"] == {"index": 1, "role": "AXTextArea", "label": "", "x": 240, "y": 210}
    assert (click["action"]["x"], click["action"]["y"]) == (240, 210)


def test_approval_request_shows_the_target_element(environment):
    settings, store, _ = environment
    async def scenario():
        task = store.submit(request(mode="review-first"))
        store.claim()
        observed = observation(id=uuid.uuid4().hex)
        store.update(task["id"], observation=observed)
        proposed = MacDesktop(settings).resolve_target(action(observation_id=observed["id"], target=0), observed)
        target = Worker.target_summary(proposed, observed)
        waiting = asyncio.create_task(Worker(settings, store, FakeDesktop(settings), FixtureModel()).approval(task["id"], 0, proposed, target))
        await asyncio.sleep(0.3)
        event = next(e["payload"] for e in store.events(task["id"]) if e["type"] == "approval_required")
        assert event["target"] == {"index": 0, "role": "AXTextArea", "label": "Report", "x": 200, "y": 100}
        assert event["digest"] == proposed.digest()
        store.transition(task["id"], "CANCELLED", revoke=True)
        with pytest.raises(LeaseRevoked):
            await waiting
    asyncio.run(scenario())


def test_bench_reports_grounding_latency_and_processor(environment):
    settings = environment[0]
    def click(payload):
        current = json.loads(payload["messages"][-1]["content"])["observation"]
        return {"kind": "click", "target": bench.TEXT_AREA, "observation_id": current["id"], "expected_app": "editor", "summary": "Click report text", "postcondition": "Text area focused"}
    report = asyncio.run(bench.run_bench(settings, 2, httpx.MockTransport(chat_handler(settings, click))))
    assert report["summary"]["runs"] == 2 and report["summary"]["grounded"] == 2 and report["summary"]["valid_json"] == 2
    assert report["runtime"]["ollama_version"] == "0.30.1" and report["runtime"]["loaded_models"][0]["processor"] == "100% GPU"
    assert report["runs"][0]["used_target"] and report["host"]["cpu_count"] and "not evidence" in report["evidence_scope"]


def test_bench_flags_misplaced_clicks_and_invalid_output(environment):
    settings = environment[0]
    def wrong(payload):
        current = json.loads(payload["messages"][-1]["content"])["observation"]
        return {"kind": "click", "target": 1, "observation_id": current["id"], "expected_app": "editor", "summary": "Click", "postcondition": "Closed"}
    report = asyncio.run(bench.run_bench(settings, 1, httpx.MockTransport(chat_handler(settings, wrong))))
    assert report["runs"][0]["grounded"] is False and "outside the text area" in report["runs"][0]["reason"]
    report = asyncio.run(bench.run_bench(settings, 1, httpx.MockTransport(chat_handler(settings, {"kind": "shell"}))))
    assert report["runs"][0]["valid"] is False and report["summary"]["grounded"] == 0


def test_bench_cli_writes_private_report_and_exit_status(environment, tmp_path, monkeypatch, capsys):
    settings = environment[0]
    config = tmp_path/"config.json"
    config.write_text(settings.model_copy(update={"projects": []}).model_dump_json())
    def click(payload):
        current = json.loads(payload["messages"][-1]["content"])["observation"]
        return {"kind": "click", "x": 5, "y": 5, "observation_id": current["id"], "expected_app": "editor", "summary": "Click", "postcondition": "Focus"}
    monkeypatch.setattr(bench, "LocalModel", lambda value, transport=None: LocalModel(value, httpx.MockTransport(chat_handler(value, click))))
    monkeypatch.setattr(sys, "argv", ["ghostdev", "--config", str(config), "bench", "--runs", "1"])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 3
    printed = json.loads(capsys.readouterr().out)
    path = Path(printed["report"])
    assert path.parent == settings.state_dir/"bench" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["summary"]["grounded"] == 0
    # The terminal output names each miss, so the report file is not needed to see why.
    assert printed["failed_runs"] == [{"run": 1, "reason": "Click lands outside the text area", "action": {"kind": "click", "target": -1, "x": 5, "y": 5, "expected_app": "editor", "summary": "Click", "text": ""}}]


@pytest.mark.parametrize("change,reason", [
    ({"observation_id": "invented"}, "observation_id 'invented' does not match"),
    ({"expected_app": "TextEdit"}, "expected_app is 'TextEdit'; the current app is 'editor'"),
    ({"kind": "type_text", "text": "Local AI keeps my work on my own machines"}, "Proposed type_text"),
])
def test_bench_names_the_exact_reason_a_run_missed(environment, change, reason):
    settings = environment[0]
    def proposal(payload):
        current = json.loads(payload["messages"][-1]["content"])["observation"]
        return {"kind": "click", "target": bench.TEXT_AREA, "observation_id": current["id"], "expected_app": "editor", "summary": "Click", "postcondition": "Focus", **change}
    run = asyncio.run(bench.run_bench(settings, 1, httpx.MockTransport(chat_handler(settings, proposal))))["runs"][0]
    assert run["grounded"] is False and reason in run["reason"]
    assert run["action"]["text"] == change.get("text", "")


def test_owner_approval_time_does_not_consume_the_task_deadline(environment, monkeypatch):
    """Six slow approvals add two simulated minutes against a 10 s task budget; only agent time counts."""
    import ghostdev.worker as worker_module
    settings, store, _ = environment
    settings = settings.model_copy(update={"task_timeout": 10})
    real = time.monotonic
    class Clock:
        offset = 0.0
        monotonic = staticmethod(lambda: real()+Clock.offset)
        time = staticmethod(time.time)
    monkeypatch.setattr(worker_module, "time", Clock)
    async def scenario():
        task = store.submit(request(mode="review-first"))
        store.claim()
        running = asyncio.create_task(Worker(settings, store, FakeDesktop(settings), FixtureModel()).run_task(task))
        approved = 0
        while not running.done():
            with store.connect() as db:
                pending = db.execute("SELECT id FROM approvals WHERE task_id=? AND status='pending'", (task["id"],)).fetchone()
            if pending and store.task(task["id"])["state"] == "AWAITING_APPROVAL":
                Clock.offset += 20
                with store.connect() as db:
                    db.execute("UPDATE approvals SET status='approved' WHERE id=?", (pending[0],))
                approved += 1
            await asyncio.sleep(0.05)
        await running
        return task["id"], approved
    task_id, approved = asyncio.run(scenario())
    assert approved == 6 and Clock.offset == 120
    assert store.task(task_id)["state"] == "COMPLETED", store.task(task_id).get("error")


def test_agent_time_still_hits_the_task_deadline(environment, monkeypatch):
    import ghostdev.worker as worker_module
    settings, store, _ = environment
    settings = settings.model_copy(update={"task_timeout": 10})
    real = time.monotonic
    class Clock:
        offset = 0.0
        monotonic = staticmethod(lambda: real()+Clock.offset)
        time = staticmethod(time.time)
    monkeypatch.setattr(worker_module, "time", Clock)
    class Slow(FixtureModel):
        async def propose(self, *args):
            Clock.offset += 4
            return await super().propose(*args)
    task = store.submit(request())
    store.claim()
    asyncio.run(Worker(settings, store, FakeDesktop(settings), Slow()).run_task(task))
    assert store.task(task["id"])["state"] == "FAILED" and "budget" in store.task(task["id"])["error"]


def test_published_contract_matches_runtime_action_model():
    published = json.loads((Path(__file__).resolve().parent.parent/"packages/contracts/action.schema.json").read_text())
    assert published == Action.model_json_schema()
