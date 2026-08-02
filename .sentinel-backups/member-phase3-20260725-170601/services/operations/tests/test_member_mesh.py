from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sentinel_ops.main import app


REPO_ROOT = Path(__file__).resolve().parents[3]
FACE_FIXTURE = REPO_ROOT / "media" / "synthetic_face_fixture.png"


def test_three_demo_members_and_persistent_cameras(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "member-demo.db"))
    client = TestClient(app)

    members = client.get("/api/members")
    assert members.status_code == 200
    payload = members.json()
    assert [user["household"] for user in payload["users"]] == [
        "17 Sher Avenue",
        "18 Sher Avenue",
        "19 Sher Avenue",
    ]

    cameras = client.get("/api/member/USR-001/cameras")
    assert cameras.status_code == 200
    assert cameras.json()["count"] == 1
    assert cameras.json()["cameras"][0]["camera_id"] == "CAM-U1-01"
    assert cameras.json()["cameras"][0]["household"] == "17 Sher Avenue"


def test_offline_demo_geocoder(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "geocode-demo.db"))
    client = TestClient(app)

    response = client.get("/api/member/geocode", params={"q": "18 Sher Avenue"})
    assert response.status_code == 200
    data = response.json()
    assert data["suburb"] == "Lakefield"
    assert data["latitude"] == -26.19809
    assert data["longitude"] == 28.31042


def test_local_detector_repeat_match_and_incident_watch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "mesh-demo.db"))
    client = TestClient(app)
    image = FACE_FIXTURE.read_bytes()

    detected = client.post(
        "/api/member/face-detect",
        files={"image": ("face.png", image, "image/png")},
    )
    assert detected.status_code == 200
    assert detected.json()["faces"]
    assert "OpenCV" in detected.json()["detector"]

    first = client.post(
        "/api/member/face-sightings",
        files={"image": ("face.png", image, "image/png")},
        data={"user_id": "USR-001", "camera_id": "CAM-U1-01", "browser_confidence": "0.99"},
    )
    assert first.status_code == 200
    assert first.json()["classification"] == "NEW_VISITOR"

    watch = client.post(
        "/api/member/incidents/start",
        json={
            "sighting_id": first.json()["sighting_id"],
            "incident_type": "DEMO_INTRUSION",
            "confirmed_by_operator": True,
        },
    )
    assert watch.status_code == 200
    statuses = {item["household"]: item["status"] for item in watch.json()["incident"]["notifications"]}
    assert statuses["17 Sher Avenue"] == "ORIGIN_CONFIRMED"
    assert statuses["18 Sher Avenue"] == "WATCH_ACTIVE"
    assert statuses["19 Sher Avenue"] == "WATCH_ACTIVE"

    second = client.post(
        "/api/member/face-sightings",
        files={"image": ("face.png", image, "image/png")},
        data={"user_id": "USR-002", "camera_id": "CAM-U2-01", "browser_confidence": "0.99"},
    )
    assert second.status_code == 200
    assert second.json()["classification"] == "REPEAT_VISITOR_CANDIDATE"
    assert second.json()["incident_watch"] is not None

    mesh = client.get("/api/member/mesh-state")
    assert mesh.status_code == 200
    assert mesh.json()["camera_count"] == 3
    assert mesh.json()["trail_count"] == 1
    notifications = mesh.json()["active_incidents"][0]["notifications"]
    status_by_house = {item["household"]: item["status"] for item in notifications}
    assert status_by_house["18 Sher Avenue"] == "MATCH_CAPTURED"
