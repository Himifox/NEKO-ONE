"""Small, public-safe Live2D asset and state helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from pathlib import PurePosixPath
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
MODEL_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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
    """Describe one operator-installed, explicitly licensed Live2D model.

    Public-room v1 deliberately does not expose the legacy model browser,
    Workshop or arbitrary filesystem paths. The public repository does not
    bundle a character model whose redistribution rights cannot be proven.
    """

    def __init__(self, *, data_dir: Path):
        self.assets_root = (data_dir / "live2d").resolve()
        self.assets_root.mkdir(parents=True, exist_ok=True)
        self.model_name = os.environ.get("NEKO_PUBLIC_LIVE2D_MODEL_NAME", "").strip()
        self.model_file = os.environ.get("NEKO_PUBLIC_LIVE2D_MODEL_FILE", "").strip()
        self.configuration_error = self._configuration_error()

    def _configuration_error(self) -> str | None:
        if not self.model_name and not self.model_file:
            return None
        if not self.model_name or not self.model_file:
            return "incomplete"
        if MODEL_COMPONENT.fullmatch(self.model_name) is None:
            return "invalid"
        if (
            MODEL_COMPONENT.fullmatch(self.model_file) is None
            or not self.model_file.endswith(".model3.json")
        ):
            return "invalid"
        return None

    @property
    def model_path(self) -> Path | None:
        if not self.model_name or not self.model_file or self.configuration_error:
            return None
        return self.assets_root / self.model_name / self.model_file

    def prepare(self) -> None:
        self.assets_root.mkdir(parents=True, exist_ok=True)

    def _reference_exists(self, model_root: Path, value: Any) -> bool:
        if not isinstance(value, str) or not value or "\\" in value:
            return False
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            return False
        candidate = model_root.joinpath(*relative.parts).resolve()
        return candidate.is_relative_to(model_root) and candidate.is_file()

    def _model_is_ready(self, model_path: Path) -> bool:
        try:
            if model_path.stat().st_size > 1024 * 1024:
                return False
            descriptor = json.loads(model_path.read_text(encoding="utf-8"))
            if not isinstance(descriptor, dict):
                return False
            references = descriptor.get("FileReferences")
            if not isinstance(references, dict):
                return False
            model_root = model_path.parent.resolve()
            if not self._reference_exists(model_root, references.get("Moc")):
                return False
            textures = references.get("Textures")
            if (
                not isinstance(textures, list)
                or not textures
                or not all(self._reference_exists(model_root, item) for item in textures)
            ):
                return False
            for key in ("Physics", "Pose", "UserData"):
                if key in references and not self._reference_exists(model_root, references[key]):
                    return False
            expressions = references.get("Expressions", [])
            if not isinstance(expressions, list):
                return False
            for expression in expressions:
                if not isinstance(expression, dict) or not self._reference_exists(
                    model_root, expression.get("File")
                ):
                    return False
            motions = references.get("Motions", {})
            if not isinstance(motions, dict):
                return False
            for group in motions.values():
                if not isinstance(group, list):
                    return False
                for motion in group:
                    if not isinstance(motion, dict) or not self._reference_exists(
                        model_root, motion.get("File")
                    ):
                        return False
                    if "Sound" in motion and not self._reference_exists(
                        model_root, motion["Sound"]
                    ):
                        return False
            return True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False

    def manifest(self) -> dict[str, Any]:
        model_path = self.model_path
        model_exists = bool(model_path and model_path.is_file())
        enabled = bool(model_path and model_exists and self._model_is_ready(model_path))
        if self.configuration_error:
            status = "invalid_configuration"
        elif not self.model_name:
            status = "not_configured"
        elif not model_exists:
            status = "missing_model"
        elif not enabled:
            status = "invalid_model"
        else:
            status = "ready"
        return {
            "enabled": enabled,
            "status": status,
            "model_name": self.model_name or None,
            "model_url": (
                f"/live2d-assets/{self.model_name}/{self.model_file}"
                if enabled
                else None
            ),
            "emotions": ["neutral", "happy", "sad", "angry", "surprised"],
        }
