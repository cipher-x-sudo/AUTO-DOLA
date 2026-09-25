from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from app.services.cookie_profiles import (
    complete_profile_reservation,
    create_profile,
    parse_cookie_json,
    profile_metadata,
    release_profile_reservation,
    reserve_profile,
    snapshot_profiles,
)


def test_cookie_json_supports_browser_array_and_map_and_filters_entries() -> None:
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    parsed = parse_cookie_json(
        (
            '[{"name":"sid","value":"secret","domain":".dola.com"},'
            '{"name":"public","value":"yes","domain":"example.com"},'
            f'{{"name":"old","value":"gone","domain":"dola.com","expirationDate":{expired}}}]'
        ).encode()
    )
    assert parsed.cookie_header == "sid=secret"
    assert parsed.names == ["sid"]

    mapped = parse_cookie_json(b'{"sessionid":"abc","ttwid":"fresh"}')
    assert mapped.names == ["sessionid", "ttwid"]


def test_bulk_import_creates_upserts_and_collects_failures() -> None:
    from app.services.cookie_profiles import bulk_import_profiles, list_profiles

    db = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(db)
    with Session(db) as session:
        first = bulk_import_profiles(
            session,
            [
                {
                    "name": "61590000000001",
                    "daily_limit": 2,
                    "cookies": [{"name": "sessionid", "value": "one", "domain": ".dola.com"}],
                },
                {
                    "name": "61590000000002",
                    "daily_limit": 2,
                    "cookies": [{"name": "sessionid", "value": "two", "domain": ".dola.com"}],
                },
                {
                    "name": "bad",
                    "daily_limit": 2,
                    "cookies": [{"name": "x", "value": "y", "domain": "example.com"}],
                },
            ],
        )
        assert first.created == 2
        assert first.updated == 0
        assert len(first.failed or []) == 1
        assert (first.failed or [])[0]["name"] == "bad"

        second = bulk_import_profiles(
            session,
            [
                {
                    "name": "61590000000001",
                    "daily_limit": 2,
                    "cookies": [{"name": "sessionid", "value": "one-v2", "domain": ".dola.com"}],
                },
            ],
        )
        assert second.created == 0
        assert second.updated == 1
        profiles = list_profiles(session)
        assert {p["name"] for p in profiles} == {"61590000000001", "61590000000002"}
        renamed = next(p for p in profiles if p["name"] == "61590000000001")
        assert renamed["daily_limit"] == 2


def test_cookie_profile_snapshots_are_encrypted_and_usage_is_reserved_atomically() -> None:
    db = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(db)
    with Session(db) as session:
        profile = create_profile(
            session,
            "Account 1",
            2,
            parse_cookie_json(b'{"sid":"secret-session","ttwid":"fresh"}'),
        )
        assert "secret-session" not in profile.cookies_encrypted
        snapshots = snapshot_profiles(session, uuid4(), [profile.id])
        session.commit()

        first = reserve_profile(session, snapshots)
        second = reserve_profile(session, snapshots)
        third = reserve_profile(session, snapshots)
        assert first is not None and second is not None and third is None
        complete_profile_reservation(session, first)
        release_profile_reservation(session, second)
        metadata = profile_metadata(session, profile)
        assert metadata["completed_today"] == 1
        assert metadata["reserved_today"] == 0
        assert metadata["remaining_today"] == 1
