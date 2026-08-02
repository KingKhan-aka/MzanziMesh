from datetime import datetime, timezone

from sentinel_ops.demo import run_demo
from sentinel_ops.models import CameraEvent, Claim, Location, ReconstructRequest
from sentinel_ops.rewind import reconstruct_incident


def test_complete_demo_loop():
    result = run_demo()
    assert result["evidence"]["score"] >= 70
    assert result["alert"]["status"] == "PENDING_REVIEW"
    assert len(result["timeline"]["items"]) >= 2
    assert result["patrol"]["optimised"]["protected_risk_per_km"] > 0


def test_reconstruct_incident_accepts_mixed_timestamp_awareness():
    location = Location(latitude=-25.75, longitude=28.2)
    timeline = reconstruct_incident(
        ReconstructRequest(
            claim=Claim(
                claim_id="CLM-MIXED-TIME",
                incident_time=datetime(2026, 8, 2, 14, 0),
                location=location,
                claim_type="Theft",
            ),
            events=[
                CameraEvent(
                    event_id="EVT-AWARE",
                    camera_id="CAM-1",
                    timestamp=datetime(2026, 8, 2, 12, 10, tzinfo=timezone.utc),
                    location=location,
                )
            ],
            minutes_before=30,
            minutes_after=30,
        )
    )

    assert [item.event_id for item in timeline.items] == ["EVT-AWARE"]
