"""Shared lifecycle, validation and durable SQLite state for targeted GWS watches.

The three services share one profile-local database and one lifecycle.  Service
modules own their selectors and Google delta readers; nothing here talks to
Google or Hermes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SERVICES = ("gmail", "drive", "calendar")
MODES = ("same_session", "new_session")
MAX_MINUTES = 1440
MAX_BATCH_SECONDS = 60
MAX_INSTRUCTIONS = 2000
MAX_IDS = 50
MAX_SKILLS = 20
MAX_BACKLOG = 1000
ACTIVE_STATES = ("active", "matched")

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:+-]{0,1023}\Z")
_EMAIL = re.compile(r"[^@\s<>]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}\Z")
_DOMAIN = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\Z"
)
_ROUTE_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


class WatchError(RuntimeError):
    """A safe, stable-code validation or state error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self):
        return {"ok": False, "error_code": self.code, "error": self.message}


def now_iso(ts=None):
    return datetime.fromtimestamp(float(ts if ts is not None else time.time()),
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_id(value, label="id"):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise WatchError("invalid_arguments",
                         f"{label} must be a non-empty, unpadded exact ID (letters, digits, _ . @ : + -; "
                         "no whitespace or control characters)")
    return value


def clean_text(value, label, *, maximum=500, allow_empty=False):
    if not isinstance(value, str):
        raise WatchError("invalid_arguments", f"{label} must be a string")
    text = value.strip()
    if (not text and not allow_empty) or text != value or len(text) > maximum:
        raise WatchError("invalid_arguments",
                         f"{label} must be unpadded, non-empty and at most {maximum} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise WatchError("invalid_arguments", f"{label} cannot contain control characters")
    return text


def clean_address(value, label="address"):
    if not isinstance(value, str) or value != value.strip() or len(value) > 320:
        raise WatchError("invalid_arguments", f"{label} must be an exact email address")
    if "<" in value or ">" in value or not _EMAIL.fullmatch(value):
        raise WatchError("invalid_arguments",
                         f"{label} must be a bare addr-spec, never a display name; dot/plus aliases are not rewritten")
    return value.lower()


def clean_domain(value, label="domain"):
    if not isinstance(value, str) or value != value.strip().lower() or len(value) > 253:
        raise WatchError("invalid_arguments", f"{label} must be an exact lowercase domain")
    if not _DOMAIN.fullmatch(value):
        raise WatchError("invalid_arguments",
                         f"{label} must be one validated domain (subdomains are not implied)")
    return value


def string_list(value, label, *, cleaner=clean_id, maximum=MAX_IDS):
    if not isinstance(value, list) or not value:
        raise WatchError("invalid_arguments", f"{label} must be a non-empty list")
    if len(value) > maximum:
        raise WatchError("invalid_arguments", f"{label} accepts at most {maximum} entries")
    result = []
    for item in value:
        cleaned = cleaner(item, label) if cleaner is not clean_id else clean_id(item, label)
        if cleaned not in result:
            result.append(cleaned)
    return result


def literal_contains(haystack, needles):
    if not isinstance(haystack, str):
        return False
    folded = haystack.casefold()
    return any(needle.casefold() in folded for needle in needles)


def parse_rfc3339(value, label):
    if not isinstance(value, str) or value != value.strip():
        raise WatchError("invalid_arguments", f"{label} must be an RFC3339 timestamp with an offset")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WatchError("invalid_arguments", f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise WatchError("invalid_arguments", f"{label} must include a timezone offset")
    return parsed


def _clean_change_types(value, allowed, label="change_types"):
    types = string_list(value, label, cleaner=lambda item, name: clean_text(item, name, maximum=32))
    unknown = [item for item in types if item not in allowed]
    if unknown:
        raise WatchError("invalid_arguments",
                         f"{label} accepts only: {', '.join(sorted(allowed))}")
    return types


def _clean_skills(value):
    if value is None:
        return []
    skills = string_list(value, "skills", cleaner=lambda item, name: clean_text(
        item, name, maximum=128), maximum=MAX_SKILLS)
    for skill in skills:
        if "/" in skill or "\\" in skill or skill.startswith("."):
            raise WatchError("invalid_arguments", "skills must be installed skill names")
    return skills


def validate_common(args, *, mode_required=True):
    """Shared start fields; per-service target/filters are validated elsewhere."""
    if not isinstance(args, dict):
        raise WatchError("invalid_arguments", "arguments must be an object")
    if "filters" in args and not isinstance(args["filters"], dict):
        raise WatchError("invalid_arguments", "filters must be an object")
    account = clean_address(args.get("account"), "account")
    mode = args.get("delivery_mode")
    if mode_required and mode not in MODES:
        raise WatchError("invalid_arguments", "delivery_mode must be same_session or new_session")
    minutes = args.get("minutes", 60)
    if type(minutes) is not int or not 1 <= minutes <= MAX_MINUTES:
        raise WatchError("invalid_arguments", f"minutes must be an integer between 1 and {MAX_MINUTES}")
    batch = args.get("batch_seconds", 10)
    if type(batch) is not int or not 0 <= batch <= MAX_BATCH_SECONDS:
        raise WatchError("invalid_arguments", f"batch_seconds must be an integer between 0 and {MAX_BATCH_SECONDS}")
    stop_after = args.get("stop_after_match", False)
    if type(stop_after) is not bool:
        raise WatchError("invalid_arguments", "stop_after_match must be a boolean")
    instructions = args.get("instructions", "Summarize the relevant change.")
    instructions = clean_text(instructions, "instructions", maximum=MAX_INSTRUCTIONS)
    skills = _clean_skills(args.get("skills"))
    if skills and mode == "same_session":
        raise WatchError("invalid_arguments",
                         "skills is only valid with delivery_mode=new_session; same-session keeps the "
                         "existing conversation's prompt and tools")
    return {"account": account, "mode": mode, "minutes": minutes, "batch_seconds": batch,
            "stop_after_match": stop_after, "instructions": instructions, "skills": skills}


def origin_from_session(*, env, session_key, session_id, profile):
    """Trusted arming origin only: never model-supplied destination fields."""
    platform = str(env("HERMES_SESSION_PLATFORM") or env("HERMES_SESSION_SOURCE") or "").strip()
    chat_id = str(env("HERMES_SESSION_CHAT_ID") or "").strip()
    if not platform or not chat_id or platform == "webhook":
        raise WatchError("no_trusted_origin",
                         "This watch needs a trusted gateway conversation origin; stateless sessions "
                         "cannot be given a delivery destination")
    if not isinstance(session_key, str) or not session_key.strip():
        raise WatchError("no_trusted_origin", "No trusted session key is bound to this call")
    if not isinstance(session_id, str) or not session_id.strip():
        raise WatchError("no_trusted_session", "No current physical session ID is bound to this call")
    principal = str(env("HERMES_SESSION_ACTOR_ID") or env("HERMES_SESSION_USER_ID") or "").strip()
    if not principal:
        raise WatchError("no_principal", "This conversation has no trusted sender identity")
    chat_type = str(env("HERMES_SESSION_CHAT_TYPE") or "").strip()
    return {
        "profile": str(profile or env("HERMES_SESSION_PROFILE") or "default"),
        "platform": platform,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "thread_id": str(env("HERMES_SESSION_THREAD_ID") or "").strip() or None,
        "scope_id": str(env("HERMES_SESSION_SCOPE_ID") or "").strip() or None,
        "parent_chat_id": str(env("HERMES_SESSION_PARENT_CHAT_ID") or "").strip() or None,
        "user_id": str(env("HERMES_SESSION_USER_ID") or "").strip() or None,
        "principal": principal,
        "session_key": session_key.strip(),
        "session_id": session_id.strip(),
    }


def fingerprint(service, account, origin, spec):
    material = {
        "service": service, "account": account, "profile": origin["profile"],
        "principal": origin["principal"], "session_key": origin["session_key"],
        "mode": spec["mode"], "target": spec["target"], "filters": spec["filters"],
        "instructions": spec["instructions"], "skills": spec["skills"],
        "stop_after_match": spec["stop_after_match"],
        "minutes": spec["minutes"], "batch_seconds": spec["batch_seconds"],
        "session_id": origin["session_id"] if spec["mode"] == "same_session" else None,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    watch_id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    account TEXT NOT NULL,
    profile TEXT NOT NULL,
    principal TEXT NOT NULL,
    origin_json TEXT NOT NULL,
    mode TEXT NOT NULL,
    target_json TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    instructions TEXT NOT NULL,
    skills_json TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    batch_seconds INTEGER NOT NULL,
    stop_after_match INTEGER NOT NULL,
    state TEXT NOT NULL,
    collection TEXT NOT NULL,
    generation INTEGER NOT NULL,
    created_at REAL NOT NULL,
    not_before REAL NOT NULL,
    expires_at REAL NOT NULL,
    route_name TEXT,
    bridge_name TEXT,
    baseline_json TEXT NOT NULL DEFAULT '{}',
    cleanup_state TEXT NOT NULL DEFAULT 'none',
    cleanup_reason TEXT,
    delivery_state TEXT NOT NULL DEFAULT 'none',
    last_error TEXT,
    gap TEXT,
    fingerprint TEXT NOT NULL,
    UNIQUE(watch_id)
);
CREATE INDEX IF NOT EXISTS watches_active ON watches(service, profile, principal, state);
CREATE INDEX IF NOT EXISTS watches_fingerprint ON watches(fingerprint, state);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    account TEXT NOT NULL,
    resource_json TEXT NOT NULL,
    query_json TEXT NOT NULL,
    cursor TEXT,
    cursor_staged TEXT,
    channel_id TEXT,
    channel_token TEXT,
    resource_id TEXT,
    expires_at REAL,
    state TEXT NOT NULL DEFAULT 'active',
    last_error TEXT,
    last_reconcile REAL,
    seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS watch_sources (
    watch_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    PRIMARY KEY (watch_id, source_id)
);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY,
    watch_id TEXT NOT NULL,
    service TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    request_id TEXT,
    detail TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, next_attempt);
CREATE TABLE IF NOT EXISTS hints (
    hint_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS hints_pending ON hints(state, source_id);
CREATE TABLE IF NOT EXISTS remotes (
    remote_id TEXT PRIMARY KEY,
    watch_id TEXT,
    source_id TEXT,
    kind TEXT NOT NULL,
    identity_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending',
    expires_at REAL,
    detail TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS delivered (
    source_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (source_id, event_key)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_WATCH_FIELDS = {
    "state", "collection", "baseline_json", "cleanup_state", "cleanup_reason",
    "delivery_state", "last_error", "gap", "route_name", "bridge_name",
}


def _backlog_full(conn, watch_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE watch_id=? AND state IN ('pending','retry','forwarding')",
        (watch_id,)).fetchone()
    return int(row["n"]) >= MAX_BACKLOG


class WatchStore:
    """One private SQLite database under the plugin's profile-local data dir."""

    def __init__(self, profile_home, *, clock=time.time):
        self.home = Path(profile_home).expanduser().resolve()
        self.path = self.home / "plugin-data" / "gws-oauth" / "watch.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        self.clock = clock
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn

    def _init(self):
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        finally:
            conn.close()

    @contextmanager
    def tx(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # --- watches ---------------------------------------------------------
    def add_watch(self, record, sources=()):
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO watches (watch_id, service, account, profile, principal, origin_json, mode, "
                "target_json, filters_json, instructions, skills_json, minutes, batch_seconds, "
                "stop_after_match, state, collection, generation, created_at, not_before, expires_at, "
                "route_name, bridge_name, baseline_json, cleanup_state, delivery_state, fingerprint) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["watch_id"], record["service"], record["account"], record["profile"],
                 record["principal"], json.dumps(record["origin"], sort_keys=True), record["mode"],
                 json.dumps(record["target"], sort_keys=True), json.dumps(record["filters"], sort_keys=True),
                 record["instructions"], json.dumps(record["skills"]), record["minutes"],
                 record["batch_seconds"], int(record["stop_after_match"]), record.get("state", "active"),
                 record.get("collection", "collecting"), record.get("generation", 1),
                 record["created_at"], record["not_before"], record["expires_at"],
                 record.get("route_name"), record.get("bridge_name"),
                 json.dumps(record.get("baseline", {}), sort_keys=True),
                 record.get("cleanup_state", "none"), record.get("delivery_state", "none"),
                 record["fingerprint"]))
            for source in sources:
                source_id = self.source_key(record["service"], record["account"],
                                            source["resource"], source.get("query", {}))
                conn.execute(
                    "INSERT OR IGNORE INTO sources (source_id, service, account, resource_json, "
                    "query_json, cursor, state, last_reconcile) VALUES (?,?,?,?,?,?,?,?)",
                    (source_id, record["service"], record["account"],
                     json.dumps(source["resource"], sort_keys=True),
                     json.dumps(source.get("query", {}), sort_keys=True), source.get("cursor"),
                     source.get("state", "active"), record["created_at"]))
                consumers = conn.execute(
                    "SELECT 1 FROM watch_sources x JOIN watches w ON w.watch_id=x.watch_id "
                    "WHERE x.source_id=? AND w.state IN ('active','matched') LIMIT 1",
                    (source_id,)).fetchone()
                if consumers is None:
                    conn.execute(
                        "UPDATE sources SET cursor=?, cursor_staged=NULL, state='active', "
                        "channel_id=NULL, channel_token=NULL, resource_id=NULL, expires_at=NULL, "
                        "last_error=NULL WHERE source_id=?", (source.get("cursor"), source_id))
                    # A fresh explicit start retries registration immediately.
                    conn.execute("DELETE FROM meta WHERE key IN (?,?)",
                                 ("backoff:sub:" + source_id, "backoff:stop:" + source_id))
                conn.execute("INSERT INTO watch_sources (watch_id, source_id) VALUES (?,?)",
                             (record["watch_id"], source_id))
        return self.get_watch(record["watch_id"])

    def get_watch(self, watch_id):
        with self.read() as conn:
            row = conn.execute("SELECT * FROM watches WHERE watch_id=?", (watch_id,)).fetchone()
        return dict(row) if row is not None else None

    def find_active(self, fingerprint, now=None):
        now = self.clock() if now is None else now
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM watches WHERE fingerprint=? AND state IN ('active','matched') "
                "AND expires_at>? ORDER BY created_at DESC LIMIT 1", (fingerprint, now)).fetchone()
        return dict(row) if row is not None else None

    def list_watches(self, *, service=None, profile=None, principal=None, active_only=False):
        clauses, values = [], []
        for column, value in (("service", service), ("profile", profile), ("principal", principal)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        if active_only:
            clauses.append("state IN ('active','matched')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.read() as conn:
            rows = conn.execute(f"SELECT * FROM watches{where} ORDER BY created_at DESC, watch_id", values).fetchall()
        return [dict(row) for row in rows]

    def update_watch(self, watch_id, **fields):
        unknown = set(fields) - _WATCH_FIELDS
        if unknown:
            raise ValueError(f"unknown watch fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.tx() as conn:
            conn.execute(f"UPDATE watches SET {assignments} WHERE watch_id=?",
                         (*fields.values(), watch_id))

    def expire_due(self, now=None):
        now = self.clock() if now is None else now
        with self.tx() as conn:
            rows = conn.execute(
                "SELECT watch_id FROM watches WHERE state='active' AND expires_at<=?",
                (now,)).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE watches SET state='expired', collection='stopped', cleanup_state='pending', "
                    "cleanup_reason='lifetime expired', delivery_state=CASE WHEN delivery_state='none' "
                    "THEN 'expired' ELSE delivery_state END WHERE watch_id=?", (row["watch_id"],))
                conn.execute(
                    "UPDATE outbox SET state='cancelled', updated_at=? WHERE watch_id=? "
                    "AND state IN ('pending','retry')", (now, row["watch_id"]))
        return [row["watch_id"] for row in rows]

    def stop(self, watch_id):
        now = self.clock()
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM watches WHERE watch_id=?", (watch_id,)).fetchone()
            if row is None:
                return None
            if row["state"] != "stopped":
                conn.execute(
                    "UPDATE watches SET state='stopped', collection='stopped', cleanup_state='pending', "
                    "cleanup_reason='stopped by owner' WHERE watch_id=?", (watch_id,))
                conn.execute(
                    "UPDATE outbox SET state='cancelled', updated_at=? WHERE watch_id=? "
                    "AND state IN ('pending','retry')", (now, watch_id))
        return self.get_watch(watch_id)

    def set_matched(self, watch_id):
        self.update_watch(watch_id, state="matched", collection="reserved")

    def clear_gap(self, watch_id):
        self.update_watch(watch_id, gap=None, last_error=None)

    # --- sources ---------------------------------------------------------
    @staticmethod
    def source_key(service, account, resource, query):
        material = json.dumps({"service": service, "account": account, "resource": resource,
                               "query": query}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode()).hexdigest()[:32]

    def ensure_source(self, service, account, resource, query, *, cursor=None, state="active"):
        source_id = self.source_key(service, account, resource, query)
        now = self.clock()
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sources (source_id, service, account, resource_json, query_json, cursor, "
                    "state, last_reconcile) VALUES (?,?,?,?,?,?,?,?)",
                    (source_id, service, account, json.dumps(resource, sort_keys=True),
                     json.dumps(query, sort_keys=True), cursor, state, now))
            elif cursor is not None and not row["cursor"] and not row["cursor_staged"]:
                conn.execute("UPDATE sources SET cursor=? WHERE source_id=?", (cursor, source_id))
        return self.get_source(source_id)

    def get_source(self, source_id):
        with self.read() as conn:
            row = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_sources(self, *, service=None, account=None, active_only=True):
        clauses, values = [], []
        if service is not None:
            clauses.append("service=?")
            values.append(service)
        if account is not None:
            clauses.append("account=?")
            values.append(account)
        if active_only:
            clauses.append("state='active'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.read() as conn:
            rows = conn.execute(f"SELECT * FROM sources{where} ORDER BY service, account", values).fetchall()
        return [dict(row) for row in rows]

    def update_source(self, source_id, **fields):
        allowed = {"cursor", "cursor_staged", "channel_id", "channel_token", "resource_id",
                   "expires_at", "state", "last_error", "last_reconcile"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown source fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.tx() as conn:
            conn.execute(f"UPDATE sources SET {assignments} WHERE source_id=?",
                         (*fields.values(), source_id))

    def link_source(self, watch_id, source_id):
        with self.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO watch_sources (watch_id, source_id) VALUES (?,?)",
                         (watch_id, source_id))

    def sources_for(self, watch_id):
        with self.read() as conn:
            rows = conn.execute(
                "SELECT s.* FROM sources s JOIN watch_sources w ON w.source_id=s.source_id "
                "WHERE w.watch_id=? ORDER BY s.service, s.source_id", (watch_id,)).fetchall()
        return [dict(row) for row in rows]

    def consumers(self, source_id):
        with self.read() as conn:
            rows = conn.execute(
                "SELECT w.* FROM watches w JOIN watch_sources x ON x.watch_id=w.watch_id "
                "WHERE x.source_id=? AND w.state IN ('active','matched') ORDER BY w.created_at",
                (source_id,)).fetchall()
        return [dict(row) for row in rows]

    def all_consumer_count(self, source_id):
        with self.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM watches w JOIN watch_sources x ON x.watch_id=w.watch_id "
                "WHERE x.source_id=? AND w.state IN ('active','matched')", (source_id,)).fetchone()
        return int(row["n"])

    def active_consumers_excluding(self, source_id, watch_id):
        with self.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM watches w JOIN watch_sources x ON x.watch_id=w.watch_id "
                "WHERE x.source_id=? AND w.watch_id!=? AND w.state='active'",
                (source_id, watch_id)).fetchone()
        return int(row["n"])

    def bump_source(self, source_id, **fields):
        with self.tx() as conn:
            assignments = ", ".join(f"{key}=?" for key in fields)
            conn.execute(f"UPDATE sources SET {assignments}, seq=seq+1 WHERE source_id=?",
                         (*fields.values(), source_id))

    # --- outbox ----------------------------------------------------------
    def add_event(self, watch_id, event_id, service, payload):
        now = self.clock()
        with self.tx() as conn:
            watch = conn.execute("SELECT state, stop_after_match, expires_at FROM watches WHERE watch_id=?",
                                 (watch_id,)).fetchone()
            if watch is None or watch["state"] != "active" or watch["expires_at"] <= now:
                return False
            if conn.execute("SELECT 1 FROM outbox WHERE event_id=?", (event_id,)).fetchone():
                return False
            if _backlog_full(conn, watch_id):
                raise WatchError("backlog_full", "Delivery backlog is full; source checkpoint retained for retry")
            cursor = conn.execute(
                "INSERT OR IGNORE INTO outbox (event_id, watch_id, service, payload_json, state, "
                "created_at, updated_at) VALUES (?,?,?,?,'pending',?,?)",
                (event_id, watch_id, service, json.dumps(payload, sort_keys=True), now, now))
            if cursor.rowcount and watch["stop_after_match"]:
                conn.execute("UPDATE watches SET state='matched', collection='reserved' WHERE watch_id=?",
                             (watch_id,))
            return bool(cursor.rowcount)

    def apply_events(self, source_id, watch_events, *, cursor=None, cursor_staged=None,
                     clear_staged=False, last_error=None):
        """Fan out one delta page and its source checkpoint in one transaction.

        A crash before this call replays the page; unique event IDs make the replay
        harmless. A crash after it has both the outbox rows and the cursor.
        """
        now = self.clock()
        inserted = 0
        with self.tx() as conn:
            for watch_id, event_id, service, payload in watch_events:
                watch = conn.execute("SELECT state, stop_after_match, expires_at FROM watches WHERE watch_id=?",
                                     (watch_id,)).fetchone()
                if watch is None or watch["state"] != "active" or watch["expires_at"] <= now:
                    continue
                if conn.execute("SELECT 1 FROM outbox WHERE event_id=?", (event_id,)).fetchone():
                    continue
                if _backlog_full(conn, watch_id):
                    # ponytail: backpressure the shared source rather than discard a consumer's event.
                    raise WatchError("backlog_full", "Delivery backlog is full; source checkpoint retained for retry")
                cursor_row = conn.execute(
                    "INSERT OR IGNORE INTO outbox (event_id, watch_id, service, payload_json, state, "
                    "created_at, updated_at) VALUES (?,?,?,?,'pending',?,?)",
                    (event_id, watch_id, service, json.dumps(payload, sort_keys=True), now, now))
                if cursor_row.rowcount:
                    inserted += 1
                    if watch["stop_after_match"]:
                        conn.execute(
                            "UPDATE watches SET state='matched', collection='reserved' WHERE watch_id=?",
                            (watch_id,))
            assignments = ["last_reconcile=?", "seq=seq+1"]
            values = [now]
            if cursor is not None:
                assignments.append("cursor=?")
                values.append(cursor)
            if cursor_staged is not None:
                assignments.append("cursor_staged=?")
                values.append(cursor_staged)
            if clear_staged:
                assignments.append("cursor_staged=NULL")
            if last_error is not None:
                assignments.append("last_error=?")
                values.append(last_error[:500])
            elif cursor is not None or cursor_staged is not None:
                assignments.append("last_error=NULL")
            conn.execute(f"UPDATE sources SET {', '.join(assignments)} WHERE source_id=?",
                         (*values, source_id))
        return inserted

    def set_cursor(self, source_id, cursor, *, clear_staged=True):
        fields = {"cursor": cursor}
        if clear_staged:
            fields["cursor_staged"] = None
        self.update_source(source_id, **fields)

    def outbox(self, watch_id):
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE watch_id=? ORDER BY created_at", (watch_id,)).fetchall()
        return [dict(row) for row in rows]

    def due_outbox(self, now=None, limit=100):
        now = self.clock() if now is None else now
        with self.read() as conn:
            rows = conn.execute(
                "SELECT o.*, w.state AS watch_state, w.mode, w.stop_after_match, w.batch_seconds, "
                "w.route_name, w.bridge_name, w.account, w.service AS watch_service, w.generation "
                "FROM outbox o JOIN watches w ON w.watch_id=o.watch_id "
                "WHERE o.state IN ('pending','retry') AND o.next_attempt<=? "
                "AND w.state IN ('active','matched') ORDER BY o.created_at LIMIT ?",
                (now, limit)).fetchall()
        return [dict(row) for row in rows]

    def set_event(self, event_id, state, *, detail=None, next_attempt=None, request_id=None):
        now = self.clock()
        assignments = ["state=?", "updated_at=?", "attempts=attempts+1"]
        values = [state, now]
        if detail is not None:
            assignments.append("detail=?")
            values.append(detail[:500])
        if next_attempt is not None:
            assignments.append("next_attempt=?")
            values.append(next_attempt)
        if request_id is not None:
            assignments.append("request_id=?")
            values.append(request_id)
        with self.tx() as conn:
            conn.execute(f"UPDATE outbox SET {', '.join(assignments)} WHERE event_id=?",
                         (*values, event_id))

    def claim_outbox(self, event_ids, *, state, expected_states, request_id=None, generation=None):
        """Atomically claim outbox rows for one send attempt.

        Actual delivery eligibility lives in the shared watch row, not in a
        caller's snapshot: only a matched reservation (which survives its
        lifetime) or an unexpired active watch may forward.  Returns
        ``(watch, rows)`` in stable order, or ``(None, [])`` when any row no
        longer matches, so a stale batch is never sent.
        """
        ids = list(dict.fromkeys(event_ids))
        if not ids:
            return None, []
        marks = ",".join("?" for _ in ids)
        states = ",".join("?" for _ in expected_states)
        now = self.clock()
        with self.tx() as conn:
            rows = conn.execute(
                f"SELECT * FROM outbox WHERE event_id IN ({marks}) ORDER BY created_at, event_id",
                ids).fetchall()
            if len(rows) != len(ids):
                return None, []
            watch_ids = {row["watch_id"] for row in rows}
            if len(watch_ids) != 1:
                return None, []
            watch = conn.execute("SELECT * FROM watches WHERE watch_id=?",
                                 (watch_ids.pop(),)).fetchone()
            if watch is None:
                return None, []
            if watch["state"] != "matched" and not (
                    watch["state"] == "active" and float(watch["expires_at"]) > now):
                return None, []
            if generation is not None and int(watch["generation"]) != int(generation):
                return None, []
            if request_id is not None:
                # A durable request id defines the whole batch. Retry only when
                # every member is due and eligible, never a truncated subset.
                members = conn.execute(
                    "SELECT event_id FROM outbox WHERE watch_id=? AND request_id=?",
                    (watch["watch_id"], request_id)).fetchall()
                if members and {row["event_id"] for row in members} != set(ids):
                    return None, []
            for row in rows:
                if row["state"] not in expected_states:
                    return None, []
                if request_id is not None and row["request_id"] not in (None, "", request_id):
                    return None, []
            conn.execute(
                f"UPDATE outbox SET state=?, updated_at=?, attempts=attempts+1, "
                f"request_id=COALESCE(NULLIF(request_id,''), ?) WHERE event_id IN ({marks})",
                (state, now, request_id, *ids))
            rows = conn.execute(
                f"SELECT * FROM outbox WHERE event_id IN ({marks}) ORDER BY created_at, event_id",
                ids).fetchall()
            return dict(watch), [dict(row) for row in rows]

    def finish_outbox(self, event_ids, state, *, expected_states, detail=None, next_attempt=None):
        """Apply one terminal/retry outcome only to rows still in the expected state."""
        ids = list(dict.fromkeys(event_ids))
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        states = ",".join("?" for _ in expected_states)
        now = self.clock()
        assignments = ["state=?", "updated_at=?"]
        values = [state, now]
        if detail is not None:
            assignments.append("detail=?")
            values.append(detail[:500])
        if next_attempt is not None:
            assignments.append("next_attempt=?")
            values.append(next_attempt)
        with self.tx() as conn:
            cursor = conn.execute(
                f"UPDATE outbox SET {', '.join(assignments)} "
                f"WHERE event_id IN ({marks}) AND state IN ({states})",
                (*values, *ids, *expected_states))
            return cursor.rowcount

    def finish_bridge_event(self, event_id, state, *, expected_states, detail=None):
        """Settle one same-session event and its watch state together.

        The outbox transition is the guard: a late worker acknowledgement or a
        concurrent hook can never move an already-settled event backwards.
        """
        now = self.clock()
        states = ",".join("?" for _ in expected_states)
        with self.tx() as conn:
            cursor = conn.execute(
                f"UPDATE outbox SET state=?, detail=?, updated_at=? "
                f"WHERE event_id=? AND state IN ({states})",
                (state, detail[:500] if detail else None, now, event_id, *expected_states))
            if not cursor.rowcount:
                return False
            row = conn.execute("SELECT watch_id FROM outbox WHERE event_id=?",
                               (event_id,)).fetchone()
            conn.execute(
                "UPDATE watches SET delivery_state=?, last_error=? WHERE watch_id=?",
                (state, None if state == "scheduled" else (detail[:500] if detail else None),
                 row["watch_id"]))
        return True

    def bridge_accepted(self, event_id, detail):
        """Record a bridge HTTP ack without overwriting a hook-set outcome."""
        now = self.clock()
        with self.tx() as conn:
            cursor = conn.execute(
                "UPDATE outbox SET detail=?, updated_at=? WHERE event_id=? AND state='forwarding'",
                (detail[:500], now, event_id))
            if not cursor.rowcount:
                return False
            row = conn.execute("SELECT watch_id FROM outbox WHERE event_id=?",
                               (event_id,)).fetchone()
            conn.execute(
                "UPDATE watches SET delivery_state='pending', last_error=NULL "
                "WHERE watch_id=? AND delivery_state NOT IN ('scheduled','uncertain','failed','admitted')",
                (row["watch_id"],))
        return True

    def delivery_summary(self, watch_id):
        rows = self.outbox(watch_id)
        counts = {}
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        uncertain = (counts.get("uncertain", 0) + counts.get("forwarding", 0)
                     + counts.get("injecting", 0))
        if uncertain:
            state = "uncertain"
        elif counts.get("retry") or counts.get("pending"):
            state = "pending"
        elif counts.get("failed"):
            state = "failed"
        elif counts.get("scheduled"):
            state = "scheduled"
        elif counts.get("admitted"):
            state = "admitted"
        else:
            state = "none"
        return {"state": state, "counts": counts, "backlog": sum(
            count for key, count in counts.items()
            if key in ("pending", "retry", "forwarding", "injecting"))}

    # --- hints and remotes ----------------------------------------------
    def add_hint(self, hint_id, source_id, payload=None):
        now = self.clock()
        with self.tx() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO hints (hint_id, source_id, state, payload_json, created_at) "
                "VALUES (?,?,'pending',?,?)",
                (hint_id, source_id, json.dumps(payload or {}, sort_keys=True), now))
        return bool(cursor.rowcount)

    def pending_hints(self, source_id=None):
        query = "SELECT * FROM hints WHERE state='pending'"
        values = ()
        if source_id:
            query += " AND source_id=?"
            values = (source_id,)
        with self.read() as conn:
            rows = conn.execute(query + " ORDER BY created_at LIMIT 100", values).fetchall()
        return [dict(row) for row in rows]

    def settle_hints(self, source_id):
        with self.tx() as conn:
            conn.execute("UPDATE hints SET state='done' WHERE source_id=? AND state='pending'", (source_id,))

    def add_remote(self, remote_id, kind, *, watch_id=None, source_id=None, identity=None,
                   state="pending", expires_at=None, detail=None):
        now = self.clock()
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO remotes (remote_id, watch_id, source_id, kind, identity_json, state, "
                "expires_at, detail, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(remote_id) DO UPDATE SET state=excluded.state, expires_at=excluded.expires_at, "
                "detail=excluded.detail, identity_json=excluded.identity_json, updated_at=excluded.updated_at",
                (remote_id, watch_id, source_id, kind, json.dumps(identity or {}, sort_keys=True),
                 state, expires_at, detail, now))

    def remotes(self, *, watch_id=None, source_id=None):
        clauses, values = [], []
        if watch_id is not None:
            clauses.append("watch_id=?")
            values.append(watch_id)
        if source_id is not None:
            clauses.append("source_id=?")
            values.append(source_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.read() as conn:
            rows = conn.execute(f"SELECT * FROM remotes{where} ORDER BY updated_at", values).fetchall()
        return [dict(row) for row in rows]

    def get_remote(self, remote_id):
        with self.read() as conn:
            row = conn.execute("SELECT * FROM remotes WHERE remote_id=?", (remote_id,)).fetchone()
        return dict(row) if row is not None else None

    def update_remote(self, remote_id, **fields):
        allowed = {"state", "expires_at", "detail", "identity_json"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown remote fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.tx() as conn:
            conn.execute(f"UPDATE remotes SET {assignments}, updated_at=? WHERE remote_id=?",
                         (*fields.values(), self.clock(), remote_id))

    def remote_for_source(self, source_id, kind_prefix):
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM remotes WHERE source_id=? AND kind LIKE ? AND state IN ('active','pending') "
                "ORDER BY updated_at DESC LIMIT 1", (source_id, kind_prefix + "%")).fetchone()
        return dict(row) if row is not None else None

    def recover_stale_outbox(self, now=None, *, stale_after=60):
        now = self.clock() if now is None else now
        with self.tx() as conn:
            conn.execute(
                "UPDATE outbox SET state='uncertain', updated_at=?, detail=? "
                "WHERE state='injecting' AND updated_at<?",
                (now, "Session injection was claimed but no outcome was recorded; not replayed",
                 now - stale_after))
            conn.execute(
                "UPDATE outbox SET state='uncertain', updated_at=?, detail=? "
                "WHERE state='forwarding' AND updated_at<? AND watch_id IN "
                "(SELECT watch_id FROM watches WHERE mode='same_session')",
                (now, "Bridge request accepted but no admission receipt was observed", now - stale_after))
            conn.execute(
                "UPDATE outbox SET state='retry', next_attempt=?, updated_at=? "
                "WHERE state='forwarding' AND updated_at<? AND watch_id IN "
                "(SELECT watch_id FROM watches WHERE mode='new_session')",
                (now, now, now - stale_after))

    def remove_remote(self, remote_id):
        with self.tx() as conn:
            conn.execute("DELETE FROM remotes WHERE remote_id=?", (remote_id,))

    def stop_source_if_unused(self, source_id):
        """Mark a source stopped only if no active watch re-armed it meanwhile.

        Remote stop calls run outside any transaction; a concurrent start can
        reset and reuse the same source while they are in flight.
        """
        with self.tx() as conn:
            cursor = conn.execute(
                "UPDATE sources SET state='stopped' WHERE source_id=? AND NOT EXISTS ("
                "SELECT 1 FROM watch_sources x JOIN watches w ON w.watch_id=x.watch_id "
                "WHERE x.source_id=? AND w.state='active')", (source_id, source_id))
            return bool(cursor.rowcount)

    def prune(self, now=None, *, retention_seconds=7 * 86400):
        """Delete terminal, fully cleaned records older than the retention window.

        Only ``cleanup_state='done'`` watches go (their route is already removed);
        ``retained``/``pending`` cleanup keeps uncertain or admitted native
        routes and their outbox evidence until an explicit stop finishes them.
        """
        now = self.clock() if now is None else now
        cutoff = now - retention_seconds
        with self.tx() as conn:
            # ponytail: age from expires_at (no terminal timestamp column); a stop
            # can precede expiry by at most MAX_MINUTES, which only delays pruning.
            ids = [row["watch_id"] for row in conn.execute(
                "SELECT watch_id FROM watches w WHERE cleanup_state='done' "
                "AND state IN ('stopped','expired','matched') AND expires_at<? "
                "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.watch_id=w.watch_id "
                "AND (o.updated_at>=? OR o.state IN ('pending','retry','forwarding','injecting','uncertain')))",
                (cutoff, cutoff)).fetchall()]
            for watch_id in ids:
                for table in ("outbox", "watch_sources", "remotes", "watches"):
                    conn.execute(f"DELETE FROM {table} WHERE watch_id=?", (watch_id,))
            conn.execute("DELETE FROM hints WHERE state='done' AND created_at<?", (cutoff,))
            conn.execute("DELETE FROM delivered WHERE at<?", (cutoff,))
            # Lapsed Google leases (Google stops calling after expiry) and stale
            # registration-error diagnostics; owned native routes are never touched here.
            conn.execute("DELETE FROM remotes WHERE kind!='native_route' AND ("
                         "(expires_at IS NOT NULL AND expires_at<?) "
                         "OR (state='error' AND updated_at<?))", (cutoff, cutoff))
            orphans = [row["source_id"] for row in conn.execute(
                "SELECT source_id FROM sources s WHERE state='stopped' AND NOT EXISTS ("
                "SELECT 1 FROM watch_sources x WHERE x.source_id=s.source_id) AND NOT EXISTS ("
                "SELECT 1 FROM remotes r WHERE r.source_id=s.source_id)").fetchall()]
            for source_id in orphans:
                conn.execute("DELETE FROM hints WHERE source_id=?", (source_id,))
                conn.execute("DELETE FROM sources WHERE source_id=?", (source_id,))
                conn.execute("DELETE FROM meta WHERE key IN (?,?)",
                             ("backoff:sub:" + source_id, "backoff:stop:" + source_id))
        return {"watches": len(ids), "sources": len(orphans)}

    # --- meta ------------------------------------------------------------
    def set_meta(self, key, value):
        with self.tx() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def delete_meta(self, key):
        with self.tx() as conn:
            conn.execute("DELETE FROM meta WHERE key=?", (key,))

    def get_meta(self, key, default=None):
        with self.read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default


def create_watch(store, service, spec, origin, *, watch_id=None, route_name=None, bridge_name=None,
                 sources=(), baseline=None):
    """Create one watch, its shared sources, and the first baseline records."""
    watch_id = watch_id or secrets.token_hex(8)
    now = store.clock()
    record = {
        "watch_id": watch_id, "service": service, "account": spec["account"],
        "profile": origin["profile"], "principal": origin["principal"], "origin": origin,
        "mode": spec["mode"], "target": spec["target"], "filters": spec["filters"],
        "instructions": spec["instructions"], "skills": spec["skills"],
        "minutes": spec["minutes"], "batch_seconds": spec["batch_seconds"],
        "stop_after_match": spec["stop_after_match"], "created_at": now, "not_before": now,
        "expires_at": now + spec["minutes"] * 60, "route_name": route_name,
        "bridge_name": bridge_name, "baseline": baseline or {},
        "fingerprint": spec["fingerprint"],
    }
    return store.add_watch(record, sources=sources)


def watch_status(store, watch):
    """Resolved, secret-free status for one watch."""
    now = store.clock()
    delivery = store.delivery_summary(watch["watch_id"])
    sources = store.sources_for(watch["watch_id"])
    lease = min((s.get("expires_at") or 0 for s in sources), default=0)
    remotes = store.remotes(watch_id=watch["watch_id"])
    return {
        "watch_id": watch["watch_id"],
        "service": watch["service"],
        "account": watch["account"],
        "delivery_mode": watch["mode"],
        "state": watch["state"],
        "collection": watch["collection"],
        "target": json.loads(watch["target_json"]),
        "filters": json.loads(watch["filters_json"]),
        "instructions": watch["instructions"],
        "skills": json.loads(watch["skills_json"]),
        "created_at": now_iso(watch["created_at"]),
        "expires_at": now_iso(watch["expires_at"]),
        "expires_in_seconds": max(0, int(watch["expires_at"] - now)),
        "stop_after_match": bool(watch["stop_after_match"]),
        "batch_seconds": watch["batch_seconds"],
        "generation": watch["generation"],
        "native_route": watch["route_name"],
        "bridge_route": watch["bridge_name"],
        "delivery": delivery,
        "delivery_completion": "unobservable" if delivery["counts"].get("admitted") else None,
        "backlog": delivery["backlog"],
        "lease_expiry": now_iso(lease) if lease else None,
        "last_reconcile": now_iso(min((s.get("last_reconcile") or 0 for s in sources), default=0))
        if sources else None,
        "remote_cleanup": watch["cleanup_state"],
        "cleanup_reason": watch["cleanup_reason"],
        "gap": watch["gap"],
        "last_error": watch["last_error"] or next((s["last_error"] for s in sources if s.get("last_error")), None),
        "remotes": [{"kind": r["kind"], "state": r["state"],
                     "expires_at": now_iso(r["expires_at"]) if r["expires_at"] else None}
                    for r in remotes],
    }


def operator_list(store, *, service=None, all_profiles=False):
    rows = store.list_watches(service=service)
    if not all_profiles:
        return rows
    return rows


def operator_stop(store, watch_id):
    watch = store.get_watch(watch_id)
    if watch is None:
        raise WatchError("not_found", "No watch with that ID in this profile")
    store.stop(watch_id)
    return store.get_watch(watch_id)
