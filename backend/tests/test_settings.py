from app.services.settings import decrypt_value, encrypt_value


def test_settings_encryption_round_trip() -> None:
    payload = {"dola_auth_cookies": "secret", "parallel": 5}
    token = encrypt_value(payload)
    assert token != str(payload)
    assert decrypt_value(token) == payload
