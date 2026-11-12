from dotenv import load_dotenv
load_dotenv()

"""School bus tracker - minimal Flask app that satisfies test_tracker.py."""
import math
import os
import time

from flask import Flask, jsonify, redirect, render_template, render_template_string, request, session

import db
import sms

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-only-change-me-in-production")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Only require HTTPS cookies when NOT on localhost (Render = production)
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
)

db.init_db()
db.seed_demo_school()

ALLOW_CLIENT_TS = os.environ.get("ALLOW_CLIENT_TS") == "1"

SCHOOL_KEY = os.environ.get("SCHOOL_KEY", "school-demo-key")
DRIVER_TOKENS = {"driver-demo-bus4": "bus4"}

CFG = {
    "buses": [
        {
            "id": "bus4",
            "name": "Bus 4",
            "plate": "KDA 004X",
            "driver_token": "driver-demo-bus4",
            "stops": [
                {"id": "kitengela", "name": "Kitengela Stage",       "lat": -1.4745, "lng": 36.9570, "offset_min": 0},
                {"id": "milimani",  "name": "Milimani Gate",         "lat": -1.4660, "lng": 36.9700, "offset_min": 8},
                {"id": "greenpark", "name": "Greenpark Estate gate", "lat": -1.4560, "lng": 36.9820, "offset_min": 16},
                {"id": "acacia",    "name": "Acacia Mall",           "lat": -1.4480, "lng": 36.9900, "offset_min": 24},
                {"id": "school",    "name": "School",                "lat": -1.4400, "lng": 37.0000, "offset_min": 32},
            ],
        }
    ]
}

def _bus_from_row(cfg, row):
    """Merge a DB row back into the runtime bus dict."""
    bus = new_bus(cfg)
    if row is None:
        return None
    bus.update({
        "lat": row["lat"],
        "lng": row["lng"],
        "ts": row["ts"],
        "finished": bool(row["finished"]),
        "started_at": row["started_at"],
        "next_index": row["next_index"],
        "passed": __import__("json").loads(row["passed"]),
        "sms_sent": set(__import__("json").loads(row["sms_sent"])),
        "late_sms_sent": bool(row["late_sms"]),
        "status": row["status"],
        "delay_min": row["delay_min"],
        "last_update": row["last_update"],
    })
    return bus


def _cfgs():
    return {b["id"]: b for b in CFG["buses"]}


def load_all_buses():
    cfgs = _cfgs()
    rows = db.load_buses()
    out = {}
    for bus_id, cfg in cfgs.items():
        out[bus_id] = _bus_from_row(cfg, rows.get(bus_id))
    return out



SMS_THRESHOLDS = [10, 5, 2]
DELAY_THRESHOLD_MIN = 5
ARRIVAL_RADIUS_KM = 0.15


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


PARENTS = {
    "parent-demo-amani": {
        "phone": os.environ.get("PARENT_AMANI_PHONE", "+254700000001"),
        "bus": "bus4",
    },
}


def send_sms(phone, text):
    return sms.send(phone, text)


def new_bus(cfg):
    return {
        "id": cfg["id"],
        "cfg": cfg,
        "lat": None,
        "lng": None,
        "ts": None,
        "finished": False,
        "started_at": None,
        "next_index": 0,
        "passed": [False] * len(cfg["stops"]),
        "sms_sent": set(),
        "late_sms_sent": False,
        "status": "waiting",
        "delay_min": 0,
        "last_update": None,
    }



# --- initialise runtime state from the DB (after new_bus is defined) ---
BUSES = load_all_buses()
EVENTS = db.recent_events(200)

def _estimate(bus):
    cfg = bus["cfg"]
    if bus["next_index"] >= len(cfg["stops"]):
        return 0.0, 0.0
    stop = cfg["stops"][bus["next_index"]]
    if bus["lat"] is None:
        return float("inf"), float("inf")
    km = haversine_km(bus["lat"], bus["lng"], stop["lat"], stop["lng"])
    eta = km / 0.4167
    return km, eta


def _snapshot(bus):
    cfg = bus["cfg"]
    stops_payload = []
    for i, s in enumerate(cfg["stops"]):
        km = eta = None
        if bus["lat"] is not None and not bus["passed"][i]:
            km = haversine_km(bus["lat"], bus["lng"], s["lat"], s["lng"])
            eta = km / 0.4167
        stops_payload.append({
            "id": s["id"],
            "name": s["name"],
            "lat": s["lat"],
            "lng": s["lng"],
            "passed": bus["passed"][i],
            "km": round(km, 3) if km is not None else None,
            "eta_min": round(eta, 1) if eta is not None else None,
        })
    next_stop_name = (
        cfg["stops"][bus["next_index"]]["name"]
        if bus["next_index"] < len(cfg["stops"])
        else None
    )
    return {
        "bus_id": bus["id"],
        "plate": cfg.get("plate"),
        "lat": bus["lat"],
        "lng": bus["lng"],
        "status": bus["status"],
        "delay_min": bus["delay_min"],
        "next_stop": next_stop_name,
        "stops": stops_payload,
    }


def _check_arrival(bus):
    cfg = bus["cfg"]
    while bus["next_index"] < len(cfg["stops"]):
        stop = cfg["stops"][bus["next_index"]]
        if bus["lat"] is None:
            break
        km = haversine_km(bus["lat"], bus["lng"], stop["lat"], stop["lng"])
        if km <= ARRIVAL_RADIUS_KM:
            bus["passed"][bus["next_index"]] = True
            bus["next_index"] += 1
            bus["sms_sent"].clear()
            bus["late_sms_sent"] = False
        else:
            break
    if bus["next_index"] >= len(cfg["stops"]):
        bus["finished"] = True
        bus["status"] = "finished"


def _check_sms(bus):
    cfg = bus["cfg"]
    if bus["next_index"] >= len(cfg["stops"]):
        return
    next_stop = cfg["stops"][bus["next_index"]]
    _, eta = _estimate(bus)
    if eta == float("inf"):
        return
    for thresh in SMS_THRESHOLDS:
        if eta <= thresh and thresh not in bus["sms_sent"]:
            bus["sms_sent"].add(thresh)
            msg = f"Bus {cfg['name']} {thresh} min from {next_stop['name']}"
            for token, info in PARENTS.items():
                send_sms(info["phone"], msg)
            db.add_event("approach", bus["id"], {"stop": next_stop["id"], "eta_min": round(eta, 1)})


def _check_late(bus, now_ts):
    cfg = bus["cfg"]
    if bus["finished"] or bus["started_at"] is None:
        return
    if bus["next_index"] >= len(cfg["stops"]):
        return
    scheduled_due = bus["started_at"] + cfg["stops"][bus["next_index"]]["offset_min"] * 60
    delay = (now_ts - scheduled_due) / 60.0
    bus["delay_min"] = round(max(0.0, delay), 1)
    bus["status"] = "delayed" if delay > DELAY_THRESHOLD_MIN else "on-time"
    if delay > DELAY_THRESHOLD_MIN and not bus["late_sms_sent"]:
        bus["late_sms_sent"] = True
        msg = f"Bus {cfg['name']} is running late by {int(delay)} min"
        for token, info in PARENTS.items():
            send_sms(info["phone"], msg)
        db.add_event("late", bus["id"], {"delay_min": int(delay)})


@app.post("/api/driver/ping")
def driver_ping():
    token = request.headers.get("X-Driver-Token", "")
    bus_id = (request.get_json(silent=True) or {}).get("bus_id")
    if DRIVER_TOKENS.get(token) != bus_id:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        lat = float(data["lat"])
        lng = float(data["lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "bad coordinates"}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"error": "bad coordinates"}), 400

    ts = data.get("ts")
    if ts is None or not isinstance(ts, (int, float)):
        if not ALLOW_CLIENT_TS:
            return jsonify({"error": "bad timestamp"}), 400
        ts = time.time()
    ts = float(ts)

    bus = BUSES.get(bus_id)
    if bus is None:
        return jsonify({"error": "unknown bus"}), 404
    if bus["finished"]:
        return jsonify({"error": "trip finished"}), 409

    if bus["started_at"] is None:
        bus["started_at"] = ts
    bus["lat"] = lat
    bus["lng"] = lng
    bus["ts"] = ts
    bus["last_update"] = time.time()

    _check_arrival(bus)
    _check_sms(bus)
    _check_late(bus, ts)
    db.save_bus(bus)
    return jsonify(_snapshot(bus))


@app.post("/api/driver/start")
def driver_start():
    token = request.headers.get("X-Driver-Token", "")
    bus_id = (request.get_json(silent=True) or {}).get("bus_id")
    if DRIVER_TOKENS.get(token) != bus_id:
        return jsonify({"error": "unauthorized"}), 401
    cfg = next((b for b in CFG["buses"] if b["id"] == bus_id), None)
    if cfg is None:
        return jsonify({"error": "unknown bus"}), 404
    BUSES[bus_id] = new_bus(cfg)
    db.delete_bus(bus_id)
    db.save_bus(BUSES[bus_id])
    return jsonify({"ok": True, "bus_id": bus_id})


@app.get("/api/fleet")
def api_fleet():
    if not _school_ok():
        return jsonify({"error": "forbidden"}), 403
    return jsonify({
        "buses": [_snapshot(b) for b in BUSES.values() if b is not None],
    })


def _parent_bus_map():
    return {"parent-demo-amani": "bus4"}


@app.get("/api/parent/<token>")
def api_parent(token):
    bus_id = _parent_bus_map().get(token)
    if bus_id is None:
        return jsonify({"error": "not found"}), 404
    bus = BUSES.get(bus_id)
    if bus is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_snapshot(bus))


@app.get("/parent/<token>")
def parent_page(token):
    bus_id = _parent_bus_map().get(token)
    if bus_id is None:
        return "Not found", 404
    cfg = next((b for b in CFG["buses"] if b["id"] == bus_id), None)
    if cfg is None:
        return "Not found", 404
    stops = [
        {"id": s["id"], "name": s["name"], "lat": s["lat"], "lng": s["lng"]}
        for s in cfg["stops"]
    ]
    center = [cfg["stops"][0]["lat"], cfg["stops"][0]["lng"]]
    return render_template(
        "parent.html",
        token=token,
        bus_name=cfg["name"],
        stops=stops,
        center=center,
    )


@app.get("/api/events")
def api_events():
    if not _school_ok():
        return jsonify({"error": "forbidden"}), 403
    limit = min(int(request.args.get("limit", 50)), 200)
    bus_id = request.args.get("bus") or None

    def parse_date(s):
        if not s:
            return None
        from datetime import datetime
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None

    since = parse_date(request.args.get("from"))
    until = parse_date(request.args.get("to"))
    if until is not None:
        until += 86399  # include the whole "to" day
    return jsonify({
        "events": db.recent_events(limit=limit, bus_id=bus_id, since=since, until=until),
        "from": request.args.get("from"),
        "to": request.args.get("to"),
    })


def _school_ok():
    """True if the caller is logged in OR supplied the correct ?key."""
    if session.get("school") is True:
        return True
    return request.args.get("key") == SCHOOL_KEY


@app.get("/driver")
def driver_page():
    token = request.args.get("token", "")
    bus_id = DRIVER_TOKENS.get(token)
    if bus_id is None:
        return "Not found", 404
    cfg = next((b for b in CFG["buses"] if b["id"] == bus_id), None)
    if cfg is None:
        return "Not found", 404
    return render_template(
        "driver.html",
        token=token,
        bus_id=bus_id,
        bus_name=cfg["name"],
        plate=cfg.get("plate", ""),
    )


# ============================================================================
# Admin routes — schools, buses, parents, students
# ============================================================================

import secrets as _secrets
import re as _re


def _require_admin():
    """Return redirect if not admin, else None."""
    if session.get("school") is not True:
        return redirect("/login")
    return None


def _slug(name):
    return _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@app.get("/admin")
def admin_dashboard():
    r = _require_admin()
    if r: return r
    schools = db.list_schools()
    counts = {
        "schools":  len(schools),
        "buses":    len(db.list_bus_configs()),
        "parents":  len(db.list_parents()),
        "students": len(db.list_students()),
    }
    return render_template("admin_dashboard.html", counts=counts, schools=schools)


# ---- schools ----

@app.get("/admin/schools")
def admin_schools():
    r = _require_admin()
    if r: return r
    return render_template("admin_schools.html", schools=db.list_schools(), error=None)


@app.post("/admin/schools")
def admin_schools_post():
    r = _require_admin()
    if r: return r
    name = (request.form.get("name") or "").strip()
    if not name:
        return render_template("admin_schools.html",
                               schools=db.list_schools(),
                               error="Name is required"), 400
    school_id = _slug(name)
    if db.get_school(school_id):
        return render_template("admin_schools.html",
                               schools=db.list_schools(),
                               error=f"A school with id '{school_id}' already exists"), 400
    key = _secrets.token_urlsafe(32)
    school = db.add_school(school_id, name, key)
    return render_template("admin_schools.html",
                           schools=db.list_schools(),
                           error=None,
                           created=school)


@app.post("/admin/schools/<school_id>/delete")
def admin_schools_delete(school_id):
    r = _require_admin()
    if r: return r
    db.delete_school(school_id)
    return redirect("/admin/schools")


# ---- buses ----

@app.get("/admin/buses")
def admin_buses():
    r = _require_admin()
    if r: return r
    return render_template("admin_buses.html",
                           schools=db.list_schools(),
                           buses=db.list_bus_configs(),
                           error=None)


@app.post("/admin/buses")
def admin_buses_post():
    r = _require_admin()
    if r: return r
    school_id = (request.form.get("school_id") or "").strip()
    name      = (request.form.get("name") or "").strip()
    plate     = (request.form.get("plate") or "").strip()
    if not (school_id and name):
        return render_template("admin_buses.html",
                               schools=db.list_schools(),
                               buses=db.list_bus_configs(),
                               error="School and name are required"), 400
    bus_id       = _slug(name) + "-" + _secrets.token_hex(2)
    driver_token = "driver-" + _secrets.token_urlsafe(12)
    stops = [
        {"id": "stop-1", "name": "First Stop", "lat": -1.4700, "lng": 36.9600, "offset_min": 0},
        {"id": "stop-2", "name": "School",     "lat": -1.4400, "lng": 37.0000, "offset_min": 30},
    ]
    db.add_bus_config(bus_id, school_id, name, plate, driver_token, stops)
    return redirect("/admin/buses")


@app.post("/admin/buses/<bus_id>/delete")
def admin_buses_delete(bus_id):
    r = _require_admin()
    if r: return r
    db.delete_bus_config(bus_id)
    return redirect("/admin/buses")


# ---- parents ----

@app.get("/admin/parents")
def admin_parents():
    r = _require_admin()
    if r: return r
    return render_template("admin_parents.html",
                           schools=db.list_schools(),
                           parents=db.list_parents(),
                           error=None)


@app.post("/admin/parents")
def admin_parents_post():
    r = _require_admin()
    if r: return r
    school_id = (request.form.get("school_id") or "").strip()
    name      = (request.form.get("name") or "").strip()
    phone     = (request.form.get("phone") or "").strip()
    if not (school_id and name and phone):
        return render_template("admin_parents.html",
                               schools=db.list_schools(),
                               parents=db.list_parents(),
                               error="School, name, and phone are required"), 400
    token = "parent-" + _secrets.token_hex(6)
    db.add_parent(school_id, token, name, phone)
    return redirect("/admin/parents")


@app.post("/admin/parents/<int:parent_id>/delete")
def admin_parents_delete(parent_id):
    r = _require_admin()
    if r: return r
    db.delete_parent(parent_id)
    return redirect("/admin/parents")


# ---- students ----

@app.get("/admin/students")
def admin_students():
    r = _require_admin()
    if r: return r
    return render_template("admin_students.html",
                           schools=db.list_schools(),
                           parents=db.list_parents(),
                           buses=db.list_bus_configs(),
                           students=db.list_students(),
                           error=None)


@app.post("/admin/students")
def admin_students_post():
    r = _require_admin()
    if r: return r
    school_id = (request.form.get("school_id") or "").strip()
    parent_id = request.form.get("parent_id")
    name      = (request.form.get("name") or "").strip()
    grade     = (request.form.get("grade") or "").strip()
    stop_id   = (request.form.get("stop_id") or "").strip()
    bus_id    = (request.form.get("bus_id") or "").strip()
    if not (school_id and parent_id and name):
        return render_template("admin_students.html",
                               schools=db.list_schools(),
                               parents=db.list_parents(),
                               buses=db.list_bus_configs(),
                               students=db.list_students(),
                               error="School, parent, and name are required"), 400
    db.add_student(school_id, int(parent_id), name, grade, stop_id, bus_id)
    return redirect("/admin/students")


@app.post("/admin/students/<int:student_id>/delete")
def admin_students_delete(student_id):
    r = _require_admin()
    if r: return r
    db.delete_student(student_id)
    return redirect("/admin/students")


@app.get("/login")
def login_page():
    if session.get("school") is True:
        return redirect("/school")
    return render_template("login.html", error=None)


@app.post("/login")
def login_post():
    key = (request.form.get("key") or "").strip()
    if key == SCHOOL_KEY:
        session["school"] = True
        return redirect("/school")
    return render_template("login.html", error="Invalid key"), 403


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.get("/school")
def school_page():
    if not _school_ok():
        return redirect("/login")
    return render_template("school.html", school_key=SCHOOL_KEY)


@app.get("/")
def index():
    return redirect("/school")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
