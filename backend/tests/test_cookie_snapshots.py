from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.main import app
from app.models import Job, JobKind, JobStatus
from app.services.cookie_snapshots import (
    append_raw_response_event,
    create_cookie_snapshot,
    dola_session_from_cookie_snapshot,
    latest_browser_snapshot_for_item,
    mark_cookie_snapshot_conversation,
    read_cookie_snapshot,
    update_cookie_snapshot,
)
from app.services.dola import DolaSession


def sample_dola_session() -> DolaSession:
    return DolaSession(
        url="https://www.dola.com/chat/completion?region=BD&fp=verify_test",
        headers={"cookie": "i18next=en; sid=secret-session; ttwid=fresh-public"},
        payload_template={},
        fp="verify_test",
        has_ttwid=True,
        has_hook_slardar=False,
        has_auth_cookies=True,
    )


def test_cookie_snapshot_encrypts_full_cookie_header_and_redacts_metadata(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        job = Job(kind=JobKind.video, status=JobStatus.running, title="Video batch (1)")
        session.add(job)
        session.commit()
        session.refresh(job)

        metadata = create_cookie_snapshot(session, job.id, job.id, 1, sample_dola_session(), base_dir=tmp_path)
        encrypted_text = Path(metadata["encrypted_file_path"]).read_text(encoding="utf-8")

        assert "sid=secret-session" not in encrypted_text
        assert "fresh-public" not in encrypted_text
        assert "sid=secret-session" not in str(metadata)
        assert metadata["cookie_names"] == ["i18next", "sid", "ttwid"]

        decrypted = read_cookie_snapshot(metadata)
        assert decrypted["cookie_header"] == "i18next=en; sid=secret-session; ttwid=fresh-public"
        assert decrypted["headers"]["cookie"] == "i18next=en; sid=secret-session; ttwid=fresh-public"


def test_cookie_snapshot_persists_conversation_and_raw_response_metadata(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        job = Job(kind=JobKind.video, status=JobStatus.running, title="Video batch (1)")
        session.add(job)
        session.commit()
        session.refresh(job)

        metadata = create_cookie_snapshot(session, job.id, job.id, 1, sample_dola_session(), base_dir=tmp_path)
        mark_cookie_snapshot_conversation(session, job.id, metadata["snapshot_id"], "1821544108913", 3)
        event = append_raw_response_event(
            session,
            job.id,
            metadata["snapshot_id"],
            "chain_poll",
            7,
            200,
            '{"code":0,"data":{"vid":"abc_123"}}',
        )

        session.expire_all()
        reloaded = session.get(Job, job.id)
        assert reloaded is not None
        stored = reloaded.dola_cookie_snapshots_json[0]

        assert stored["conversation_id_masked"] == "*44108913"
        assert stored["conversation_type"] == 3
        assert stored["raw_response_events"][0]["response_type"] == "chain_poll"
        assert stored["raw_response_events"][0]["body_sha256"] == event["body_sha256"]
        assert "body" not in stored["raw_response_events"][0]

        decrypted = read_cookie_snapshot(stored)
        assert decrypted["raw_response_events"][0]["body"] == '{"code":0,"data":{"vid":"abc_123"}}'
        assert decrypted["conversation_id"] == "1821544108913"


def test_browser_cookie_snapshot_can_reconstruct_dola_session(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        job = Job(kind=JobKind.video, status=JobStatus.running, title="Video batch (1)")
        session.add(job)
        session.commit()
        session.refresh(job)

        metadata = create_cookie_snapshot(
            session,
            job.id,
            job.id,
            1,
            sample_dola_session(),
            base_dir=tmp_path,
            source="browser",
            chat_url="https://www.dola.com/chat/1821544108913",
            extra_metadata={"submit_url": "https://www.dola.com/chat/completion?fp=verify_test"},
            extra_payload={"submit_url": "https://www.dola.com/chat/completion?fp=verify_test"},
        )
        mark_cookie_snapshot_conversation(session, job.id, metadata["snapshot_id"], "1821544108913", 3)

        latest = latest_browser_snapshot_for_item(session, job.id, job.id)
        assert latest is not None
        assert latest["source"] == "browser"
        assert latest["chat_url"] == "https://www.dola.com/chat/1821544108913"

        restored, payload = dola_session_from_cookie_snapshot(latest)
        assert restored.url == "https://www.dola.com/chat/completion?region=BD&fp=verify_test"
        assert restored.headers["cookie"] == "i18next=en; sid=secret-session; ttwid=fresh-public"
        assert payload["conversation_id"] == "1821544108913"


def test_update_cookie_snapshot_persists_browser_ready_metadata(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        job = Job(kind=JobKind.video, status=JobStatus.running, title="Video batch (1)")
        session.add(job)
        session.commit()
        session.refresh(job)

        metadata = create_cookie_snapshot(
            session,
            job.id,
            job.id,
            1,
            sample_dola_session(),
            base_dir=tmp_path,
            source="browser",
            chat_url="https://www.dola.com/chat/1821544108913",
        )
        update_cookie_snapshot(
            session,
            job.id,
            metadata["snapshot_id"],
            metadata_updates={
                "browser_ready_detected": True,
                "download_captured_from": "browser_ready_card",
                "vid": "video_abc",
                "has_download_url": True,
            },
            payload_updates={"download_url": "https://example.com/video.mp4"},
        )

        session.expire_all()
        reloaded = session.get(Job, job.id)
        assert reloaded is not None
        stored = reloaded.dola_cookie_snapshots_json[0]
        assert stored["browser_ready_detected"] is True
        assert stored["download_captured_from"] == "browser_ready_card"
        assert stored["vid"] == "video_abc"

        decrypted = read_cookie_snapshot(stored)
        assert decrypted["download_url"] == "https://example.com/video.mp4"


def test_admin_cookie_snapshot_endpoint_requires_token(tmp_path: Path) -> None:
    from app.database import engine, init_db

    init_db()

    with Session(engine) as session:
        job = Job(kind=JobKind.video, status=JobStatus.running, title="Video batch (1)")
        session.add(job)
        session.commit()
        session.refresh(job)
        metadata = create_cookie_snapshot(session, job.id, job.id, 1, sample_dola_session(), base_dir=tmp_path)
        job_id = job.id

    with TestClient(app) as client:
        denied = client.get(f"/api/video/jobs/{job_id}/dola-cookie-snapshots/{metadata['snapshot_id']}")
        assert denied.status_code == 403

        allowed = client.get(
            f"/api/video/jobs/{job_id}/dola-cookie-snapshots/{metadata['snapshot_id']}",
            headers={"X-Admin-Token": settings.admin_token},
        )

    assert allowed.status_code == 200
    body = allowed.json()
    assert body["snapshot"]["cookie_header"] == "i18next=en; sid=secret-session; ttwid=fresh-public"
    assert body["redacted"]["cookie_names"] == ["i18next", "sid", "ttwid"]
    assert "headers" not in body["redacted"]
    assert "cookie" in body["redacted"]["header_names"]
