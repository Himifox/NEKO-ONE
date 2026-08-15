"""Bounded fan-out for public-room WebSocket clients."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from uuid import uuid4

from fastapi import WebSocket


@dataclass(eq=False, slots=True)
class RoomConnection:
    id: str
    room_id: str
    visitor_id: str
    websocket: WebSocket
    queue: asyncio.Queue[str]
    writer_task: asyncio.Task[None] | None = None


class RoomConnectionHub:
    def __init__(self, *, per_connection_queue_size: int = 128):
        self._rooms: dict[str, dict[str, RoomConnection]] = {}
        self._lock = asyncio.Lock()
        self._queue_size = per_connection_queue_size

    async def register(
        self, websocket: WebSocket, *, room_id: str, visitor_id: str
    ) -> RoomConnection:
        connection = RoomConnection(
            id=f"conn_{uuid4().hex}",
            room_id=room_id,
            visitor_id=visitor_id,
            websocket=websocket,
            queue=asyncio.Queue(maxsize=self._queue_size),
        )
        connection.writer_task = asyncio.create_task(
            self._writer(connection), name=f"room-ws-writer:{connection.id}"
        )
        async with self._lock:
            self._rooms.setdefault(room_id, {})[connection.id] = connection
        return connection

    async def unregister(self, connection: RoomConnection) -> None:
        async with self._lock:
            room = self._rooms.get(connection.room_id)
            if room is not None:
                room.pop(connection.id, None)
                if not room:
                    self._rooms.pop(connection.room_id, None)
        writer = connection.writer_task
        if writer is not None and writer is not asyncio.current_task():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    async def send(self, connection: RoomConnection, event: dict) -> bool:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return await self._enqueue(connection, payload)

    async def broadcast(self, room_id: str, event: dict) -> None:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            connections = list(self._rooms.get(room_id, {}).values())
        overflowed: list[RoomConnection] = []
        for connection in connections:
            if not await self._enqueue(connection, payload):
                overflowed.append(connection)
        for connection in overflowed:
            await self._close_slow_connection(connection)

    async def _enqueue(self, connection: RoomConnection, payload: str) -> bool:
        try:
            connection.queue.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            return False

    async def _close_slow_connection(self, connection: RoomConnection) -> None:
        await self.unregister(connection)
        try:
            await connection.websocket.close(code=1013, reason="client_too_slow")
        except Exception:
            pass

    async def _writer(self, connection: RoomConnection) -> None:
        try:
            while True:
                payload = await connection.queue.get()
                try:
                    await asyncio.wait_for(
                        connection.websocket.send_text(payload), timeout=5.0
                    )
                finally:
                    connection.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.unregister(connection)

    async def online_count(self, room_id: str) -> int:
        async with self._lock:
            return len(self._rooms.get(room_id, {}))

    async def online_visitor_ids(self, room_id: str) -> set[str]:
        async with self._lock:
            return {
                connection.visitor_id
                for connection in self._rooms.get(room_id, {}).values()
            }

    async def disconnect_visitor(self, visitor_id: str) -> int:
        async with self._lock:
            connections = [
                connection
                for room in self._rooms.values()
                for connection in room.values()
                if connection.visitor_id == visitor_id
            ]
        for connection in connections:
            await self.unregister(connection)
            try:
                await connection.websocket.close(code=1008, reason="visitor_blocked")
            except Exception:
                pass
        return len(connections)

    async def shutdown(self) -> None:
        async with self._lock:
            connections = [
                connection
                for room in self._rooms.values()
                for connection in room.values()
            ]
            self._rooms.clear()
        for connection in connections:
            if connection.writer_task:
                connection.writer_task.cancel()
            try:
                await connection.websocket.close(code=1001, reason="server_shutdown")
            except Exception:
                pass
        await asyncio.gather(
            *(c.writer_task for c in connections if c.writer_task),
            return_exceptions=True,
        )
