import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ghostdev.api import create_app
from ghostdev.auth import Auth
from ghostdev.config import Project, Settings
from ghostdev.db import Store


@pytest.fixture
def environment(tmp_path):
    root = tmp_path / "original"
    root.mkdir()
    (root/"GhostDev Report.txt").write_text("GhostDev report\n")
    (root/"cart.js").write_text("function calculateTotal(price, quantity) { return price; }\nmodule.exports = {calculateTotal};\n")
    (root/"index.html").write_text("<title>GhostDev Cart</title>")
    project = Project(id="cart", name="Cart", root=root, checks={"cart": ["node", "/verification/cart-check.cjs"]}, required_checks=["cart"])
    settings = Settings(state_dir=tmp_path/"state", projects=[project], model_name="local-test-fixture", secure_cookies=False, web_dir=tmp_path/"web")
    store = Store(settings.state_dir/"ghostdev.sqlite3")
    return settings, store, project


@pytest.fixture
def client(environment):
    settings, store, _ = environment
    with TestClient(create_app(settings, store), base_url="http://localhost:8765") as client:
        client.headers["origin"] = "http://localhost:8765"
        yield client


def pair(client, store):
    auth = Auth(store)
    secret = auth.issue()
    claimed = client.post("/pairing/claim", json={"secret": secret["secret"]}).json()
    auth.confirm(claimed["pairing_id"])
    result = client.post("/pairing/exchange", json={"pairing_id": claimed["pairing_id"], "exchange": claimed["exchange"]}).json()
    client.headers["x-csrf-token"] = result["csrf"]
    return result


def request(**overrides):
    return {"request_id": "request_123", "project_id": "cart", "instruction": "Open and edit the report", "task_type": "desktop", "expected_text": "Local AI works", "mode": "bounded-task", **overrides}
