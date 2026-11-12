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


# ============================================================================
# Schools, buses (config), parents, students
# ============================================================================

def _ensure_extended_tables():
    with _lock, _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS schools (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            key        TEXT NOT NULL UNIQUE,
            created    REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bus_configs (
            id           TEXT PRIMARY KEY,
            school_id    TEXT NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            plate        TEXT,
            driver_token TEXT NOT NULL UNIQUE,
            stops_json   TEXT NOT NULL,
            created      REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS parents (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id  TEXT NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
            token      TEXT NOT NULL UNIQUE,
            name       TEXT NOT NULL,
            phone      TEXT NOT NULL,
            created    REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS students (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id  TEXT NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
            parent_id  INTEGER NOT NULL REFERENCES parents(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            grade      TEXT,
            stop_id    TEXT,
            bus_id     TEXT,
            created    REAL NOT NULL
        );
        """)


# ---- schools ----

def list_schools():
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM schools ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_school(school_id):
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM schools WHERE id=?", (school_id,)).fetchone()
    return dict(row) if row else None


def add_school(school_id, name, key):
    import time
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO schools (id, name, key, created) VALUES (?,?,?,?)",
            (school_id, name, key, time.time()),
        )
    return get_school(school_id)


def delete_school(school_id):
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM schools WHERE id=?", (school_id,))


# ---- bus configs ----

def list_bus_configs(school_id=None):
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        if school_id:
            rows = conn.execute(
                "SELECT * FROM bus_configs WHERE school_id=? ORDER BY name",
                (school_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM bus_configs ORDER BY school_id, name").fetchall()
    import json as _json
    out = []
    for r in rows:
        d = dict(r)
        d["stops"] = _json.loads(d.pop("stops_json"))
        out.append(d)
    return out


def get_bus_config(bus_id):
    buses = [b for b in list_bus_configs() if b["id"] == bus_id]
    return buses[0] if buses else None


def add_bus_config(bus_id, school_id, name, plate, driver_token, stops):
    import json as _json, time
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO bus_configs
               (id, school_id, name, plate, driver_token, stops_json, created)
               VALUES (?,?,?,?,?,?,?)""",
            (bus_id, school_id, name, plate, driver_token, _json.dumps(stops), time.time()),
        )
    return get_bus_config(bus_id)


def delete_bus_config(bus_id):
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM bus_configs WHERE id=?", (bus_id,))


# ---- parents ----

def list_parents(school_id=None):
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        if school_id:
            rows = conn.execute(
                "SELECT * FROM parents WHERE school_id=? ORDER BY name",
                (school_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM parents ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_parent_by_token(token):
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM parents WHERE token=?", (token,)).fetchone()
    return dict(row) if row else None


def get_parent_by_id(parent_id):
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM parents WHERE id=?", (parent_id,)).fetchone()
    return dict(row) if row else None


def add_parent(school_id, token, name, phone):
    import time
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO parents (school_id, token, name, phone, created) VALUES (?,?,?,?,?)",
            (school_id, token, name, phone, time.time()),
        )
        parent_id = cur.lastrowid
    return get_parent_by_id(parent_id)


def delete_parent(parent_id):
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM parents WHERE id=?", (parent_id,))


# ---- students ----

def list_students(school_id=None, parent_id=None):
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        q = """SELECT s.*, p.name AS parent_name, p.phone AS parent_phone, p.token AS parent_token
               FROM students s LEFT JOIN parents p ON p.id = s.parent_id WHERE 1=1"""
        args = []
        if school_id:
            q += " AND s.school_id=?"; args.append(school_id)
        if parent_id:
            q += " AND s.parent_id=?"; args.append(parent_id)
        q += " ORDER BY s.name"
        rows = conn.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def get_student(student_id):
    students = [s for s in list_students() if s["id"] == student_id]
    return students[0] if students else None


def add_student(school_id, parent_id, name, grade, stop_id, bus_id):
    import time
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO students
               (school_id, parent_id, name, grade, stop_id, bus_id, created)
               VALUES (?,?,?,?,?,?,?)""",
            (school_id, parent_id, name, grade, stop_id, bus_id, time.time()),
        )
        student_id = cur.lastrowid
    return get_student(student_id)


def delete_student(student_id):
    _ensure_extended_tables()
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM students WHERE id=?", (student_id,))


def import_students_csv(school_id, rows):
    """
    rows: list of dicts with keys name, grade, parent_name, parent_phone, stop_id, bus_id
    Returns dict with counts: {added, parents_created, skipped, errors}
    """
    added = 0
    parents_created = 0
    skipped = 0
    errors = []
    for i, row in enumerate(rows, start=2):  # 2 = header line offset
        try:
            name = (row.get("name") or "").strip()
            parent_name = (row.get("parent_name") or "").strip()
            parent_phone = (row.get("parent_phone") or "").strip()
            if not name or not parent_name or not parent_phone:
                errors.append(f"row {i}: missing name/parent_name/parent_phone")
                skipped += 1
                continue

            existing_parents = [p for p in list_parents(school_id)
                                if p["phone"] == parent_phone]
            if existing_parents:
                parent = existing_parents[0]
            else:
                token = "parent-" + __import__("secrets").token_hex(6)
                parent = add_parent(school_id, token, parent_name, parent_phone)
                parents_created += 1

            add_student(
                school_id=school_id,
                parent_id=parent["id"],
                name=name,
                grade=(row.get("grade") or "").strip(),
                stop_id=(row.get("stop_id") or "").strip(),
                bus_id=(row.get("bus_id") or "").strip(),
            )
            added += 1
        except Exception as e:
            errors.append(f"row {i}: {e}")
            skipped += 1
    return {
        "added": added,
        "parents_created": parents_created,
        "skipped": skipped,
        "errors": errors,
    }


def seed_demo_school():
    """Idempotent seed for the existing demo data."""
    _ensure_extended_tables()
    if get_school("greenpark"):
        return
    s = add_school("greenpark", "Greenpark Academy", "school-demo-key")
    add_bus_config(
        bus_id="bus4",
        school_id="greenpark",
        name="Bus 4",
        plate="KDA 004X",
        driver_token="driver-demo-bus4",
        stops=[
            {"id": "kitengela", "name": "Kitengela Stage",       "lat": -1.4745, "lng": 36.9570, "offset_min": 0},
            {"id": "milimani",  "name": "Milimani Gate",         "lat": -1.4660, "lng": 36.9700, "offset_min": 8},
            {"id": "greenpark", "name": "Greenpark Estate gate", "lat": -1.4560, "lng": 36.9820, "offset_min": 16},
            {"id": "school",    "name": "Greenpark Academy",     "lat": -1.4400, "lng": 37.0000, "offset_min": 32},
        ],
    )
    p = add_parent("greenpark", "parent-demo-amani", "Mrs. Amani", "+254700000001")
    add_student("greenpark", p["id"], "Amani Jr.", "Grade 4", "greenpark", "bus4")
