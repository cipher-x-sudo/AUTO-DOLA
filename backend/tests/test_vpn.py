from pathlib import Path

import pytest

from app.services import vpn


def test_safe_vpn_name_forces_ovpn_extension() -> None:
    assert vpn.safe_vpn_name("../Hong Kong VPN") == "Hong_Kong_VPN.ovpn"
    assert vpn.safe_vpn_name("hk.ovpn") == "hk.ovpn"


def test_choose_vpn_username_supports_lines_and_commas() -> None:
    selected = vpn.choose_vpn_username("user1\nuser2,user3")

    assert selected in {"user1", "user2", "user3"}


def test_choose_vpn_username_requires_candidates() -> None:
    with pytest.raises(ValueError, match="VPN_USERNAME_MISSING"):
        vpn.choose_vpn_username("")


def test_list_and_choose_vpn_configs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(vpn.settings, "vpn_dir", tmp_path)
    (tmp_path / "a.ovpn").write_text("client", encoding="utf-8")
    (tmp_path / "b.txt").write_text("ignored", encoding="utf-8")

    configs = vpn.list_vpn_configs()

    assert configs == [{"name": "a.ovpn", "size_bytes": 6}]
    assert vpn.choose_vpn_config("a.ovpn")["name"] == "a.ovpn"
