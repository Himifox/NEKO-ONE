# Copyright (c) 2024 Alibaba Inc (authors: Xiang Lyu, Zhihao Du)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Small text/time helpers still used by the public and Memory runtimes."""

from __future__ import annotations

from datetime import datetime


def calculate_text_similarity(text1: str, text2: str) -> float:
    """Return the character-trigram Jaccard similarity of two strings."""

    if not text1 or not text2:
        return 0.0

    def trigrams(text: str) -> set[str]:
        normalized = text.lower().strip()
        if len(normalized) < 3:
            return {normalized}
        return {
            normalized[index : index + 3]
            for index in range(len(normalized) - 2)
        }

    left = trigrams(text1)
    right = trigrams(text2)
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def get_timestamp() -> str:
    """Return the English timestamp format expected by existing Memory prompts."""

    now = datetime.now()
    weekdays = (
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    )
    months = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )
    hour = now.hour % 12 or 12
    meridiem = "AM" if now.hour < 12 else "PM"
    return (
        f"{weekdays[now.weekday()]}, {months[now.month - 1]} {now.day:02d}, "
        f"{now.year} at {hour:02d}:{now.minute:02d} {meridiem}"
    )
