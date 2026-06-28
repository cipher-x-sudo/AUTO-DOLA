from pathlib import Path

import httpx
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


@pytest.mark.asyncio
async def test_browser_manager_vpn_request_returns_success_json(monkeypatch: pytest.MonkeyPatch) -> None:
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://manager/vpn/test-ip"
        return httpx.Response(200, json={"ok": True, "connected": True})

    def mock_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)

    result = await vpn.browser_manager_vpn_request("http://manager", "/vpn/test-ip")

    assert result == {"ok": True, "connected": True}


@pytest.mark.asyncio
async def test_browser_manager_vpn_request_raises_manager_error(monkeypatch: pytest.MonkeyPatch) -> None:
    original_client = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False, "error": "VPN_AUTH_FAILED"})

    def mock_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)

    with pytest.raises(ValueError, match="VPN_AUTH_FAILED"):
        await vpn.browser_manager_vpn_request("http://manager", "/vpn/test-ip")


@pytest.mark.asyncio
async def test_browser_manager_vpn_request_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed", request=request)

    def mock_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)

    with pytest.raises(ValueError, match="VPN_MANAGER_UNAVAILABLE"):
        await vpn.browser_manager_vpn_request("http://manager", "/vpn/test-ip")
