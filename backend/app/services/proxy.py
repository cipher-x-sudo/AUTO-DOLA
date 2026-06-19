from __future__ import annotations

import httpx


async def test_proxy(proxy_url: str) -> dict:
    if not proxy_url:
        return {"ok": False, "message": "Proxy URL is required."}
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=15) as client:
            response = await client.get("https://api.ipify.org?format=json")
            response.raise_for_status()
            return {"ok": True, "ip": response.json().get("ip"), "message": "Proxy is reachable."}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
