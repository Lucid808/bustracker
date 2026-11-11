"""SQLite persistence layer for the bus tracker."""
import json
import os
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracker.db")
_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS buses (
            id          TEXT PRIMARY KEY,
            lat         REAL,
            lng         REAL,
            ts          REAL,
            finished    INTEGER NOT NULL DEFAULT 0,
            started_at  REAL,
            next_index  INTEGER NOT NULL DEFAULT 0,
            passed      TEXT NOT NULL DEFAULT '[]',
            sms_sent    TEXT NOT NULL DEFAULT '[]',
            late_sms    INTEGER NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'waiting',
            delay_min   REAL NOT NULL DEFAULT 0,
            last_update REAL
        );

        CREATE TABLE IF NOT EXISTS events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            type      TEXT NOT NULL,
            bus       TEXT NOT NULL,
            payload   TEXT NOT NULL,
            created   REAL NOT NULL
        );
        """)


def load_buses():
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM buses").fetchall()
    return {r["id"]: dict(r) for r in rows}


def save_bus(bus):
    with _lock, _connect() as conn:
        conn.execute("""
        INSERT INTO buses (id, lat, lng, ts, finished, started_at, next_index,
                           passed, sms_sent, late_sms, status, delay_min, last_update)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            lat=excluded.lat, lng=excluded.lng, ts=excluded.ts,
            finished=excluded.finished, started_at=excluded.started_at,
            next_index=excluded.next_index, passed=excluded.passed,
            sms_sent=excluded.sms_sent, late_sms=excluded.late_sms,
            status=excluded.status, delay_min=excluded.delay_min,
            last_update=excluded.last_update
        """, (
            bus["id"], bus["lat"], bus["lng"], bus["ts"],
            int(bus["finished"]), bus["started_at"], bus["next_index"],
            json.dumps(bus["passed"]), json.dumps(sorted(bus["sms_sent"])),
            int(bus["late_sms_sent"]), bus["status"], bus["delay_min"],
            bus["last_update"],
        ))


def delete_bus(bus_id):
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM buses WHERE id=?", (bus_id,))


def add_event(kind, bus_id, payload):
    import time
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO events (type, bus, payload, created) VALUES (?,?,?,?)",
            (kind, bus_id, json.dumps(payload), time.time()),
        )


def recent_events(limit=50, bus_id=None, since=None, until=None):
    q = "SELECT * FROM events WHERE 1=1"
    args = []
    if bus_id:
        q += " AND bus=?"; args.append(bus_id)
    if since is not None:
        q += " AND created>=?"; args.append(since)
    if until is not None:
        q += " AND created<=?"; args.append(until)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    with _lock, _connect() as conn:
        rows = conn.execute(q, args).fetchall()
    return [
        {
            "id": r["id"],
            "type": r["type"],
            "bus": r["bus"],
            "payload": json.loads(r["payload"]),
            "created": r["created"],
        }
        for r in rows
    ]
