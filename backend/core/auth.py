"""
Authentication and session storage.

WHY THIS EXISTS
---------------
Before this, `role` arrived in the request body: `{"role": "cxo"}` was
self-asserted, so the RBAC layer — which is genuinely enforced in SQL — was
protecting against a caller who had already told it who they were. That is a
lock on a door with the key taped to it.

Now the role and its scope claims come from the stored user record, keyed by a
server-issued session token. The request body can no longer influence them.

Storage is a separate SQLite file: the DuckDB warehouse is opened READ-ONLY by
design and must stay that way, so user state cannot live there.

Password hashing is PBKDF2-HMAC-SHA256 from the standard library rather than
bcrypt/argon2 — one less dependency in the runtime image, and at 600k iterations
it is within current OWASP guidance. A production system would use argon2id.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from backend import config

PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
SESSION_TTL_S = int(os.getenv("SESSION_TTL_S", str(12 * 3600)))
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_S = 300

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
# Per-username failure counters. In-process on purpose: a real deployment puts
# this in Redis or at the edge, and pretending otherwise would be worse.
_failures: dict[str, list[float]] = {}


class AuthError(Exception):
    """Login refused. The message is deliberately vague to the caller."""


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    role: str
    claims: dict[str, str]


def _db() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            path = Path(config.AUTH_DB_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(path), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                  username      TEXT PRIMARY KEY,
                  display_name  TEXT NOT NULL,
                  role          TEXT NOT NULL,
                  claims_json   TEXT NOT NULL DEFAULT '{}',
                  pw_salt       BLOB NOT NULL,
                  pw_hash       BLOB NOT NULL,
                  created_at    INTEGER NOT NULL,
                  last_login_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS sessions (
                  token_hash  BLOB PRIMARY KEY,
                  username    TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                  created_at  INTEGER NOT NULL,
                  expires_at  INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
            """)
            _conn.commit()
        return _conn


def _hash_pw(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               PBKDF2_ITERATIONS)


def _token_hash(token: str) -> bytes:
    """Sessions are stored hashed, so a leaked DB file is not a set of live tokens."""
    return hashlib.sha256(token.encode("utf-8")).digest()


# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────
def create_user(username: str, password: str, *, role: str, display_name: str,
                claims: dict | None = None, replace: bool = False) -> User:
    import json
    if len(password) < 8:
        raise AuthError("password must be at least 8 characters")
    salt = secrets.token_bytes(SALT_BYTES)
    db = _db()
    db.execute(
        f"INSERT {'OR REPLACE ' if replace else ''}INTO users "
        "(username, display_name, role, claims_json, pw_salt, pw_hash, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (username, display_name, role, json.dumps(claims or {}), salt,
         _hash_pw(password, salt), int(time.time())))
    db.commit()
    return User(username, display_name, role, claims or {})


def list_users() -> list[User]:
    import json
    return [User(r["username"], r["display_name"], r["role"],
                 json.loads(r["claims_json"]))
            for r in _db().execute(
                "SELECT username, display_name, role, claims_json FROM users "
                "ORDER BY username")]


def _rate_limited(username: str) -> bool:
    now = time.time()
    hits = [t for t in _failures.get(username, []) if now - t < LOCKOUT_WINDOW_S]
    _failures[username] = hits
    return len(hits) >= MAX_FAILED_ATTEMPTS


def _record_failure(username: str) -> None:
    _failures.setdefault(username, []).append(time.time())


# ─────────────────────────────────────────────────────────────────────────────
# Login / sessions
# ─────────────────────────────────────────────────────────────────────────────
def login(username: str, password: str) -> tuple[str, User, int]:
    """Returns (token, user, expires_at). Raises AuthError on any failure."""
    import json
    username = (username or "").strip().lower()
    if _rate_limited(username):
        raise AuthError("too many failed attempts — try again in a few minutes")

    row = _db().execute(
        "SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    # Hash even when the user does not exist, so response time does not reveal
    # which usernames are real.
    salt = row["pw_salt"] if row else b"\x00" * SALT_BYTES
    expected = row["pw_hash"] if row else b"\x00" * 32
    candidate = _hash_pw(password or "", salt)

    if not row or not hmac.compare_digest(candidate, expected):
        _record_failure(username)
        raise AuthError("invalid username or password")

    _failures.pop(username, None)
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + SESSION_TTL_S
    db = _db()
    db.execute("INSERT INTO sessions (token_hash, username, created_at, expires_at) "
               "VALUES (?,?,?,?)", (_token_hash(token), username, now, expires))
    db.execute("UPDATE users SET last_login_at = ? WHERE username = ?", (now, username))
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db.commit()
    return token, User(row["username"], row["display_name"], row["role"],
                       json.loads(row["claims_json"])), expires


def resolve_session(token: str | None) -> User | None:
    import json
    if not token:
        return None
    row = _db().execute("""
        SELECT u.username, u.display_name, u.role, u.claims_json, s.expires_at
        FROM sessions s JOIN users u ON u.username = s.username
        WHERE s.token_hash = ?""", (_token_hash(token),)).fetchone()
    if not row or row["expires_at"] < int(time.time()):
        return None
    return User(row["username"], row["display_name"], row["role"],
                json.loads(row["claims_json"]))


def logout(token: str | None) -> bool:
    if not token:
        return False
    db = _db()
    cur = db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    db.commit()
    return cur.rowcount > 0


# ─────────────────────────────────────────────────────────────────────────────
# Demo seeding
# ─────────────────────────────────────────────────────────────────────────────
DEMO_USERS = [
    ("priya",  "Priya Nair",    "cxo",              {},                        "CXO — full access"),
    ("rahul",  "Rahul Menon",   "regional_manager", {"region": "South"},       "South region only"),
    ("asha",   "Asha Pillai",   "analyst",          {},                        "All rows, terminal_id masked"),
    ("m1042",  "Kanaka Stores", "merchant_user",    {"merchant_id": "M-1042"}, "One merchant only"),
]


def seed_demo_users(password: str | None = None) -> list[dict]:
    """
    Idempotent. The demo password is visible in the UI on purpose — this is an
    assignment build, and a hidden credential nobody can use is not a demo. A real
    deployment seeds nothing and provisions through an identity provider.
    """
    pw = password or config.DEMO_PASSWORD
    out = []
    for username, display, role, claims, note in DEMO_USERS:
        create_user(username, pw, role=role, display_name=display, claims=claims,
                    replace=True)
        out.append({"username": username, "display_name": display, "role": role,
                    "claims": claims, "note": note})
    return out
