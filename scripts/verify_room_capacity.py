"""Deterministic multi-visitor capacity and soak verification.

This exercises the production RoomConnectionHub, RoomDirector, single-writer
PostgreSQL path and PublicRoomService task lifecycle while replacing paid external
providers with bounded local fakes. It is not a substitute for the separate
real-provider acceptance run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verification_postgres import (
    PUBLIC_TABLES,
    connect,
    database_url,
    reset_public_tables,
    scalar,
)


@dataclass(slots=True)
class SinkStats:
    visitor_id: str
    persisted_events: int = 0
    last_room_seq: int = 0
    ordering_errors: int = 0
    max_active_generations: int = 0
    closed_code: int | None = None
    closed_reason: str | None = None


class SinkWebSocket:
    def __init__(self, visitor_id: str, *, delay_seconds: float = 0.0):
        self.stats = SinkStats(visitor_id=visitor_id)
        self.delay_seconds = delay_seconds
        self.event_types: Counter[str] = Counter()
        self._active_generations: set[str] = set()

    async def send_text(self, raw: str) -> None:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        event = json.loads(raw)
        event_type = str(event.get("type") or "unknown")
        self.event_types[event_type] += 1
        room_seq = event.get("room_seq")
        if isinstance(room_seq, int):
            expected = self.stats.last_room_seq + 1
            if room_seq != expected:
                self.stats.ordering_errors += 1
            self.stats.last_room_seq = room_seq
            self.stats.persisted_events += 1
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        generation_id = str(payload.get("generation_id") or "")
        if event_type == "stream.started" and generation_id:
            self._active_generations.add(generation_id)
            self.stats.max_active_generations = max(
                self.stats.max_active_generations,
                len(self._active_generations),
            )
        elif event_type in {"stream.completed", "stream.failed"} and generation_id:
            self._active_generations.discard(generation_id)

    async def close(self, *, code: int, reason: str) -> None:
        self.stats.closed_code = code
        self.stats.closed_reason = reason


@dataclass(slots=True)
class ProfileResult:
    visitors: int
    submitted_messages: int
    elapsed_seconds: float
    drain_seconds: float
    submit_p95_ms: float
    max_director_depth: int
    max_generation_concurrency: int
    room_last_seq: int
    postgres_messages: int
    postgres_events: int
    postgres_turns: int
    peak_python_mib: float
    python_end_mib: float
    python_growth_mib: float
    max_runtime_tasks: int
    database_mib: float
    wal_mib: float
    slow_client_isolated: bool
    writer_tasks_after_disconnect: int
    progress_samples: list[dict[str, Any]]


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile_value) - 1))
    return ordered[index]


def relevant_tasks() -> list[asyncio.Task[Any]]:
    prefixes = (
        "public-room-",
        "room-ws-writer:",
    )
    return [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith(prefixes)
    ]


async def wait_for_room_drain(service, expected_messages: int, timeout: float) -> tuple[float, int]:
    started = time.monotonic()
    max_depth = 0
    while time.monotonic() - started < timeout:
        director = service.directors["main"]
        depth = await director.size()
        max_depth = max(max_depth, depth)
        snapshot = await service.store.admin_snapshot(limit=1)
        active_turns = [
            task for task in service._active_turn_tasks.values() if not task.done()
        ]
        if (
            depth == 0
            and not active_turns
            and service.active_generation("main") is None
            and snapshot["totals"]["messages"] == expected_messages * 2
        ):
            if service._background_tasks:
                await asyncio.gather(
                    *tuple(service._background_tasks), return_exceptions=True
                )
            return time.monotonic() - started, max_depth
        await asyncio.sleep(0.01)
    raise TimeoutError(
        f"room did not drain: expected_messages={expected_messages}, "
        f"director={await service.directors['main'].size()}, "
        f"active={service.active_generation('main') is not None}"
    )


async def run_profile(args: argparse.Namespace, visitors_count: int) -> ProfileResult:
    from main_logic.room.service import PublicRoomService

    reset_public_tables()
    workspace_var = ROOT / "var"
    workspace_var.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"capacity-{visitors_count}-", dir=workspace_var
    ) as temporary:
        data_dir = Path(temporary)
        service = PublicRoomService(database_url=database_url(), data_dir=data_dir)
        service.controls["proactive_enabled"] = False
        service.speech._disabled = False

        generation_active = 0
        generation_max = 0

        async def fake_character() -> tuple[str, str]:
            return "NEKO", "capacity verification persona"

        async def fake_context(**_kwargs) -> str:
            service.memory.context_degraded = False
            service.memory.context_error_code = None
            return "[公共房间规则]\n容量验证隔离上下文"

        async def fake_generate(*, room_context, user_text, on_delta):
            nonlocal generation_active, generation_max
            assert "公共房间" in room_context
            assert "capacity" in user_text
            generation_active += 1
            generation_max = max(generation_max, generation_active)
            try:
                await on_delta("容量")
                await asyncio.sleep(args.model_delay_ms / 1000.0)
                await on_delta("正常")
                return "NEKO", "容量正常"
            finally:
                generation_active -= 1

        async def fake_memory_write(**_kwargs) -> None:
            return None

        async def fake_speech(_text: str) -> dict[str, Any]:
            return {
                "speech_id": "capacity",
                "url": "/speech-assets/capacity.wav",
                "content_type": "audio/wav",
                "sample_rate": 48000,
                "provider": "capacity-fake",
            }

        service.engine.character = fake_character
        service.engine.generate = fake_generate
        service.memory.build_context = fake_context
        service.memory.record_interaction = fake_memory_write
        service.memory.record_mentions = fake_memory_write
        service.speech.synthesize = fake_speech

        await service.start()
        wal_start = scalar("SELECT pg_current_wal_lsn()::text AS lsn")
        await service.update_limits(
            {
                "max_message_chars": 2000,
                "messages_per_window": 20,
                "window_seconds": 1,
            }
        )
        baseline_tasks = len(relevant_tasks())
        visitors = [
            await service.store.create_visitor(f"Capacity {index + 1}")
            for index in range(visitors_count)
        ]
        sockets: list[SinkWebSocket] = []
        connections = []
        for visitor in visitors:
            socket = SinkWebSocket(visitor.id)
            connection = await service.hub.register(
                socket, room_id="main", visitor_id=visitor.id
            )
            assert await service.hub.activate(connection)
            sockets.append(socket)
            connections.append(connection)
        assert await service.hub.online_count("main") == visitors_count

        slow_socket = SinkWebSocket("vis_capacity_slow", delay_seconds=10.0)
        slow_connection = await service.hub.register(
            slow_socket, room_id="main", visitor_id=slow_socket.stats.visitor_id
        )
        assert await service.hub.activate(slow_connection)
        for probe in range(180):
            await service.hub.broadcast(
                "main", {"type": "capacity.probe", "payload": {"index": probe}}
            )
            if probe % 5 == 0:
                await asyncio.sleep(0)
            if slow_socket.stats.closed_code is not None:
                break
        assert slow_socket.stats.closed_code == 1013
        assert slow_socket.stats.closed_reason == "client_too_slow"
        assert await service.hub.online_count("main") == visitors_count

        tracemalloc.start()
        started = time.monotonic()
        submit_latencies: list[float] = []
        submitted_by_visitor: Counter[str] = Counter()
        message_number = 0
        monitor_stop = asyncio.Event()
        monitor_stats: dict[str, int | None] = {
            "first_memory": None,
            "max_depth": 0,
            "max_tasks": 0,
        }
        progress_samples: list[dict[str, Any]] = []

        async def monitor_runtime() -> None:
            last_progress = started
            sample_interval = 0.25 if args.duration_seconds == 0 else 1.0
            while not monitor_stop.is_set():
                current_memory, _peak_memory = tracemalloc.get_traced_memory()
                if monitor_stats["first_memory"] is None:
                    monitor_stats["first_memory"] = current_memory
                director_depth = await service.directors["main"].size()
                monitor_stats["max_depth"] = max(
                    int(monitor_stats["max_depth"] or 0), director_depth
                )
                monitor_stats["max_tasks"] = max(
                    int(monitor_stats["max_tasks"] or 0), len(relevant_tasks())
                )
                now = time.monotonic()
                if (
                    args.duration_seconds > 0
                    and now - last_progress >= args.progress_seconds
                ):
                    sample = {
                        "elapsed_seconds": round(now - started, 1),
                        "submitted_messages": message_number,
                        "director_depth": director_depth,
                        "active_generation": service.active_generation("main")
                        is not None,
                        "python_mib": round(current_memory / (1024 * 1024), 3),
                        "runtime_tasks": len(relevant_tasks()),
                    }
                    progress_samples.append(sample)
                    print(
                        json.dumps(
                            {
                                "type": "capacity.progress",
                                "visitors": visitors_count,
                                **sample,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    last_progress = now
                await asyncio.sleep(sample_interval)

        monitor_task = asyncio.create_task(
            monitor_runtime(), name=f"capacity-monitor:{visitors_count}"
        )

        async def submit(visitor) -> None:
            nonlocal message_number
            message_number += 1
            request_number = message_number
            before = time.monotonic()
            accepted = await service.submit_message(
                room_id="main",
                visitor=visitor,
                request_id=f"capacity-{visitors_count}-{request_number}",
                text=f"capacity message {request_number}",
            )
            submit_latencies.append((time.monotonic() - before) * 1000.0)
            assert accepted["type"] == "chat.accepted"
            assert accepted["payload"]["duplicate"] is False
            submitted_by_visitor[visitor.id] += 1

        if args.duration_seconds > 0:
            interval = 1.0 / args.messages_per_second
            deadline = time.monotonic() + args.duration_seconds
            next_submission = time.monotonic()
            visitor_index = 0
            while time.monotonic() < deadline:
                await submit(visitors[visitor_index % visitors_count])
                visitor_index += 1
                next_submission += interval
                remaining = min(
                    deadline - time.monotonic(),
                    next_submission - time.monotonic(),
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
        else:
            for _round in range(args.messages_per_visitor):
                await asyncio.gather(*(submit(visitor) for visitor in visitors))

        submitted = sum(submitted_by_visitor.values())
        drain_seconds, max_depth = await wait_for_room_drain(
            service,
            submitted,
            timeout=max(30.0, submitted * max(0.02, args.model_delay_ms / 500.0)),
        )
        for connection in connections:
            await asyncio.wait_for(connection.queue.join(), timeout=10.0)

        monitor_stop.set()
        await monitor_task
        current_memory, peak_memory = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        elapsed = time.monotonic() - started
        first_memory = int(monitor_stats["first_memory"] or 0)

        room = await service.store.room_snapshot("main")
        expected_last_seq = submitted * 3
        assert room["last_seq"] == expected_last_seq
        assert generation_max == 1
        for socket in sockets:
            assert socket.stats.closed_code is None
            assert socket.stats.ordering_errors == 0
            assert socket.stats.last_room_seq == expected_last_seq
            assert socket.stats.persisted_events == expected_last_seq
            assert socket.stats.max_active_generations == 1

        with connect() as connection:
            postgres_messages = connection.execute(
                "SELECT COUNT(*) AS count FROM messages"
            ).fetchone()["count"]
            postgres_events = connection.execute(
                "SELECT COUNT(*) AS count FROM room_events"
            ).fetchone()["count"]
            postgres_turns = connection.execute(
                "SELECT COUNT(*) AS count FROM turns"
            ).fetchone()["count"]
            distinct_sequences = connection.execute(
                "SELECT COUNT(DISTINCT room_seq) AS count FROM room_events"
            ).fetchone()["count"]
            running_turns = connection.execute(
                "SELECT COUNT(*) AS count FROM turns WHERE status != 'completed'"
            ).fetchone()["count"]
            reply_rows = connection.execute(
                "SELECT metadata_json FROM messages WHERE author_type = 'neko'"
            ).fetchall()
        assert postgres_messages == submitted * 2
        assert postgres_events == expected_last_seq
        assert postgres_turns == submitted
        assert distinct_sequences == expected_last_seq
        assert running_turns == 0
        replied_by_visitor: Counter[str] = Counter()
        for row in reply_rows:
            metadata = row["metadata_json"]
            replied_by_visitor[str(metadata["target_visitor_id"])] += 1
        assert replied_by_visitor == submitted_by_visitor

        for room_connection in connections:
            await service.hub.unregister(room_connection)
        await asyncio.sleep(0)
        writer_tasks_after_disconnect = len(
            [task for task in relevant_tasks() if task.get_name().startswith("room-ws-writer:")]
        )
        assert writer_tasks_after_disconnect == 0
        assert await service.hub.online_count("main") == 0
        assert len(relevant_tasks()) == baseline_tasks

        database_bytes = scalar(
            """
            SELECT COALESCE(
                SUM(pg_total_relation_size(format('%%I.%%I', schemaname, tablename)::regclass)),
                0
            )
            FROM pg_tables
            WHERE schemaname = current_schema() AND tablename = ANY(%s)
            """,
            (list(PUBLIC_TABLES),),
        )
        wal_bytes = scalar(
            "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s::pg_lsn)",
            (wal_start,),
        )
        result = ProfileResult(
            visitors=visitors_count,
            submitted_messages=submitted,
            elapsed_seconds=round(elapsed, 3),
            drain_seconds=round(drain_seconds, 3),
            submit_p95_ms=round(percentile(submit_latencies, 0.95), 3),
            max_director_depth=max(
                max_depth, int(monitor_stats["max_depth"] or 0)
            ),
            max_generation_concurrency=generation_max,
            room_last_seq=room["last_seq"],
            postgres_messages=postgres_messages,
            postgres_events=postgres_events,
            postgres_turns=postgres_turns,
            peak_python_mib=round(peak_memory / (1024 * 1024), 3),
            python_end_mib=round(current_memory / (1024 * 1024), 3),
            python_growth_mib=round(
                (current_memory - first_memory) / (1024 * 1024), 3
            ),
            max_runtime_tasks=int(monitor_stats["max_tasks"] or 0),
            database_mib=round(float(database_bytes) / (1024 * 1024), 3),
            wal_mib=round(float(wal_bytes) / (1024 * 1024), 3),
            slow_client_isolated=True,
            writer_tasks_after_disconnect=writer_tasks_after_disconnect,
            progress_samples=progress_samples,
        )
        await service.shutdown()
        await asyncio.sleep(0)
        assert not relevant_tasks()
        return result


def parse_profiles(raw: str) -> list[int]:
    profiles = sorted({int(value.strip()) for value in raw.split(",") if value.strip()})
    if not profiles or any(value < 1 or value > 200 for value in profiles):
        raise argparse.ArgumentTypeError("profiles must contain integers from 1 to 200")
    return profiles


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=parse_profiles, default=parse_profiles("10,25,50"))
    parser.add_argument("--messages-per-visitor", type=int, default=1)
    parser.add_argument("--duration-seconds", type=float, default=0.0)
    parser.add_argument("--messages-per-second", type=float, default=1.0)
    parser.add_argument("--model-delay-ms", type=float, default=2.0)
    parser.add_argument("--progress-seconds", type=float, default=30.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.messages_per_visitor < 1 or args.messages_per_visitor > 20:
        parser.error("--messages-per-visitor must be between 1 and 20")
    if args.duration_seconds < 0:
        parser.error("--duration-seconds cannot be negative")
    if not 0.01 <= args.messages_per_second <= 100:
        parser.error("--messages-per-second must be between 0.01 and 100")
    if not 0 <= args.model_delay_ms <= 10000:
        parser.error("--model-delay-ms must be between 0 and 10000")
    if not 5 <= args.progress_seconds <= 3600:
        parser.error("--progress-seconds must be between 5 and 3600")
    return args


async def async_main(args: argparse.Namespace) -> dict[str, Any]:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results = []
    for profile in args.profiles:
        result = await run_profile(args, profile)
        results.append(asdict(result))
        console_result = dict(results[-1])
        console_result["progress_sample_count"] = len(
            console_result.pop("progress_samples")
        )
        print(json.dumps(console_result, ensure_ascii=False), flush=True)
    return {
        "ok": True,
        "started_at": started_at,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "configuration": {
            "profiles": args.profiles,
            "messages_per_visitor": args.messages_per_visitor,
            "duration_seconds_per_profile": args.duration_seconds,
            "messages_per_second": args.messages_per_second,
            "model_delay_ms": args.model_delay_ms,
            "providers": "deterministic-fakes",
        },
        "results": results,
    }


def main() -> None:
    args = arguments()
    report = asyncio.run(async_main(args))
    if args.output:
        destination = args.output.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(f"{destination.suffix}.tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8"
        )
        os.replace(temporary, destination)
        print(f"capacity report written: {destination}")
    print("room capacity verification passed")


if __name__ == "__main__":
    main()
