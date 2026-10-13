from __future__ import annotations

import hashlib
import secrets
import time
import uuid

from .db import Store


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Auth:
    def __init__(self, store: Store):
        self.store = store

    def issue(self) -> dict:
        secret = secrets.token_urlsafe(24)
        pairing_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO pairing(id,secret_hash,expires) VALUES(?,?,?)", (pairing_id, digest(secret), time.time() + 300))
        return {"id": pairing_id, "secret": secret, "expires_in": 300}

    def claim(self, secret: str) -> dict:
        exchange = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM pairing WHERE secret_hash=? AND expires>? AND claimed=0", (digest(secret), time.time())).fetchone()
            if not row:
                raise ValueError("Invalid, expired, or already claimed pairing code")
            db.execute("UPDATE pairing SET claimed=1,exchange_hash=? WHERE id=?", (digest(exchange), row["id"]))
        return {"pairing_id": row["id"], "exchange": exchange, "confirmation_required": True}

    def confirm(self, pairing_id: str):
        with self.store.connect() as db:
            cursor = db.execute("UPDATE pairing SET confirmed=1 WHERE id=? AND claimed=1 AND expires>?", (pairing_id, time.time()))
            if not cursor.rowcount:
                raise ValueError("No unexpired claimed pairing request with that ID")

    def exchange(self, pairing_id: str, exchange: str) -> tuple[str, str] | None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM pairing WHERE id=? AND exchange_hash=? AND expires>?", (pairing_id, digest(exchange), time.time())).fetchone()
            if not row:
                raise ValueError("Invalid pairing exchange")
            if not row["confirmed"]:
                return None
            token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(24)
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), csrf, time.time() + 7 * 86400))
            db.execute("DELETE FROM pairing WHERE id=?", (pairing_id,))
        return token, csrf

    def session(self, token: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires>?", (digest(token), time.time())).fetchone()
        if not row:
            raise ValueError("Pair this phone with the desktop")
        return dict(row)

    def revoke(self, token: str):
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def revoke_all(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions")
