import os

os.environ["ALLOW_CLIENT_TS"] = "1"

import pytest

import app as tracker

H = {"X-Driver-Token": "driver-demo-bus4"}
STOPS = tracker.CFG["buses"][0]["stops"]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    tracker.BUSES.clear()
    tracker.BUSES.update({b["id"]: tracker.new_bus(b) for b in tracker.CFG["buses"]})
    tracker.EVENTS.clear()
    sms = []
    monkeypatch.setattr(tracker, "send_sms", lambda phone, text: sms.append(text))
    return sms


@pytest.fixture
def client():
    return tracker.app.test_client()


def ping(client, lat, lng, ts, headers=H):
    return client.post(
        "/api/driver/ping",
        json={"bus_id": "bus4", "lat": lat, "lng": lng, "ts": ts},
        headers=headers,
    )


def test_rejects_wrong_token(client):
    assert ping(client, -1.47, 36.95, 1000, headers={"X-Driver-Token": "nope"}).status_code == 401


def test_rejects_bad_coordinates(client):
    assert ping(client, 999, 36.95, 1000).status_code == 400
    assert client.post(
        "/api/driver/ping",
        json={"bus_id": "bus4", "lat": "abc", "lng": 1},
        headers=H,
    ).status_code == 400


def test_eta_and_distance_shrink_as_bus_approaches(client):
    a = ping(client, -1.4745, 36.9570, 1000).get_json()
    b = ping(client, -1.4700, 36.9640, 1010).get_json()
    sa = next(s for s in a["stops"] if s["id"] == "greenpark")
    sb = next(s for s in b["stops"] if s["id"] == "greenpark")
    assert sb["km"] < sa["km"] and sb["eta_min"] < sa["eta_min"]


def test_arrival_marks_stop_passed_and_advances(client):
    ping(client, -1.4745, 36.9570, 1000)
    r = ping(client, -1.4660, 36.9700, 1060).get_json()
    assert [s["passed"] for s in r["stops"]] == [True, True, False, False, False]
    assert r["next_stop"] == "Greenpark Estate gate"


def test_sms_sent_once_per_threshold(client, fresh):
    for i, ts in enumerate(range(1000, 1600, 20)):
        f = i / 29
        ping(
            client,
            -1.4745 + (-1.4560 + 1.4745) * f,
            36.9570 + (36.9820 - 36.9570) * f,
            ts,
        )
    approaching = [m for m in fresh if "from Greenpark" in m]
    assert 1 <= len(approaching) <= 3
    assert len(approaching) == len(set(approaching))


def test_late_bus_flagged_delayed_and_parents_told_once(client, fresh):
    ping(client, -1.4745, 36.9570, 1000)
    r = ping(client, -1.4745, 36.9570, 1000 + 40 * 60).get_json()
    assert r["status"] == "delayed" and r["delay_min"] > 5
    ping(client, -1.4745, 36.9570, 1000 + 41 * 60)
    assert sum("late" in m for m in fresh) == 1


def test_finished_trip_refuses_pings_until_restarted(client):
    for s in STOPS:
        ping(client, s["lat"], s["lng"], 1000 + STOPS.index(s) * 60)
    assert tracker.BUSES["bus4"]["finished"]
    assert ping(client, -1.4, 37.0, 5000).status_code == 409
    assert client.post(
        "/api/driver/start", json={"bus_id": "bus4"}, headers=H
    ).status_code == 200
    assert ping(client, -1.4745, 36.9570, 6000).status_code == 200


def test_access_control(client):
    key = tracker.SCHOOL_KEY
    assert client.get("/api/fleet").status_code == 403
    assert client.get(f"/api/fleet?key={key}").status_code == 200
    assert client.get("/api/parent/wrong-token").status_code == 404
    assert client.get("/api/parent/parent-demo-amani").status_code == 200
    assert client.get("/parent/parent-demo-amani").status_code == 200
    assert client.get(f"/school?key={key}").status_code == 200
