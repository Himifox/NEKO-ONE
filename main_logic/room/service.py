"""Authoritative single-writer runtime for the first public room."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from uuid import uuid4

from .avatar import split_emotion_tags
from .conversation import ConversationEngine
from .director import RoomDirector
from .hub import RoomConnectionHub
from .memory_facade import MemoryFacade
from .models import ActiveGeneration, RoomMessage, TurnCandidate, Visitor, utc_now
from .speech import SpeechService
from .store import RoomStore

logger = logging.getLogger(__name__)


class RoomInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PublicRoomService:
    def __init__(self, *, database_path: Path):
        self.store = RoomStore(database_path)
        self.hub = RoomConnectionHub()
        self.engine = ConversationEngine()
        self.memory = MemoryFacade()
        self.speech = SpeechService(database_path.parent / "speech")
        self.directors: dict[str, RoomDirector] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._proactive_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_turn_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_generations: dict[str, ActiveGeneration] = {}
        self._recent_submissions: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=32)
        )
        self._submission_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._last_room_activity: dict[str, float] = defaultdict(time.monotonic)
        self._last_proactive: dict[str, float] = defaultdict(lambda: 0.0)
        self._started = False
        self._shutdown_lock = asyncio.Lock()
        self._room_resumed = asyncio.Event()
        self._room_resumed.set()
        self.limits = {
            "max_message_chars": 2000,
            "messages_per_window": 5,
            "window_seconds": 10.0,
        }
        self.controls = {
            "paused": False,
            "read_only": False,
            "proactive_enabled": os.environ.get(
                "NEKO_PUBLIC_PROACTIVE_ENABLED", "0"
            )
            == "1",
        }

    async def start(self) -> None:
        if self._started:
            return
        await self.store.initialize()
        stored_limits = await self.store.get_setting("public_limits", {})
        if isinstance(stored_limits, dict):
            self._apply_limits(stored_limits)
        stored_controls = await self.store.get_setting("room_controls", {})
        if isinstance(stored_controls, dict):
            self._apply_controls(stored_controls)
        await self._ensure_room_runtime("main")
        self._started = True

    async def _ensure_room_runtime(self, room_id: str) -> None:
        if room_id in self._workers and not self._workers[room_id].done():
            return
        character_name, _prompt = await self.engine.character()
        director = RoomDirector(character_names=("NEKO", "猫娘", character_name))
        self.directors[room_id] = director
        self._workers[room_id] = asyncio.create_task(
            self._room_worker(room_id, director), name=f"public-room-worker:{room_id}"
        )
        await self._sync_proactive_task(room_id)

    async def submit_message(
        self,
        *,
        room_id: str,
        visitor: Visitor,
        request_id: str,
        text: str,
        reply_to_id: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_room_runtime(room_id)
        normalized_request_id = str(request_id or "").strip()
        if not normalized_request_id or len(normalized_request_id) > 96:
            raise RoomInputError("invalid_request_id", "request_id is required")
        normalized = str(text or "").replace("\x00", "").strip()
        if not normalized:
            raise RoomInputError("empty_message", "message is empty")
        if len(normalized) > self.limits["max_message_chars"]:
            raise RoomInputError(
                "message_too_long",
                f"message exceeds {self.limits['max_message_chars']} characters",
            )
        if visitor.status != "active":
            raise RoomInputError("visitor_blocked", "visitor is not allowed to send")
        async with self._submission_locks[visitor.id]:
            previous = await self.store.find_client_request(
                visitor.id, normalized_request_id
            )
            if previous is not None:
                message, event = previous
                duplicate = True
            else:
                self._ensure_room_accepting_messages()
                self._enforce_rate_limit(visitor.id)
                message, event, duplicate = await self.store.append_user_message(
                    room_id=room_id,
                    visitor=visitor,
                    request_id=normalized_request_id,
                    content=normalized,
                    reply_to_id=reply_to_id,
                )
        if not duplicate:
            self._last_room_activity[room_id] = time.monotonic()
            await self.hub.broadcast(room_id, event)
            candidate = TurnCandidate(message=message, enqueued_monotonic=time.monotonic())
            await self.directors[room_id].enqueue(candidate)
            await self._broadcast_queue(room_id)
        return {
            "type": "chat.accepted",
            "request_id": normalized_request_id,
            "server_time": utc_now(),
            "payload": {
                "message_id": message.id,
                "room_seq": message.room_seq,
                "duplicate": duplicate,
                "queue_size": await self.directors[room_id].size(),
            },
        }

    def _enforce_rate_limit(self, visitor_id: str) -> None:
        now = time.monotonic()
        recent = self._recent_submissions[visitor_id]
        while recent and now - recent[0] > self.limits["window_seconds"]:
            recent.popleft()
        if len(recent) >= self.limits["messages_per_window"]:
            raise RoomInputError("rate_limited", "too many messages")
        recent.append(now)

    def _apply_limits(self, values: dict[str, Any]) -> None:
        self.limits = {
            "max_message_chars": max(100, min(int(values.get("max_message_chars", self.limits["max_message_chars"])), 4000)),
            "messages_per_window": max(1, min(int(values.get("messages_per_window", self.limits["messages_per_window"])), 20)),
            "window_seconds": max(1.0, min(float(values.get("window_seconds", self.limits["window_seconds"])), 300.0)),
        }

    async def update_limits(self, values: dict[str, Any]) -> dict[str, Any]:
        self._apply_limits(values)
        await self.store.set_setting("public_limits", self.limits)
        self._recent_submissions.clear()
        return dict(self.limits)

    def _ensure_room_accepting_messages(self) -> None:
        if self.controls["paused"]:
            raise RoomInputError("room_paused", "room is paused by an administrator")
        if self.controls["read_only"]:
            raise RoomInputError(
                "room_read_only", "room is temporarily read-only"
            )

    def _apply_controls(self, values: dict[str, Any]) -> None:
        self.controls = {
            "paused": bool(values.get("paused", self.controls["paused"])),
            "read_only": bool(
                values.get("read_only", self.controls["read_only"])
            ),
            "proactive_enabled": bool(
                values.get(
                    "proactive_enabled", self.controls["proactive_enabled"]
                )
            ),
        }
        if self.controls["paused"]:
            self._room_resumed.clear()
        else:
            self._room_resumed.set()

    async def update_controls(
        self, room_id: str, values: dict[str, Any]
    ) -> dict[str, bool]:
        previous = dict(self.controls)
        self._apply_controls(values)
        await self.store.set_setting("room_controls", self.controls)
        await self._sync_proactive_task(room_id)
        if self.controls["paused"] and not previous["paused"]:
            await self.cancel_generation(room_id, reason="room_paused")
        event = await self.store.append_event(
            room_id,
            "room.control.updated",
            {"controls": dict(self.controls)},
        )
        await self.hub.broadcast(room_id, event)
        await self._broadcast_queue(room_id)
        return dict(self.controls)

    async def _sync_proactive_task(self, room_id: str) -> None:
        existing = self._proactive_tasks.get(room_id)
        if self.controls["proactive_enabled"]:
            if existing is None or existing.done():
                self._proactive_tasks[room_id] = asyncio.create_task(
                    self._proactive_loop(room_id),
                    name=f"public-room-proactive:{room_id}",
                )
            return
        if existing is not None:
            self._proactive_tasks.pop(room_id, None)
            existing.cancel()
            await asyncio.gather(existing, return_exceptions=True)

    async def cancel_generation(
        self, room_id: str, *, reason: str = "admin_cancelled"
    ) -> bool:
        task = self._active_turn_tasks.get(room_id)
        generation = self._active_generations.get(room_id)
        if (
            task is None
            or task.done()
            or generation is None
            or generation.phase not in {"preparing", "generating"}
        ):
            return False
        generation.cancel_reason = reason
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.store.audit(
            "room.generation.cancel",
            "room",
            room_id,
            {"generation_id": generation.id, "reason": reason},
        )
        await self._broadcast_queue(room_id)
        return True

    async def _room_worker(self, room_id: str, director: RoomDirector) -> None:
        while True:
            try:
                await self._room_resumed.wait()
                candidate, decision = await director.next_turn()
                await self._room_resumed.wait()
                turn_task = asyncio.create_task(
                    self._run_turn(room_id, candidate, decision),
                    name=f"public-room-turn:{room_id}",
                )
                self._active_turn_tasks[room_id] = turn_task
                try:
                    await turn_task
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                finally:
                    if self._active_turn_tasks.get(room_id) is turn_task:
                        self._active_turn_tasks.pop(room_id, None)
                await self._broadcast_queue(room_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("public-room worker recovered from an unexpected error")
                await asyncio.sleep(0.2)

    async def _proactive_loop(self, room_id: str) -> None:
        idle_seconds = max(
            30.0, float(os.environ.get("NEKO_PUBLIC_PROACTIVE_IDLE_SECONDS", "120"))
        )
        cooldown_seconds = max(
            idle_seconds,
            float(os.environ.get("NEKO_PUBLIC_PROACTIVE_COOLDOWN_SECONDS", "300")),
        )
        while True:
            await asyncio.sleep(min(15.0, idle_seconds / 2.0))
            now = time.monotonic()
            director = self.directors.get(room_id)
            if director is None or await self.hub.online_count(room_id) <= 0:
                continue
            if self.controls["paused"] or not self.controls["proactive_enabled"]:
                continue
            if room_id in self._active_generations or await director.size() > 0:
                continue
            if now - self._last_room_activity[room_id] < idle_seconds:
                continue
            if now - self._last_proactive[room_id] < cooldown_seconds:
                continue
            synthetic = RoomMessage(
                id=f"director_{uuid4().hex}",
                room_id=room_id,
                room_seq=0,
                author_type="director",
                author_id="room",
                display_name="房间",
                content="房间安静了一会儿。请用符合 Persona 的自然方式主持一下，可以延续最近话题或抛出一个轻量问题，但不要催促任何人。",
                created_at=utc_now(),
                metadata={"proactive": True, "reason_code": "dead_air"},
            )
            await director.enqueue(
                TurnCandidate(message=synthetic, enqueued_monotonic=now)
            )
            self._last_proactive[room_id] = now

    async def _run_turn(
        self,
        room_id: str,
        candidate: TurnCandidate,
        decision: dict[str, Any],
    ) -> None:
        turn_id = f"turn_{uuid4().hex}"
        generation = ActiveGeneration(
            id=f"gen_{uuid4().hex}",
            room_id=room_id,
            target_visitor_id=candidate.message.author_id,
            source_message_ids=[candidate.message.id],
        )
        self._active_generations[room_id] = generation
        is_proactive = bool(candidate.message.metadata.get("proactive"))
        reason_code = str(decision.get("reason_code") or "queued_message")
        await self.store.start_turn(
            turn_id=turn_id,
            room_id=room_id,
            target_visitor_id=None if is_proactive else candidate.message.author_id,
            source_message_ids=[candidate.message.id],
            reason_code=reason_code,
            decision=decision,
        )
        turn_event = await self.store.append_event(
            room_id,
            "turn.started",
            {
                "turn_id": turn_id,
                "generation_id": generation.id,
                "target_visitor_id": None if is_proactive else candidate.message.author_id,
                "source_message_ids": [candidate.message.id],
                "reason_code": reason_code,
            },
        )
        await self.hub.broadcast(room_id, turn_event)
        await self.hub.broadcast(
            room_id,
            {
                "type": "stream.started",
                "server_time": utc_now(),
                "payload": generation.snapshot(),
            },
        )

        try:
            if is_proactive:
                visitor = Visitor(id="room", display_name="房间里的大家")
            else:
                visitor = await self.store.get_visitor(candidate.message.author_id)
                if visitor is None:
                    raise RuntimeError("target visitor disappeared")
            recent_messages = await self.store.list_messages(room_id, limit=40)
            room_context = await self.memory.build_context(
                character_name=(await self.engine.character())[0],
                room_id=room_id,
                target_visitor=visitor,
                recent_messages=recent_messages,
                include_visitor_memory=not is_proactive,
            )

            async def on_delta(delta: str) -> None:
                generation.text += delta
                generation.chunk_index += 1
                await self.hub.broadcast(
                    room_id,
                    {
                        "type": "stream.delta",
                        "server_time": utc_now(),
                        "payload": {
                            "generation_id": generation.id,
                            "chunk_index": generation.chunk_index,
                            "delta": delta,
                        },
                    },
                )

            generation.phase = "generating"
            character_name, raw_final_text = await self.engine.generate(
                room_context=room_context,
                user_text=(
                    candidate.message.content
                    if is_proactive
                    else (
                        f"{visitor.display_name} 在公共房间对你说：\n"
                        f"{candidate.message.content}\n"
                        f"请直接回复 {visitor.display_name}。"
                    )
                ),
                on_delta=on_delta,
            )
            final_text, emotion = split_emotion_tags(raw_final_text)
            if not final_text:
                raise RuntimeError("model returned an empty response")
            # From this point forward, cancellation is cooperative-only. In
            # particular, never cancel the SQLite commit thread after it has
            # started and then mark the same turn as interrupted.
            generation.phase = "finalizing"
            await self.hub.broadcast(
                room_id,
                {
                    "type": "avatar.state",
                    "server_time": utc_now(),
                    "payload": {
                        "generation_id": generation.id,
                        "emotion": emotion,
                        "speaking": False,
                    },
                },
            )
            message, message_event = await self.store.append_assistant_message(
                room_id=room_id,
                character_id="neko",
                display_name=character_name,
                content=final_text,
                reply_to_id=None if is_proactive else candidate.message.id,
                metadata={
                    "turn_id": turn_id,
                    "generation_id": generation.id,
                    "target_visitor_id": None if is_proactive else visitor.id,
                    "emotion": emotion,
                    "memory_scope": self.memory.scope_metadata(visitor),
                },
            )
            await self.hub.broadcast(room_id, message_event)
            await self.hub.broadcast(
                room_id,
                {
                    "type": "stream.completed",
                    "server_time": utc_now(),
                    "payload": {
                        "generation_id": generation.id,
                        "message_id": message.id,
                        "room_seq": message.room_seq,
                    },
                },
            )
            await self.store.finish_turn(turn_id, status="completed")
            generation.phase = "completed"
            self._last_room_activity[room_id] = time.monotonic()
            self._spawn_background_task(
                self._publish_speech(
                    room_id=room_id,
                    message_id=message.id,
                    text=final_text,
                ),
                name="public-room-speech",
            )
            if not is_proactive:
                self._spawn_background_task(
                    self._record_completed_turn(
                        character_name=character_name,
                        room_id=room_id,
                        visitor=visitor,
                        user_message=candidate.message,
                        assistant_text=final_text,
                    ),
                    name="public-room-memory-write",
                )
        except asyncio.CancelledError:
            generation.phase = "interrupted"
            reason = generation.cancel_reason or "cancelled"
            await self.store.finish_turn(
                turn_id, status="interrupted", error_code=reason
            )
            interrupted_event = await self.store.append_event(
                room_id,
                "turn.interrupted",
                {
                    "turn_id": turn_id,
                    "generation_id": generation.id,
                    "reason": reason,
                },
            )
            await self.hub.broadcast(room_id, interrupted_event)
            await self.hub.broadcast(
                room_id,
                {
                    "type": "stream.failed",
                    "server_time": utc_now(),
                    "payload": {
                        "generation_id": generation.id,
                        "code": reason,
                    },
                },
            )
            raise
        except Exception as exc:
            generation.phase = "failed"
            logger.exception("public-room generation failed")
            await self.store.finish_turn(
                turn_id, status="failed", error_code=type(exc).__name__
            )
            interrupted_event = await self.store.append_event(
                room_id,
                "turn.interrupted",
                {"turn_id": turn_id, "generation_id": generation.id, "reason": "generation_failed"},
            )
            await self.hub.broadcast(room_id, interrupted_event)
            await self.hub.broadcast(
                room_id,
                {
                    "type": "stream.failed",
                    "server_time": utc_now(),
                    "payload": {
                        "generation_id": generation.id,
                        "code": "generation_failed",
                    },
                },
            )
        finally:
            self._active_generations.pop(room_id, None)

    def _spawn_background_task(self, coroutine, *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _publish_speech(
        self, *, room_id: str, message_id: str, text: str
    ) -> None:
        try:
            payload = await self.speech.synthesize(text)
            payload["message_id"] = message_id
            await self.hub.broadcast(
                room_id,
                {
                    "type": "speech.ready",
                    "server_time": utc_now(),
                    "payload": payload,
                },
            )
        except Exception:
            logger.exception("public-room shared speech generation failed")
            await self.hub.broadcast(
                room_id,
                {
                    "type": "speech.failed",
                    "server_time": utc_now(),
                    "payload": {"message_id": message_id},
                },
            )

    async def _record_completed_turn(
        self,
        *,
        character_name: str,
        room_id: str,
        visitor: Visitor,
        user_message,
        assistant_text: str,
    ) -> None:
        try:
            await self.memory.record_interaction(
                character_name=character_name,
                room_id=room_id,
                visitor=visitor,
                user_message=user_message,
                assistant_text=assistant_text,
            )
            await self.memory.record_mentions(
                character_name=character_name,
                room_id=room_id,
                visitor_id=visitor.id,
                response_text=assistant_text,
            )
        except Exception:
            # Memory is an asynchronous side effect. The committed public reply
            # stays authoritative and a memory outage cannot roll it back.
            logger.exception("public-room scoped memory write failed")

    async def _broadcast_queue(self, room_id: str) -> None:
        director = self.directors.get(room_id)
        await self.hub.broadcast(
            room_id,
            {
                "type": "queue.updated",
                "server_time": utc_now(),
                "payload": {
                    "waiting": await director.size() if director else 0,
                    "generating": room_id in self._active_generations,
                },
            },
        )

    def active_generation(self, room_id: str) -> ActiveGeneration | None:
        return self._active_generations.get(room_id)

    async def presence_event(self, room_id: str) -> dict[str, Any]:
        return {
            "type": "presence.updated",
            "server_time": utc_now(),
            "payload": {"online": await self.hub.online_count(room_id)},
        }

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            for director in self.directors.values():
                await director.close()
            for worker in self._workers.values():
                worker.cancel()
            for task in self._active_turn_tasks.values():
                task.cancel()
            for task in self._proactive_tasks.values():
                task.cancel()
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
            await asyncio.gather(
                *self._active_turn_tasks.values(), return_exceptions=True
            )
            await asyncio.gather(*self._proactive_tasks.values(), return_exceptions=True)
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            await self.speech.shutdown()
            await self.hub.shutdown()
            self._workers.clear()
            self._active_turn_tasks.clear()
            self._proactive_tasks.clear()
            self.directors.clear()
            self._started = False
