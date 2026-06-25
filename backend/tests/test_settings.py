from app.services.settings import decrypt_value, encrypt_value, load_public_settings


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
