from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from uuid import UUID


class EventHub:
    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)

    async def subscribe(self, channel: str):
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self._queues[channel].add(queue)
        try:
            yield queue
        finally:
            self._queues[channel].discard(queue)

    async def publish(self, channel: str, payload: dict) -> None:
        text = json.dumps(payload, default=str)
        for queue in list(self._queues[channel]):
            if queue.full():
                queue.get_nowait()
            await queue.put(text)


hub = EventHub()


async def publish_log(payload: dict) -> None:
    await hub.publish("system", payload)
    if payload.get("job_id"):
        await hub.publish(job_channel(payload["job_id"]), payload)


def job_channel(job_id: UUID | str) -> str:
    return f"job:{job_id}"
