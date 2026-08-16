"""Explainable turn selection for a small public room."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from .models import TurnCandidate


class RoomDirector:
    def __init__(self, *, character_names: tuple[str, ...] = ("NEKO", "猫娘")):
        self.set_character_names(character_names)
        self._candidates: list[TurnCandidate] = []
        self._condition = asyncio.Condition()
        self._recent_targets: deque[str] = deque(maxlen=5)
        self._closed = False

    def set_character_names(self, character_names: tuple[str, ...]) -> None:
        """Refresh future direct-mention matching without dropping queued turns."""

        self._mention_names = tuple(
            dict.fromkeys(name.casefold() for name in character_names if name)
        )

    def _is_direct_mention(self, content: str) -> bool:
        text = str(content or "").casefold()
        for name in self._mention_names:
            if f"@{name}" in text:
                return True
            # A short name such as "a" is only allowed after @.  Longer
            # ASCII names retain natural mentions ("NEKO 你好") but must not
            # occur inside a larger ASCII word. CJK names use explicit @
            # syntax because Unicode word boundaries are ambiguous.
            if len(name) < 2 or not name.isascii():
                continue
            start = text.find(name)
            while start >= 0:
                end = start + len(name)
                before = text[start - 1] if start else ""
                after = text[end] if end < len(text) else ""
                if not (before.isascii() and (before.isalnum() or before == "_")) and not (
                    after.isascii() and (after.isalnum() or after == "_")
                ):
                    return True
                start = text.find(name, start + 1)
        return False

    async def enqueue(self, candidate: TurnCandidate) -> None:
        candidate.mentioned_neko = self._is_direct_mention(candidate.message.content)
        async with self._condition:
            self._candidates.append(candidate)
            self._condition.notify()

    async def next_turn(self) -> tuple[TurnCandidate, dict[str, Any]]:
        async with self._condition:
            await self._condition.wait_for(lambda: self._closed or bool(self._candidates))
            if self._closed:
                raise asyncio.CancelledError
            now = time.monotonic()
            ranked: list[tuple[float, int, TurnCandidate, dict[str, Any]]] = []
            for index, candidate in enumerate(self._candidates):
                wait_seconds = max(0.0, now - candidate.enqueued_monotonic)
                proactive = bool(candidate.message.metadata.get("proactive"))
                mention_bonus = 100.0 if candidate.mentioned_neko else 0.0
                fairness_penalty = 25.0 if candidate.message.author_id in self._recent_targets else 0.0
                age_score = min(wait_seconds, 120.0) / 4.0
                # A real visitor message always outranks an already queued
                # dead-air prompt that has not started yet.
                score = mention_bonus + age_score - fairness_penalty - (1000.0 if proactive else 0.0)
                decision = {
                    "score": round(score, 3),
                    "mentioned_neko": candidate.mentioned_neko,
                    "proactive": proactive,
                    "wait_seconds": round(wait_seconds, 3),
                    "fairness_penalty": fairness_penalty,
                    "tie_break_room_seq": candidate.message.room_seq,
                }
                ranked.append((score, -candidate.message.room_seq, candidate, decision))
            _, _, chosen, decision = max(ranked, key=lambda item: (item[0], item[1]))
            self._candidates.remove(chosen)
            self._recent_targets.append(chosen.message.author_id)
            if chosen.message.metadata.get("proactive"):
                decision["reason_code"] = str(
                    chosen.message.metadata.get("reason_code") or "dead_air"
                )
            else:
                decision["reason_code"] = "direct_mention" if chosen.mentioned_neko else "queued_message"
            return chosen, decision

    async def size(self) -> int:
        async with self._condition:
            return len(self._candidates)

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
