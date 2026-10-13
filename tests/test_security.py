import json
import time

import pytest
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from ghostdev.auth import Auth
from ghostdev.config import Settings
from ghostdev.contracts import Action
from ghostdev.db import LeaseRevoked
from ghostdev.desktop import MacDesktop, input_point
from ghostdev.workspace import Workspace, safe_path
from conftest import pair, request


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://integrate.api.nvidia.com/v1", "http://8.8.8.8", "http://169.254.169.254", "http://example.com", "http://user:password@127.0.0.1", "http://127.0.0.1?secret=x", "http://100.80.1.2:11434", "http://192.168.1.9:8000/v1", "http://10.0.0.2:11434"])
def test_public_and_ambiguous_model_endpoints_fail_closed(url):
    with pytest.raises(ValidationError):
        Settings(model_url=url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://127.0.0.1:8000/v1", "http://[::1]:11434"])
def test_local_model_addresses(url):
    assert Settings(model_url=url).model_url == url


def test_mac_local_defaults_do_not_require_nvidia():
    config = Settings()
    config.ready_model()
    assert config.model_name == 'qwen3:4b' and config.model_url == 'http://127.0.0.1:11434'
    assert config.model_publisher == 'Qwen' and config.model_think is False


def test_all_sensitive_routes_require_session(client):
    for route in ["/projects", "/settings", "/tasks", "/tasks/unknown", "/tasks/unknown/events", "/artifacts/unknown", "/tasks/unknown/screen"]:
        assert client.get(route).status_code == 401
    assert client.post("/tasks", json=request()).status_code == 401


def test_pairing_requires_local_confirmation_and_is_one_time(client, environment):
    _, store, _ = environment
    issued = Auth(store).issue()
    claimed = client.post("/pairing/claim", json={"secret": issued["secret"]}).json()
    exchange = {"pairing_id": claimed["pairing_id"], "exchange": claimed["exchange"]}
    assert client.post("/pairing/exchange", json=exchange).json()["pending"]
    assert client.get("/session").status_code == 401
    assert client.post("/pairing/claim", json={"secret": issued["secret"]}).status_code == 400
    Auth(store).confirm(claimed["pairing_id"])
    result = client.post("/pairing/exchange", json=exchange)
    assert result.status_code == 200 and not result.json()["pending"]
    assert "HttpOnly" in result.headers["set-cookie"] and "SameSite=strict" in result.headers["set-cookie"]
    assert client.post("/pairing/exchange", json=exchange).status_code == 400


def test_csrf_and_origin(client, environment):
    pair(client, environment[1])
    client.headers["x-csrf-token"] = "wrong"
    assert client.post("/tasks", json=request()).status_code == 403
    client.headers["origin"] = "https://attacker.invalid"
    assert client.post("/pairing/claim", json={"secret":"x"*30}).status_code == 403


def test_revoked_session(client, environment):
    pair(client, environment[1])
    assert client.post("/session/revoke", json={}).status_code == 200
    assert client.get("/projects").status_code == 401


def test_websocket_denies_unpaired_or_wrong_origin(client, environment):
    task = environment[1].submit(request())
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/tasks/{task['id']}/stream", headers={"origin":"http://localhost:8765"}):
            pass
    pair(client, environment[1])
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/tasks/{task['id']}/stream", headers={"origin":"https://attacker.invalid"}):
            pass


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", ".env", ".git/config", "folder/../../secret.txt", "private.key", "foo\\bar", "verification/cart-check.cjs"])
def test_paths_are_bounded(environment, path):
    with pytest.raises(ValueError):
        safe_path(environment[2].root, path)


def test_symlinks_are_rejected(environment, tmp_path):
    outside = tmp_path/"outside.txt"
    outside.write_text("secret")
    (environment[2].root/"link.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        safe_path(environment[2].root, "link.txt")


def test_arbitrary_model_code_is_rejected():
    base = {"kind":"click", "observation_id":"id", "expected_app":"editor", "summary":"Click", "postcondition":"Focus field"}
    with pytest.raises(ValidationError):
        Action.model_validate({**base, "python":"import os; os.system('anything')"})
    with pytest.raises(ValidationError):
        Action.model_validate({**base, "kind":"shell"})
    with pytest.raises(ValidationError):
        Action.model_validate({**base, "x":"10"})


def action(**fields):
    return Action(kind="click", observation_id="current", expected_app="editor", summary="Click field", postcondition="Field focused", **fields)


def observation():
    return {"id":"current", "app":"editor", "timestamp":time.time(), "capture_allowed":True,"image_width":640,"image_height":400,"focus_role":"AXTextArea"}


def test_stale_focus_and_coordinate_denials(environment):
    desktop = MacDesktop(environment[0])
    current = observation()
    for change, proposed in [({"id":"changed"}, action()), ({"app":"browser"}, action()), ({"timestamp":0}, action()), ({}, action(x=641)), ({"capture_allowed":False}, action())]:
        with pytest.raises(ValueError):
            desktop.validate(proposed, {**current, **change})


def test_no_typing_into_browser_password_or_terminal(environment):
    desktop = MacDesktop(environment[0])
    typing = Action(kind="type_text", observation_id="current", expected_app="editor", summary="Type", postcondition="Note", text="hello")
    for role in ["AXSecureTextField", "AXButton", "AXUnknown"]:
        with pytest.raises(ValueError):
            desktop.validate(typing, {**observation(), "focus_role":role})
    typing.expected_app = "browser"
    with pytest.raises(ValueError):
        desktop.validate(typing, {**observation(), "app":"browser"})


def test_task_idempotency_cancel_lease_and_event_order(client, environment):
    store = environment[1]
    pair(client, store)
    one = client.post("/tasks", json=request()).json()
    two = client.post("/tasks", json=request()).json()
    assert one["id"] == two["id"]
    assert client.post("/tasks", json=request(instruction="Different")).status_code == 409
    store.claim()
    before = store.task(one["id"])["generation"]
    assert client.post(f"/tasks/{one['id']}/cancel", json={}).json()["state"] == "CANCELLED"
    assert client.post(f"/tasks/{one['id']}/cancel", json={}).status_code == 200
    with pytest.raises(LeaseRevoked):
        store.lease(one["id"], before)
    events = client.get(f"/tasks/{one['id']}/events?after_seq=1").json()
    assert [e["seq"] for e in events] == [2,3]


def test_exact_approval_binding_and_takeover(client, environment):
    store = environment[1]
    pair(client, store)
    task = store.submit(request())
    store.claim()
    proposed = action()
    store.update(task["id"], observation=observation())
    store.transition(task["id"], "AWAITING_APPROVAL")
    with store.connect() as db:
        db.execute("INSERT INTO approvals(id,task_id,generation,digest,action) VALUES('approval',?,0,?,?)", (task["id"], proposed.digest(), proposed.model_dump_json()))
    route = f"/tasks/{task['id']}/approvals/approval/resolve"
    assert client.post(route, json={"digest":"wrong", "approve":True}).status_code == 409
    assert client.post('/desktop/takeover',json={}).status_code == 200
    assert client.post(route,json={"digest":proposed.digest(),"approve":True}).status_code == 409
    assert store.task(task["id"])["state"] == "PAUSED"


def test_patch_apply_conflict_and_hash(environment):
    settings, _, project = environment
    ws = Workspace(settings.state_dir/"tasks"/"abc", project)
    ws.prepare(True)
    ws.patch("cart.js", "return price;", "return price * quantity;")
    with pytest.raises(ValueError, match="hash"):
        ws.apply("wrong")
    (project.root/"cart.js").write_text("Concurrent edit\n")
    with pytest.raises(ValueError, match="changed"):
        ws.apply(ws.diff()["sha256"])
    assert (project.root/"cart.js").read_text() == "Concurrent edit\n"


def test_exact_reviewed_patch_applies_and_isolated_copy(environment):
    settings, _, project = environment
    original = (project.root/"cart.js").read_text()
    ws = Workspace(settings.state_dir/"tasks"/"abc", project)
    ws.prepare(True)
    ws.patch("cart.js", "return price;", "return price * quantity;")
    assert (project.root/"cart.js").read_text() == original
    diff = ws.diff()
    ws.apply(diff["sha256"])
    assert "price * quantity" in (project.root/"cart.js").read_text()


def test_worker_restart_interrupts_without_replaying(environment):
    store = environment[1]
    task = store.submit(request())
    store.claim()
    store.interrupted()
    assert store.task(task["id"])["state"] == "INTERRUPTED"
    assert store.claim() is None


def test_retina_and_resized_window_coordinates_are_display_relative():
    original={'bounds':{'X':100,'Y':50,'Width':800,'Height':600},'image_width':1600,'image_height':1200}
    resized={**original,'image_width':400,'image_height':300}
    assert input_point(original,800,600)==(500,350)
    assert input_point(resized,200,150)==(500,350)
    assert input_point(resized,0,0)==(100,50)
