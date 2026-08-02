from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sentinel_ops.main import app


def test_three_demo_members_and_persistent_cameras(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "member-demo.db"))
    client = TestClient(app)

    members = client.get("/api/members")
    assert members.status_code == 200
    payload = members.json()
    assert [user["user_id"] for user in payload["users"]] == [
        "USR-001",
        "USR-002",
        "USR-003",
    ]

    cameras = client.get("/api/member/USR-001/cameras")
    assert cameras.status_code == 200
    assert cameras.json()["count"] == 1
    assert cameras.json()["cameras"][0]["camera_id"] == "CAM-U1-01"


def test_offline_demo_geocoder(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "geocode-demo.db"))
    client = TestClient(app)

    response = client.get("/api/member/geocode", params={"q": "14 Hillcrest Ave"})
    assert response.status_code == 200
    data = response.json()
    assert data["suburb"] == "Bryanston"
    assert data["latitude"] == -26.0514
    assert data["longitude"] == 28.0281
