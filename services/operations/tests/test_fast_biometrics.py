from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from sentinel_ops.main import app
from sentinel_ops.member_mesh import _gallery_candidates, initialise_member_store
from sentinel_ops.storage import connect

REPO_ROOT = Path(__file__).resolve().parents[3]
FACE_FIXTURE = REPO_ROOT / "media" / "synthetic_face_fixture.png"


def test_vectorised_gallery_returns_runner_up_margin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "gallery.db"))
    initialise_member_store()
    first = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    second = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    with connect() as db:
        for profile_id, vector in (("FACE-A", first), ("FACE-B", second)):
            db.execute(
                """
                INSERT INTO face_profiles(
                    profile_id, anonymous_label, embedding, embedding_size,
                    first_seen, last_seen, sighting_count
                ) VALUES (?, ?, ?, ?, '2026-08-01T00:00:00+02:00',
                          '2026-08-01T00:00:00+02:00', 1)
                """,
                (profile_id, profile_id, vector.tobytes(), vector.size),
            )
        best, runner_up = _gallery_candidates(db, np.array([0.99, 0.01, 0.0, 0.0], dtype=np.float32))
    assert best is not None and best["row"]["profile_id"] == "FACE-A"
    assert runner_up is not None and runner_up["row"]["profile_id"] == "FACE-B"
    assert best["similarity"] - runner_up["similarity"] > 0.4


def test_tracked_batch_cross_camera_and_profile_poisoning_guard(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "fast-batch.db"))
    image = FACE_FIXTURE.read_bytes()
    client = TestClient(app)

    first = client.post(
        "/api/member/face-sightings/batch",
        files=[
            ("images", (f"track-1-{index}.png", image, "image/png"))
            for index in range(3)
        ],
        data={
            "user_id": "USR-001",
            "camera_id": "CAM-U1-01",
            "candidates_json": json.dumps([
                {
                    "track_id": "TRACK-1",
                    "sample_index": index,
                    "confidence": 0.99,
                    "quality": 92 - index,
                }
                for index in range(3)
            ]),
        },
    )
    assert first.status_code == 200
    first_result = first.json()["results"][0]
    assert first_result["classification"] == "NEW_VISITOR"
    assert first_result["samples_considered"] == 3
    assert first_result["signature_method"] == "QUALITY_WEIGHTED_MULTI_FRAME"
    profile_id = first_result["profile_id"]
    assert first_result["sighting"]["sighting_id"] == first_result["sighting_id"]
    recent = client.get("/api/member/USR-001/face-sightings")
    assert recent.status_code == 200
    assert recent.headers["cache-control"] == "no-store, max-age=0"
    assert recent.json()["sightings"][0]["profile_id"] == profile_id
    visitors = client.get("/api/member/visitors", params={"user_id": "USR-001"})
    assert visitors.status_code == 200
    assert visitors.json()["visitors"][0]["profile_id"] == profile_id
    with connect() as db:
        before = bytes(db.execute(
            "SELECT embedding FROM face_profiles WHERE profile_id=?", (profile_id,)
        ).fetchone()["embedding"])

    second = client.post(
        "/api/member/face-sightings/batch",
        files=[("images", ("track-2.png", image, "image/png"))],
        data={
            "user_id": "USR-002",
            "camera_id": "CAM-U2-01",
            "candidates_json": json.dumps([
                {"track_id": "TRACK-2", "confidence": 0.99, "quality": 91}
            ]),
        },
    )
    assert second.status_code == 200
    result = second.json()["results"][0]
    assert result["classification"] == "REPEAT_VISITOR_CANDIDATE"
    assert result["track_id"] == "TRACK-2"
    assert result["continuity"]["profile_updated_from_candidate"] is False
    assert result["match_margin"] >= result["margin_threshold"]
    with connect() as db:
        after = bytes(db.execute(
            "SELECT embedding FROM face_profiles WHERE profile_id=?", (profile_id,)
        ).fetchone()["embedding"])
    assert after == before


def test_confirmed_intruder_stays_red_across_household_sightings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "intruder-visible.db"))
    image = FACE_FIXTURE.read_bytes()
    client = TestClient(app)
    first = client.post(
        "/api/member/face-sightings/batch",
        files=[("images", ("track-1.png", image, "image/png"))],
        data={
            "user_id": "USR-001",
            "camera_id": "CAM-U1-01",
            "candidates_json": '[{"track_id":"TRACK-1","confidence":0.99,"quality":92}]',
        },
    ).json()["results"][0]
    classified = client.post(
        f"/api/member/visitors/{first['profile_id']}/classify",
        json={
            "user_id": "USR-001",
            "status": "CONFIRMED_INTRUDER",
            "notes": "Human-reviewed test incident",
            "updated_by": "Test operator",
            "start_incident_watch": False,
        },
    )
    assert classified.status_code == 200

    second = client.post(
        "/api/member/face-sightings/batch",
        files=[("images", ("track-2.png", image, "image/png"))],
        data={
            "user_id": "USR-002",
            "camera_id": "CAM-U2-01",
            "candidates_json": '[{"track_id":"TRACK-2","confidence":0.99,"quality":92}]',
        },
    )
    assert second.status_code == 200
    result = second.json()["results"][0]
    assert result["profile_status"] == "CONFIRMED_INTRUDER"
    assert result["viewer_classification"]["status"] == "UNKNOWN"

    recent = client.get("/api/member/USR-002/face-sightings").json()["sightings"]
    current = next(item for item in recent if item["sighting_id"] == result["sighting_id"])
    assert current["effective_status"] == "CONFIRMED_INTRUDER"


def test_dashboard_fast_batch_renders_recent_and_intruder_colours():
    dashboard = REPO_ROOT / "services" / "operations" / "static" / "dashboard.html"
    html = dashboard.read_text(encoding="utf-8")
    assert "memberUpsertRecentSighting(d)" in html
    assert "intruder-sighting" in html
    assert "RECOGNISED & SAVED" in html


def test_full_reset_is_idempotent_and_resets_metrics(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "full-reset.db"))
    client = TestClient(app)
    image = FACE_FIXTURE.read_bytes()
    client.post(
        "/api/member/face-sightings/batch",
        files=[("images", ("track.png", image, "image/png"))],
        data={
            "user_id": "USR-001",
            "camera_id": "CAM-U1-01",
            "candidates_json": '[{"track_id":"TRACK-1","confidence":0.99,"quality":90}]',
        },
    )
    first = client.delete("/api/demo/reset?full=true")
    second = client.delete("/api/demo/reset?full=true")
    assert first.status_code == second.status_code == 200
    assert first.json()["reset"] is True
    assert second.json()["removed"]["member_mesh"]["removed"]["face_sightings"] == 0
    assert second.json()["performance"]["metrics"] == {}
