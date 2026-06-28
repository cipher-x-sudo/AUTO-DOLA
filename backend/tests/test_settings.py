from sqlmodel import Session, SQLModel, create_engine

from app.services.settings import decrypt_value, encrypt_value, load_app_settings, load_public_settings, save_app_settings


def test_settings_encryption_round_trip() -> None:
    payload = {"dola_auth_cookies": "secret", "parallel": 5}
    token = encrypt_value(payload)
    assert token != str(payload)
    assert decrypt_value(token) == payload


def test_public_settings_default_to_hybrid_dola_mode(monkeypatch) -> None:
    class MissingSession:
        def get(self, *_args):
            return None

    settings = load_public_settings(MissingSession())

    assert settings["dola_mode"] == "hybrid"


def make_session() -> Session:
    from app.models import Setting

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_save_settings_makes_vpn_and_proxy_mutually_exclusive() -> None:
    with make_session() as session:
        saved = save_app_settings(session, {"proxy_enabled": True, "vpn_enabled": True, "vpn_password": "secret"})

        assert saved["vpn_enabled"] is True
        assert saved["proxy_enabled"] is False
        assert saved["vpn_password"] == ""
        assert saved["vpn_password_saved"] is True


def test_save_settings_direct_clears_proxy_and_vpn_but_keeps_password() -> None:
    with make_session() as session:
        save_app_settings(session, {"vpn_enabled": True, "vpn_password": "secret"})
        saved = save_app_settings(session, {"proxy_enabled": False, "vpn_enabled": False, "vpn_password": ""})
        private = load_app_settings(session, include_secrets=True)

        assert saved["proxy_enabled"] is False
        assert saved["vpn_enabled"] is False
        assert saved["vpn_password"] == ""
        assert saved["vpn_password_saved"] is True
        assert private["vpn_password"] == "secret"
