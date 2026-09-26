"""施工变更影响审批服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS change_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','engineer','site','approver','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zones (
    zone_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL REFERENCES zones(zone_id),
    revision INTEGER NOT NULL,
    housing_units TEXT NOT NULL,
    water_drainage TEXT NOT NULL,
    school_seats TEXT NOT NULL,
    road_capacity TEXT NOT NULL,
    fire_coverage TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    supersedes_version_id INTEGER REFERENCES service_versions(version_id),
    created_by TEXT NOT NULL REFERENCES change_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(zone_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_service_versions_zone
ON service_versions(zone_id, revision);

CREATE TABLE IF NOT EXISTS buildings (
    building_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL REFERENCES zones(zone_id),
    name TEXT NOT NULL,
    households INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'standing' CHECK(state IN ('standing','modified','demolished')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL REFERENCES zones(zone_id),
    applicant TEXT NOT NULL,
    housing_units TEXT NOT NULL,
    water_drainage TEXT NOT NULL,
    school_seats TEXT NOT NULL,
    road_capacity TEXT NOT NULL,
    fire_coverage TEXT NOT NULL,
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'approved' CHECK(state IN ('approved','excluded','withdrawn')),
    excluded_by TEXT REFERENCES changes(change_id),
    exclusion_json TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES change_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_applications_zone_state
ON applications(zone_id, state, priority, created_at);

CREATE TABLE IF NOT EXISTS changes (
    change_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL REFERENCES zones(zone_id),
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN
        ('draft','submitted','approved','rejected','in_progress','completed',
         'failed','rolling_back','rolled_back','manual_takeover','manual_closed','cancelled')),
    current_revision INTEGER NOT NULL DEFAULT 1,
    approved_revision INTEGER,
    bundle_sha256 TEXT,
    submitted_by TEXT REFERENCES change_users(user_id),
    submitted_at TEXT,
    decided_by TEXT REFERENCES change_users(user_id),
    decided_at TEXT,
    decision_reason TEXT,
    failed_phase TEXT,
    failed_step TEXT,
    failure_note TEXT,
    takeover_by TEXT REFERENCES change_users(user_id),
    takeover_note TEXT,
    takeover_report TEXT,
    result_version_id INTEGER REFERENCES service_versions(version_id),
    created_by TEXT NOT NULL REFERENCES change_users(user_id),
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS change_revisions (
    change_id TEXT NOT NULL REFERENCES changes(change_id),
    revision INTEGER NOT NULL,
    parent_revision INTEGER,
    payload_json TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    impact_json TEXT,
    snapshot_version_id INTEGER REFERENCES service_versions(version_id),
    created_by TEXT NOT NULL REFERENCES change_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(change_id, revision)
);

CREATE TABLE IF NOT EXISTS change_steps (
    change_id TEXT NOT NULL REFERENCES changes(change_id),
    phase TEXT NOT NULL CHECK(phase IN ('execution','rollback')),
    seq INTEGER NOT NULL,
    step_key TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','failed')),
    receipt_note TEXT,
    receipt_by TEXT REFERENCES change_users(user_id),
    receipt_at TEXT,
    PRIMARY KEY(change_id, phase, seq)
);

CREATE TABLE IF NOT EXISTS change_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS change_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_audit_entity
ON change_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # 服务在 HTTP 层用锁串行处理请求，连接可跨线程复用。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
