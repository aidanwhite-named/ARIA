"""작업별 이벤트 버스.

SSE 를 쓴다. 서버 → 클라이언트 단방향이면 충분하고, 취소는 별도 POST 로
처리하면 되므로 WebSocket 의 양방향성이 필요 없다. 재연결/버퍼링도 SSE 쪽이
단순하다.

늦게 붙은 구독자를 위해 작업별로 최근 이벤트를 메모리에 보관해 재생한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

_MAX_REPLAY = 2000


@dataclass
class Event:
    seq: int
    type: str
    payload: dict
    ts: str

    def to_dict(self) -> dict:
        return {"seq": self.seq, "type": self.type, "payload": self.payload, "ts": self.ts}


@dataclass
class JobStream:
    events: list[Event] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    closed: bool = False
    seq: int = 0


class EventBus:
    def __init__(self) -> None:
        self._streams: dict[str, JobStream] = {}
        self._lock = asyncio.Lock()

    def _stream(self, job_id: str) -> JobStream:
        stream = self._streams.get(job_id)
        if stream is None:
            stream = JobStream()
            self._streams[job_id] = stream
        return stream

    async def publish(self, job_id: str, event_type: str, payload: dict) -> Event:
        async with self._lock:
            stream = self._stream(job_id)
            stream.seq += 1
            event = Event(
                seq=stream.seq,
                type=event_type,
                payload=payload,
                ts=datetime.now(timezone.utc).isoformat(),
            )
            stream.events.append(event)
            if len(stream.events) > _MAX_REPLAY:
                del stream.events[: len(stream.events) - _MAX_REPLAY]
            subscribers = list(stream.subscribers)

        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    async def close(self, job_id: str) -> None:
        async with self._lock:
            stream = self._stream(job_id)
            stream.closed = True
            subscribers = list(stream.subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, job_id: str, after: int = 0) -> tuple[asyncio.Queue, list[Event]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        async with self._lock:
            stream = self._stream(job_id)
            stream.subscribers.add(queue)
            backlog = [e for e in stream.events if e.seq > after]
            closed = stream.closed
        if closed:
            queue.put_nowait(None)
        return queue, backlog

    async def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            stream = self._streams.get(job_id)
            if stream is not None:
                stream.subscribers.discard(queue)

    async def is_closed(self, job_id: str) -> bool:
        async with self._lock:
            stream = self._streams.get(job_id)
            return stream is None or stream.closed

    async def forget(self, job_id: str) -> None:
        async with self._lock:
            self._streams.pop(job_id, None)


BUS = EventBus()
