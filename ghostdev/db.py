from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .contracts import LIVE, TRANSITIONS


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(task_id TEXT NOT NULL, seq INTEGER NOT NULL, timestamp REAL NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(task_id,seq));
            CREATE TABLE IF NOT EXISTS pairing(id TEXT PRIMARY KEY, secret_hash TEXT UNIQUE, expires REAL, claimed INTEGER DEFAULT 0, confirmed INTEGER DEFAULT 0, exchange_hash TEXT);
            CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, csrf TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, task_id TEXT, generation INTEGER, digest TEXT, action TEXT, status TEXT DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, task_id TEXT, path TEXT, media_type TEXT, name TEXT);
            """)
        path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def task(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("Task not found")
        return {"id": row["id"], "state": row["state"], "generation": row["generation"], "created": row["created"], **json.loads(row["data"])}

    def _event(self, db, task_id: str, kind: str, payload: dict):
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
        db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (task_id, seq, time.time(), kind, json.dumps(payload)))

    def event(self, task_id: str, kind: str, payload: dict):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._event(db, task_id, kind, payload)

    def events(self, task_id: str, after: int = 0) -> list[dict]:
        self.task(task_id)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 250", (task_id, after)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def submit(self, request: dict) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT id,data FROM tasks WHERE request_id=?", (request["request_id"],)).fetchone()
            if old:
                original = json.loads(old["data"])
                if any(original.get(k) != v for k, v in request.items()):
                    raise ValueError("request_id was already used for a different request")
                task_id = old["id"]
            else:
                slots = tuple(LIVE)
                if db.execute("SELECT 1 FROM tasks WHERE state IN (" + ",".join("?" for _ in slots) + ")", slots).fetchone():
                    raise ValueError("One active task at a time")
                task_id = uuid.uuid4().hex
                db.execute("INSERT INTO tasks(id,request_id,state,created,data) VALUES(?,?,'QUEUED',?,?)", (task_id, request["request_id"], time.time(), json.dumps(request)))
                self._event(db, task_id, "task_state_changed", {"state": "QUEUED"})
        return self.task(task_id)

    def update(self, task_id: str, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("Task not found")
            data = json.loads(row[0])
            data.update(fields)
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(data), task_id))

    def transition(self, task_id: str, state: str, *, revoke=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("Task not found")
            if row[0] == state:
                return
            if state not in TRANSITIONS.get(row[0], set()):
                raise ValueError(f"Cannot transition {row[0]} to {state}")
            db.execute("UPDATE tasks SET state=?,generation=generation+? WHERE id=?", (state, int(revoke), task_id))
            if revoke:
                db.execute("UPDATE approvals SET status='expired' WHERE task_id=? AND status='pending'", (task_id,))
            self._event(db, task_id, "task_state_changed", {"state": state})

    def claim(self) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM tasks WHERE state='QUEUED' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE tasks SET state='RUNNING' WHERE id=?", (row[0],))
            self._event(db, row[0], "task_state_changed", {"state": "RUNNING"})
        return self.task(row[0])

    def interrupted(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id FROM tasks WHERE state IN ('RUNNING','VERIFYING','AWAITING_APPROVAL','PAUSED','APPLYING')").fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET state='INTERRUPTED',generation=generation+1 WHERE id=?", (row[0],))
                db.execute("UPDATE approvals SET status='expired' WHERE task_id=?", (row[0],))
                self._event(db, row[0], "task_state_changed", {"state": "INTERRUPTED", "reason": "Worker restarted; input is never replayed"})

    def lease(self, task_id: str, generation: int):
        task = self.task(task_id)
        if task["state"] not in {"RUNNING", "VERIFYING"} or task["generation"] != generation:
            raise LeaseRevoked("Desktop execution lease revoked")

    def artifact(self, task_id: str, path: Path, media_type: str, name: str) -> str:
        artifact_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)", (artifact_id, task_id, str(path), media_type, name))
        return artifact_id


class LeaseRevoked(RuntimeError):
    pass
