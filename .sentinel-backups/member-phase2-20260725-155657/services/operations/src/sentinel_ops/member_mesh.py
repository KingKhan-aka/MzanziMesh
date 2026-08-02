"""Member demo identities, persistent cameras and anonymous repeat-face sightings.

This module deliberately uses SQLite for the local hackathon demo. It gives the
three demo members stable state across restarts without making the demo depend on
AWS credentials or venue internet. The same records can later be mirrored to
DynamoDB behind the existing optional AWS layer.

Face matching stores anonymous SFace embeddings, not names. A match is returned as
"repeat visitor candidate" and is never treated as proof of identity or wrongdoing.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sentinel_ops.claims_bridge import load_claims_hotspots
from sentinel_ops.roles_api import _suburb_stats
from sentinel_ops.storage import connect

router = APIRouter(tags=["member mesh"])

OPERATIONS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = OPERATIONS_ROOT.parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"
FACE_MEDIA_ROOT = OPERATIONS_ROOT / "data" / "member_faces"
FACE_MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

FACE_MATCH_THRESHOLD = float(os.getenv("SENTINEL_FACE_MATCH_THRESHOLD", "0.72"))
MAX_FACE_UPLOAD = 5 * 1024 * 1024

DEMO_USERS = [
    {
        "user_id": "USR-001",
        "display_name": "User 1",
        "member_number": "DISC-1001",
        "household": "14 Hillcrest Ave",
        "suburb": "Bryanston",
        "metro": "Gauteng",
        "latitude": -26.0514,
        "longitude": 28.0281,
    },
    {
        "user_id": "USR-002",
        "display_name": "User 2",
        "member_number": "DISC-1002",
        "household": "8 Montrose Ave",
        "suburb": "Fourways",
        "metro": "Gauteng",
        "latitude": -26.0186,
        "longitude": 28.0104,
    },
    {
        "user_id": "USR-003",
        "display_name": "User 3",
        "member_number": "DISC-1003",
        "household": "17 Van Buuren Rd",
        "suburb": "Bedfordview",
        "metro": "Gauteng",
        "latitude": -26.1795,
        "longitude": 28.1345,
    },
]

LOCAL_GEOCODES = {
    "14 hillcrest ave": DEMO_USERS[0],
    "14 hillcrest avenue": DEMO_USERS[0],
    "bryanston": DEMO_USERS[0],
    "8 montrose ave": DEMO_USERS[1],
    "8 montrose avenue": DEMO_USERS[1],
    "fourways": DEMO_USERS[1],
    "17 van buuren rd": DEMO_USERS[2],
    "17 van buuren road": DEMO_USERS[2],
    "bedfordview": DEMO_USERS[2],
}


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def initialise_member_store() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS member_users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                member_number TEXT NOT NULL,
                household TEXT NOT NULL,
                suburb TEXT NOT NULL,
                metro TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS member_cameras (
                camera_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                household TEXT NOT NULL,
                suburb TEXT NOT NULL,
                metro TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                device_label TEXT NOT NULL,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                hotspot_id TEXT,
                geofence_risk REAL,
                registered_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES member_users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_member_cameras_user
                ON member_cameras(user_id);
            CREATE TABLE IF NOT EXISTS face_profiles (
                profile_id TEXT PRIMARY KEY,
                anonymous_label TEXT NOT NULL,
                embedding BLOB NOT NULL,
                embedding_size INTEGER NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                sighting_count INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS face_sightings (
                sighting_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                camera_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                similarity REAL NOT NULL,
                detection_confidence REAL,
                media_name TEXT,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                FOREIGN KEY(profile_id) REFERENCES face_profiles(profile_id),
                FOREIGN KEY(user_id) REFERENCES member_users(user_id),
                FOREIGN KEY(camera_id) REFERENCES member_cameras(camera_id)
            );
            CREATE INDEX IF NOT EXISTS idx_face_sightings_profile
                ON face_sightings(profile_id, captured_at);
            """
        )
        for user in DEMO_USERS:
            db.execute(
                """
                INSERT INTO member_users(
                    user_id, display_name, member_number, household, suburb, metro,
                    latitude, longitude, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (
                    user["user_id"], user["display_name"], user["member_number"],
                    user["household"], user["suburb"], user["metro"],
                    user["latitude"], user["longitude"], _now(),
                ),
            )
            camera_id = f"CAM-U{user['user_id'][-1]}-01"
            mode, hotspot_id, risk = _risk_context(user["metro"], user["suburb"])
            db.execute(
                """
                INSERT INTO member_cameras(
                    camera_id, user_id, household, suburb, metro, latitude, longitude,
                    device_label, status, mode, hotspot_id, geofence_risk, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(camera_id) DO NOTHING
                """,
                (
                    camera_id, user["user_id"], user["household"], user["suburb"],
                    user["metro"], user["latitude"], user["longitude"],
                    "Laptop / doorbell demo camera", "READY_FOR_LIVE_FEED", mode,
                    hotspot_id, risk, _now(),
                ),
            )


def _risk_context(metro: str, suburb: str) -> tuple[str, str | None, float | None]:
    try:
        hotspots, _ = load_claims_hotspots(metro)
        for hotspot in hotspots:
            if hotspot.name.strip().lower() == suburb.strip().lower():
                risk = float(
                    getattr(hotspot, "operational_priority", None)
                    or getattr(hotspot, "risk_score", 0)
                    or 0
                )
                return ("HEIGHTENED" if risk >= 60 else "NORMAL", hotspot.hotspot_id, round(risk, 1))
    except Exception:
        pass
    return "NORMAL", None, None


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _get_user(user_id: str) -> dict[str, Any]:
    initialise_member_store()
    with connect() as db:
        user = _row(db.execute("SELECT * FROM member_users WHERE user_id = ?", (user_id,)).fetchone())
    if not user:
        raise HTTPException(status_code=404, detail="member not found")
    return user


def _get_camera(camera_id: str, user_id: str | None = None) -> dict[str, Any]:
    initialise_member_store()
    query = "SELECT * FROM member_cameras WHERE camera_id = ?"
    args: tuple[Any, ...] = (camera_id,)
    if user_id:
        query += " AND user_id = ?"
        args = (camera_id, user_id)
    with connect() as db:
        camera = _row(db.execute(query, args).fetchone())
    if not camera:
        raise HTTPException(status_code=404, detail="camera not found for this member")
    return camera


class CameraRegistration(BaseModel):
    user_id: str
    household: str = Field(..., min_length=3)
    suburb: str = Field(..., min_length=2)
    metro: str = "Gauteng"
    latitude: float
    longitude: float
    device_label: str = "Doorbell camera"
    consent_acknowledged: bool


@router.get("/api/members")
def list_members():
    initialise_member_store()
    with connect() as db:
        users = [dict(row) for row in db.execute(
            "SELECT * FROM member_users ORDER BY user_id"
        ).fetchall()]
        for user in users:
            user["camera_count"] = db.execute(
                "SELECT COUNT(*) AS n FROM member_cameras WHERE user_id = ?",
                (user["user_id"],),
            ).fetchone()["n"]
    return {"count": len(users), "users": users, "demo_only": True}


@router.get("/api/member/geocode")
def geocode_address(q: str = Query(..., min_length=3)):
    cleaned = " ".join(q.strip().lower().split())
    for key, item in LOCAL_GEOCODES.items():
        if key in cleaned or cleaned in key:
            return {
                "query": q,
                "source": "demo address index",
                "household": item["household"],
                "suburb": item["suburb"],
                "metro": item["metro"],
                "latitude": item["latitude"],
                "longitude": item["longitude"],
                "display_name": f"{item['household']}, {item['suburb']}, South Africa",
            }

    # Internet-assisted fallback. The timeout is deliberately small so the local
    # demo never hangs when venue Wi-Fi is unavailable.
    params = urllib.parse.urlencode({
        "format": "jsonv2", "limit": 1, "countrycodes": "za",
        "addressdetails": 1, "q": q,
    })
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "SentinelMesh-GradHack-Demo/0.2"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as response:  # noqa: S310
            results = json.loads(response.read().decode("utf-8"))
        if results:
            result = results[0]
            address = result.get("address") or {}
            suburb = (
                address.get("suburb") or address.get("neighbourhood")
                or address.get("city_district") or address.get("town")
                or address.get("city") or ""
            )
            province = (address.get("state") or "").lower()
            metro = "Cape Town" if "western cape" in province else "Gauteng"
            return {
                "query": q, "source": "OpenStreetMap Nominatim",
                "household": q.strip(), "suburb": suburb, "metro": metro,
                "latitude": float(result["lat"]), "longitude": float(result["lon"]),
                "display_name": result.get("display_name", q),
            }
    except Exception:
        pass
    raise HTTPException(
        status_code=404,
        detail="Address could not be geocoded. For the offline demo use one of the three preloaded addresses.",
    )


@router.get("/api/member/{user_id}/cameras")
def member_cameras(user_id: str):
    user = _get_user(user_id)
    with connect() as db:
        cameras = [dict(row) for row in db.execute(
            "SELECT * FROM member_cameras WHERE user_id = ? ORDER BY registered_at",
            (user_id,),
        ).fetchall()]
    return {"user": user, "count": len(cameras), "cameras": cameras}


@router.post("/api/member/cameras/register")
def register_member_camera(body: CameraRegistration):
    _get_user(body.user_id)
    if not body.consent_acknowledged:
        raise HTTPException(status_code=422, detail="Household consent is required")
    if not (-35 < body.latitude < -22 and 16 < body.longitude < 33):
        raise HTTPException(status_code=422, detail="coordinates fall outside South Africa")
    initialise_member_store()
    with connect() as db:
        seq = db.execute(
            "SELECT COUNT(*) AS n FROM member_cameras WHERE user_id = ?", (body.user_id,)
        ).fetchone()["n"] + 1
        camera_id = f"CAM-U{body.user_id[-1]}-{seq:02d}"
        mode, hotspot_id, risk = _risk_context(body.metro, body.suburb)
        registered_at = _now()
        db.execute(
            """
            INSERT INTO member_cameras(
                camera_id, user_id, household, suburb, metro, latitude, longitude,
                device_label, status, mode, hotspot_id, geofence_risk, registered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                camera_id, body.user_id, body.household.strip(), body.suburb.strip().title(),
                body.metro, body.latitude, body.longitude, body.device_label.strip(),
                "READY_FOR_LIVE_FEED", mode, hotspot_id, risk, registered_at,
            ),
        )
        db.execute(
            """
            UPDATE member_users SET household=?, suburb=?, metro=?, latitude=?, longitude=?
            WHERE user_id=?
            """,
            (
                body.household.strip(), body.suburb.strip().title(), body.metro,
                body.latitude, body.longitude, body.user_id,
            ),
        )
    camera = _get_camera(camera_id, body.user_id)
    return {
        "camera": camera,
        "next_steps": [
            "Choose this camera in My Property and attach the laptop webcam.",
            "A stable face scan creates an anonymous sighting and compares it across all three demo homes.",
            "A match is a repeat-visitor candidate for human review, not proof of identity or wrongdoing.",
        ],
    }


@router.get("/api/member/{user_id}/summary")
def member_summary(user_id: str):
    user = _get_user(user_id)
    stats = _suburb_stats().get(user["suburb"].strip().title())
    cameras = member_cameras(user_id)["cameras"]
    base = {"user": user, "cameras": cameras, "known": bool(stats)}
    if not stats:
        return {**base, "message": "No claims history for this suburb in the supplied data."}
    return {
        **base,
        "incidents_5y": stats["count"],
        "peak_hours": stats["peak_hours"],
        "peak_days": stats["peak_days"],
        "common_perils": stats["perils"],
        "privacy_note": "This member sees only their property, their cameras and area-level patterns.",
    }


@lru_cache(maxsize=1)
def _face_system():
    try:
        from sentinel_camera_ai.config import AppConfig
        from sentinel_camera_ai.detectors.face import FaceSystem
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"face recognition package unavailable: {exc}") from exc
    config = AppConfig.load(CONFIG_PATH)
    system = FaceSystem(config)
    if not system.embedding_enabled:
        raise RuntimeError("OpenCV SFace model could not be loaded")
    return system


def _embedding_from_image(image: np.ndarray) -> tuple[np.ndarray, float, np.ndarray]:
    from sentinel_camera_ai.detection import Detection
    from sentinel_camera_ai.schemas import BoundingBox

    system = _face_system()
    detections = system.detect(image)
    if detections:
        detection = detections[0]
        crop = detection.crop(image, padding=0.16)
        confidence = float(detection.confidence)
    else:
        h, w = image.shape[:2]
        detection = Detection(
            kind="face",
            box=BoundingBox(x=0, y=0, width=w, height=h),
            confidence=0.5,
        )
        crop = image
        confidence = 0.5
    vector = system.embedding(image, detection)
    if vector is None:
        raise HTTPException(status_code=422, detail="No usable face embedding could be produced")
    return vector.astype(np.float32), confidence, crop


def _decode_embedding(blob: bytes, size: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=size).copy()


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-12:
        return 0.0
    cosine = float(np.dot(a, b) / denom)
    return max(0.0, min(1.0, (cosine + 1.0) / 2.0))


@router.post("/api/member/face-sightings")
async def create_face_sighting(
    image: UploadFile = File(...),
    user_id: str = Form(...),
    camera_id: str = Form(...),
    browser_confidence: float | None = Form(None),
):
    user = _get_user(user_id)
    camera = _get_camera(camera_id, user_id)
    raw = await image.read()
    if not raw or len(raw) > MAX_FACE_UPLOAD:
        raise HTTPException(status_code=413, detail="face frame is empty or too large")
    frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=415, detail="face frame is not a valid image")

    vector, server_confidence, crop = _embedding_from_image(frame)
    captured_at = _now()
    initialise_member_store()
    with connect() as db:
        profiles = db.execute(
            "SELECT profile_id, anonymous_label, embedding, embedding_size, sighting_count FROM face_profiles"
        ).fetchall()
        best = None
        for profile in profiles:
            candidate = _decode_embedding(profile["embedding"], profile["embedding_size"])
            score = _similarity(vector, candidate)
            if best is None or score > best["similarity"]:
                best = {"row": profile, "similarity": score, "embedding": candidate}

        matched = bool(best and best["similarity"] >= FACE_MATCH_THRESHOLD)
        if matched:
            profile_id = best["row"]["profile_id"]
            anonymous_label = best["row"]["anonymous_label"]
            prior_count = int(best["row"]["sighting_count"])
            merged = (best["embedding"] * prior_count + vector) / (prior_count + 1)
            norm = float(np.linalg.norm(merged))
            if norm > 1e-12:
                merged = merged / norm
            db.execute(
                """
                UPDATE face_profiles
                SET embedding=?, embedding_size=?, last_seen=?, sighting_count=sighting_count+1
                WHERE profile_id=?
                """,
                (merged.astype(np.float32).tobytes(), merged.size, captured_at, profile_id),
            )
            similarity = float(best["similarity"])
        else:
            profile_id = f"FACE-{uuid.uuid4().hex[:8].upper()}"
            sequence = len(profiles) + 1
            anonymous_label = f"Anonymous visitor {sequence:02d}"
            similarity = 1.0
            db.execute(
                """
                INSERT INTO face_profiles(
                    profile_id, anonymous_label, embedding, embedding_size,
                    first_seen, last_seen, sighting_count
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    profile_id, anonymous_label, vector.tobytes(), vector.size,
                    captured_at, captured_at,
                ),
            )

        sighting_id = f"SIGHT-{uuid.uuid4().hex[:10].upper()}"
        media_name = f"{sighting_id}.jpg"
        media_path = FACE_MEDIA_ROOT / media_name
        cv2.imwrite(str(media_path), crop)
        confidence = max(
            float(server_confidence),
            float(browser_confidence or 0.0),
        )
        db.execute(
            """
            INSERT INTO face_sightings(
                sighting_id, profile_id, user_id, camera_id, captured_at,
                similarity, detection_confidence, media_name, latitude, longitude
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sighting_id, profile_id, user_id, camera_id, captured_at,
                similarity, confidence, media_name, camera["latitude"], camera["longitude"],
            ),
        )
        rows = db.execute(
            """
            SELECT s.*, u.display_name, c.household, c.suburb
            FROM face_sightings s
            JOIN member_users u ON u.user_id=s.user_id
            JOIN member_cameras c ON c.camera_id=s.camera_id
            WHERE s.profile_id=? ORDER BY s.captured_at
            """,
            (profile_id,),
        ).fetchall()

    sightings = [
        {
            **dict(row),
            "media_url": f"/api/member/face-media/{row['media_name']}" if row["media_name"] else None,
        }
        for row in rows
    ]
    other_users = sorted({row["display_name"] for row in sightings if row["user_id"] != user_id})
    return {
        "sighting_id": sighting_id,
        "profile_id": profile_id,
        "anonymous_label": anonymous_label,
        "classification": "REPEAT_VISITOR_CANDIDATE" if matched else "NEW_VISITOR",
        "matched": matched,
        "similarity": round(similarity, 3),
        "threshold": FACE_MATCH_THRESHOLD,
        "sighting_count": len(sightings),
        "seen_at_other_properties": other_users,
        "current_user": user["display_name"],
        "camera": camera,
        "sightings": sightings,
        "notice": "Anonymous biometric candidate only; human review is required before any alert or action.",
    }


@router.get("/api/member/{user_id}/face-sightings")
def list_face_sightings(user_id: str, limit: int = Query(30, ge=1, le=200)):
    _get_user(user_id)
    initialise_member_store()
    with connect() as db:
        rows = db.execute(
            """
            SELECT s.*, p.anonymous_label, p.sighting_count, u.display_name,
                   c.household, c.suburb
            FROM face_sightings s
            JOIN face_profiles p ON p.profile_id=s.profile_id
            JOIN member_users u ON u.user_id=s.user_id
            JOIN member_cameras c ON c.camera_id=s.camera_id
            WHERE s.user_id=? OR p.profile_id IN (
                SELECT profile_id FROM face_sightings WHERE user_id=?
            )
            ORDER BY s.captured_at DESC LIMIT ?
            """,
            (user_id, user_id, limit),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["media_url"] = f"/api/member/face-media/{item['media_name']}" if item["media_name"] else None
        items.append(item)
    return {"user_id": user_id, "count": len(items), "sightings": items}


@router.get("/api/member/face-trails")
def face_trails():
    initialise_member_store()
    with connect() as db:
        profiles = db.execute(
            "SELECT * FROM face_profiles WHERE sighting_count >= 2 ORDER BY last_seen DESC"
        ).fetchall()
        trails = []
        for profile in profiles:
            rows = db.execute(
                """
                SELECT s.*, u.display_name, c.household, c.suburb
                FROM face_sightings s
                JOIN member_users u ON u.user_id=s.user_id
                JOIN member_cameras c ON c.camera_id=s.camera_id
                WHERE s.profile_id=? ORDER BY s.captured_at
                """,
                (profile["profile_id"],),
            ).fetchall()
            trails.append({
                "profile_id": profile["profile_id"],
                "anonymous_label": profile["anonymous_label"],
                "sighting_count": profile["sighting_count"],
                "first_seen": profile["first_seen"],
                "last_seen": profile["last_seen"],
                "points": [dict(row) for row in rows],
            })
    return {"count": len(trails), "trails": trails}


@router.get("/api/member/face-media/{media_name}", include_in_schema=False)
def face_media(media_name: str):
    target = (FACE_MEDIA_ROOT / Path(media_name).name).resolve()
    if not target.is_relative_to(FACE_MEDIA_ROOT.resolve()) or not target.is_file():
        raise HTTPException(status_code=404, detail="face media not found")
    return FileResponse(target)


@router.delete("/api/member/face-sightings")
def reset_face_sightings():
    initialise_member_store()
    with connect() as db:
        counts = {
            "sightings": db.execute("SELECT COUNT(*) AS n FROM face_sightings").fetchone()["n"],
            "profiles": db.execute("SELECT COUNT(*) AS n FROM face_profiles").fetchone()["n"],
        }
        db.execute("DELETE FROM face_sightings")
        db.execute("DELETE FROM face_profiles")
    for path in FACE_MEDIA_ROOT.glob("*.jpg"):
        path.unlink(missing_ok=True)
    return {"removed": counts}
