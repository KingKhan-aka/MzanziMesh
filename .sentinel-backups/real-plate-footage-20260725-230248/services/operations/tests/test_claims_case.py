from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from sentinel_ops.main import app
from sentinel_ops.member_mesh import initialise_member_store
from sentinel_ops.storage import connect


def test_claim_case_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "claims-case.db"))
    with TestClient(app) as client:
        queue = client.get("/api/fraud/cases/queue?limit=2")
        assert queue.status_code == 200
        source = queue.json()["claims"][0]["incident_id"]

        opened = client.post(f"/api/fraud/cases/open/{source}")
        assert opened.status_code == 200
        data = opened.json()
        case_id = data["case"]["case_id"]
        assert len(data["validations"]) == 10
        assert data["database"]["counts"]["claim_cases"] == 1

        updated = client.post(
            f"/api/fraud/cases/{case_id}/validations",
            json={
                "check_code": "POLICY_ACTIVE",
                "status": "VERIFIED",
                "value": "POL-DEMO",
                "note": "Checked against policy record",
            },
        )
        assert updated.status_code == 200
        policy = next(v for v in updated.json()["validations"] if v["check_code"] == "POLICY_ACTIVE")
        assert policy["status"] == "VERIFIED"

        agent = client.post(f"/api/fraud/cases/{case_id}/agent/run")
        assert agent.status_code == 200
        assert agent.json()["agent"]["status"] == "COMPLETED"
        assert agent.json()["tasks"]

        report = client.post(f"/api/fraud/cases/{case_id}/report/generate")
        assert report.status_code == 200
        assert report.json()["report"]["case_id"] == case_id
        assert report.json()["workspace"]["database"]["counts"]["claim_case_reports"] == 1


def test_latest_member_incident_becomes_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "member-to-claim.db"))
    initialise_member_store()
    now = datetime.now().astimezone().isoformat()
    with connect() as db:
        db.execute(
            """
            INSERT INTO face_profiles(
                profile_id, anonymous_label, embedding, embedding_size, first_seen,
                last_seen, sighting_count, system_status, review_required
            ) VALUES ('PROF-DEMO', 'Anonymous visitor demo', ?, 2, ?, ?, 1, 'UNKNOWN', 0)
            """,
            (b"12345678", now, now),
        )
        db.execute(
            """
            INSERT INTO face_sightings(
                sighting_id, profile_id, user_id, camera_id, captured_at, similarity,
                detection_confidence, latitude, longitude, review_status
            ) VALUES ('SIG-DEMO', 'PROF-DEMO', 'USR-001', 'CAM-U1-01', ?, 1.0, 0.99,
                      -26.198020, 28.310300, 'UNREVIEWED')
            """,
            (now,),
        )
        db.execute(
            """
            INSERT INTO member_incidents(
                incident_id, profile_id, origin_user_id, origin_camera_id,
                origin_sighting_id, incident_type, status, started_at, updated_at,
                duration_minutes, notes
            ) VALUES ('INC-DEMO', 'PROF-DEMO', 'USR-001', 'CAM-U1-01', 'SIG-DEMO',
                      'HOME_INVASION', 'ACTIVE', ?, ?, 30, 'Door forced')
            """,
            (now, now),
        )

    with TestClient(app) as client:
        response = client.post(
            "/api/fraud/cases/from-member/latest",
            json={"claim_amount": 250000, "claim_type": "Home Invasion", "item_type": "Contents"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["case"]["source_type"] == "MEMBER_INCIDENT"
        assert data["case"]["member_incident_id"] == "INC-DEMO"
        assert any(e["evidence_type"] == "MEMBER_INCIDENT" for e in data["evidence"])


def test_camera_inbox_auto_ingest_links_repeat_plate(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATABASE_PATH", str(tmp_path / "camera-inbox.db"))
    from sentinel_ops import claims_case as case_module

    def fake_process(
        case_id,
        source_path,
        *,
        original_name,
        camera_id,
        latitude,
        longitude,
        captured_at,
        source_upload_id=None,
        source_media_url=None,
        only_plate_events=False,
    ):
        observation_id = f"PLT-{camera_id}"
        event_id = f"EVT-{camera_id}"
        now = captured_at.isoformat()
        payload = {
            "event_id": event_id,
            "camera_id": camera_id,
            "timestamp": now,
            "source_media": original_name,
            "source_media_url": source_media_url,
            "source_upload_id": source_upload_id,
            "plate": {"text": "AB12CDGP", "ocr_confidence": 0.91, "detection_confidence": 0.82},
        }
        with connect() as db:
            db.execute(
                """
                INSERT INTO claim_plate_observations(
                    observation_id, case_id, event_id, plate_text, normalized_plate,
                    ocr_confidence, detection_confidence, camera_id, captured_at,
                    media_url, match_status, payload_json, created_at
                ) VALUES (?, ?, ?, 'AB12CDGP', 'AB12CDGP', .91, .82, ?, ?, NULL,
                          'CANDIDATE', ?, ?)
                """,
                (observation_id, case_id, event_id, camera_id, now, case_module._safe_json(payload), now),
            )
        return {
            "batch": camera_id,
            "event_count": 1,
            "observations": [{
                "observation_id": observation_id,
                "plate_text": "AB12CDGP",
                "normalized_plate": "AB12CDGP",
                "match_status": "CANDIDATE",
                "ocr_confidence": .91,
                "detection_confidence": .82,
                "camera_id": camera_id,
                "captured_at": now,
                "media_url": None,
            }],
        }

    monkeypatch.setattr(case_module, "_process_case_plate_path", fake_process)

    with TestClient(app) as client:
        source = client.get("/api/fraud/cases/queue?limit=1").json()["claims"][0]["incident_id"]
        case_id = client.post(f"/api/fraud/cases/open/{source}").json()["case"]["case_id"]
        response = client.post(f"/api/fraud/cases/{case_id}/camera-inbox/auto-ingest")
        assert response.status_code == 200
        data = response.json()
        assert len(data["workspace"]["camera_uploads"]) == 2
        assert all(item["status"] == "PROCESSED" for item in data["workspace"]["camera_uploads"])
        assert data["continuity"]["best_match"]["plate"] == "AB12CDGP"
        assert data["continuity"]["best_match"]["status"] == "CROSS_CAMERA_MATCH"
        assert {item["match_status"] for item in data["workspace"]["plates"]} == {"CROSS_CAMERA_MATCH"}
        validation = next(item for item in data["workspace"]["validations"] if item["check_code"] == "PLATE_MATCH")
        assert validation["status"] == "VERIFIED"
        assert validation["value"] == "AB12CDGP"
        assert data["workspace"]["database"]["counts"]["claim_camera_uploads"] == 2
