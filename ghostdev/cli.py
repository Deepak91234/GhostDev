from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .auth import Auth
from .config import Project, Settings, load_settings
from .db import Store
from .desktop import MacDesktop
from .model import LocalModel


def main():
    parser = argparse.ArgumentParser(prog="ghostdev")
    parser.add_argument("--config", default=os.environ.get("GHOSTDEV_CONFIG", "config.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    local = commands.add_parser("use-local-model")
    local.add_argument("--model", default="qwen3:4b")
    local.add_argument("--publisher", default="Qwen")
    commands.add_parser("serve")
    commands.add_parser("worker")
    commands.add_parser("doctor")
    bench = commands.add_parser("bench")
    bench.add_argument("--runs", type=int, default=3, choices=range(1, 21), metavar="1-20")
    commands.add_parser("pair")
    confirm = commands.add_parser("confirm")
    confirm.add_argument("pairing_id")
    commands.add_parser("pending-pairs")
    commands.add_parser("takeover")
    commands.add_parser("revoke")
    args = parser.parse_args()
    try:
        if args.command == "init":
            path = Path(args.config)
            if path.exists():
                raise ValueError("Config already exists; refusing to overwrite it")
            config = Settings(projects=[Project(id="cart", name="Cart demo", root=Path("demo/cart-app"), checks={"cart": ["node", "/verification/cart-check.cjs"]}, required_checks=["cart"])])
            path.write_text(config.model_dump_json(indent=2))
            path.chmod(0o600)
            print("Created config.json for Mac-local Ollama (qwen3:4b, port 11434). Pull the model on this Mac and configure phone HTTPS; see README.md.")
            return
        if args.command == "use-local-model":
            path = Path(args.config)
            if not path.exists():
                raise ValueError("Run ghostdev init first")
            data = json.loads(path.read_text())
            data.update(model_backend="ollama", model_url="http://127.0.0.1:11434", model_name=args.model, model_publisher=args.publisher, model_vision=False, model_think=False, model_context_tokens=8192, hardware_evidence="")
            revised = Settings.model_validate(data)
            revised.ready_model()
            path.write_text(revised.model_dump_json(indent=2))
            path.chmod(0o600)
            print("Updated inference to this Mac's Ollama on port 11434. Projects, pairing state and phone origins are preserved. Pull "+args.model+" locally before starting.")
            return
        settings = load_settings(args.config)
        store = Store(settings.state_dir/"ghostdev.sqlite3")
        auth = Auth(store)
        if args.command == "serve":
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(settings, store), host=settings.host, port=settings.port, proxy_headers=False)
        elif args.command == "worker":
            from .worker import Worker
            asyncio.run(Worker(settings, store).run())
        elif args.command == "pair":
            print(json.dumps(auth.issue(), indent=2))
            print("Enter the secret in the phone. Check `ghostdev pending-pairs`, then confirm ONLY your phone's matching ID.")
        elif args.command == "pending-pairs":
            with store.connect() as db:
                rows = db.execute("SELECT id,expires FROM pairing WHERE claimed=1 AND confirmed=0 AND expires>strftime('%s','now')").fetchall()
            print(json.dumps([dict(row) for row in rows], indent=2))
        elif args.command == "confirm":
            auth.confirm(args.pairing_id)
            print("Confirmed this pairing request locally.")
        elif args.command == "revoke":
            auth.revoke_all()
            print("All phone sessions revoked.")
        elif args.command == "takeover":
            with store.connect() as db:
                ids = [row[0] for row in db.execute("SELECT id FROM tasks WHERE state IN ('RUNNING','VERIFYING','AWAITING_APPROVAL')")]
            for task_id in ids:
                store.transition(task_id, "PAUSED", revoke=True)
                store.event(task_id, "desktop_control_changed", {"owner": "human", "source": "local CLI"})
            print("Agent input revoked. Prior actions are not undone.")
        elif args.command == "bench":
            import httpx
            from .bench import run_bench
            try:
                report = asyncio.run(run_bench(settings, args.runs))
            except httpx.HTTPError as exc:
                raise ValueError("Local model server unreachable: "+str(exc)) from exc
            target = settings.state_dir/"bench"
            target.mkdir(exist_ok=True, mode=0o700)
            path = target/("bench-"+report["started"].replace(":", "").replace("+0000", "Z")+".json")
            path.write_text(json.dumps(report, indent=2))
            path.chmod(0o600)
            failed = [{key: run.get(key) for key in ("run", "reason", "action")} for run in report["runs"] if not run["grounded"]]
            print(json.dumps({"summary": report["summary"], "failed_runs": failed, "runtime": report["runtime"], "report": str(path), "scope": report["evidence_scope"]}, indent=2))
            if report["summary"]["grounded"] < report["summary"]["runs"]:
                sys.exit(3)
        elif args.command == "doctor":
            report = {"desktop": MacDesktop(settings).doctor(), "docker": bool(shutil.which("docker")), "phone_ui_built": (settings.web_dir/"index.html").exists(), "model": {"ready": False}, "hardware_tested_here": False}
            try:
                report["model"] = {"ready": True, **asyncio.run(LocalModel(settings).doctor())}
            except Exception as exc:
                report["model"]["reason"] = str(exc)
            if shutil.which("docker"):
                result = subprocess.run(["docker", "image", "inspect", "ghostdev-runner:local"], capture_output=True, timeout=10)
                report["runner_image_ready"] = result.returncode == 0
            else:
                report["runner_image_ready"] = False
            print(json.dumps(report, indent=2))
            if not report["desktop"]["ready"] or not report["model"]["ready"]:
                sys.exit(2)
    except (ValueError, RuntimeError) as exc:
        print("GhostDev: "+str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
