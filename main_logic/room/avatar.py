"""Small, public-safe Live2D asset and state helpers."""

from __future__ import annotations

import re
import tarfile
from pathlib import Path
from typing import Any


EMOTION_ALIASES = {
    "happy": "happy",
    "开心": "happy",
    "高兴": "happy",
    "sad": "sad",
    "悲伤": "sad",
    "难过": "sad",
    "angry": "angry",
    "生气": "angry",
    "愤怒": "angry",
    "surprised": "surprised",
    "surprise": "surprised",
    "惊讶": "surprised",
    "neutral": "neutral",
    "平静": "neutral",
    "relaxed": "neutral",
}
EMOTION_TAG = re.compile(r"<\s*([^<>]{1,24})\s*>", re.IGNORECASE)


def split_emotion_tags(text: str) -> tuple[str, str]:
    """Return display text and the last supported semantic emotion tag."""

    emotion = "neutral"

    def replace(match: re.Match[str]) -> str:
        nonlocal emotion
        mapped = EMOTION_ALIASES.get(match.group(1).strip().lower())
        if mapped:
            emotion = mapped
            return ""
        return match.group(0)

    cleaned = EMOTION_TAG.sub(replace, str(text or ""))
    return cleaned.strip(), emotion


class PublicAvatar:
    """Extract and describe one bundled Live2D model for the public page.

    Public-room v1 deliberately does not expose the legacy model browser,
    Workshop or arbitrary filesystem paths. An operator may choose another
    bundled archive with environment variables, but clients only receive a
    same-origin model URL.
    """

    def __init__(self, *, repo_root: Path, data_dir: Path):
        self.repo_root = repo_root.resolve()
        self.assets_root = (data_dir / "live2d").resolve()
        self.assets_root.mkdir(parents=True, exist_ok=True)
        self.model_name = "yui-lolita"
        self.model_file = "yui-lolita.model3.json"
        self.archive = self.repo_root / "assets" / "yui-lolita.tar.gz"

    @property
    def model_path(self) -> Path:
        return self.assets_root / self.model_name / self.model_file

    def prepare(self) -> None:
        if self.model_path.is_file():
            return
        if not self.archive.is_file():
            raise FileNotFoundError(f"bundled Live2D archive is missing: {self.archive}")
        with tarfile.open(self.archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                if member.issym() or member.islnk() or member.isdev():
                    raise ValueError("Live2D archive contains an unsupported link or device")
                target = (self.assets_root / member.name).resolve()
                try:
                    target.relative_to(self.assets_root)
                except ValueError as exc:
                    raise ValueError("Live2D archive contains an unsafe path") from exc
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError(f"cannot read Live2D archive member: {member.name}")
                with source, target.open("wb") as destination:
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
        if not self.model_path.is_file():
            raise FileNotFoundError("bundled Live2D model descriptor was not extracted")

    def manifest(self) -> dict[str, Any]:
        return {
            "enabled": self.model_path.is_file(),
            "model_name": self.model_name,
            "model_url": f"/live2d-assets/{self.model_name}/{self.model_file}",
            "emotions": ["neutral", "happy", "sad", "angry", "surprised"],
        }
