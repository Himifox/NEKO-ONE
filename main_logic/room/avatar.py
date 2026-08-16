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
SOULLINK_PROFILE_FILE = "soullink.profile.json"
SOULLINK_MOTION_STYLES = frozenset({"natural", "lively", "calm", "shy"})


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

    def configure(self, *, model_name: str, model_file: str) -> dict[str, Any]:
        """Select an installed model after validating its complete asset graph."""

        previous = (self.model_name, self.model_file, self.configuration_error)
        self.model_name = str(model_name or "").strip()
        self.model_file = str(model_file or "").strip()
        self.configuration_error = self._configuration_error()
        manifest = self.manifest()
        if manifest["status"] != "ready":
            self.model_name, self.model_file, self.configuration_error = previous
            raise ValueError(str(manifest["status"]))
        return manifest

    def disable(self) -> dict[str, Any]:
        self.model_name = ""
        self.model_file = ""
        self.configuration_error = None
        return self.manifest()

    def restore(self, selection: Any) -> dict[str, Any]:
        """Apply a trusted persisted selection, falling back to env configuration."""

        if not isinstance(selection, dict):
            return self.manifest()
        if selection.get("enabled") is False:
            return self.disable()
        try:
            return self.configure(
                model_name=str(selection.get("model_name") or ""),
                model_file=str(selection.get("model_file") or ""),
            )
        except ValueError:
            # A removed or damaged model must not prevent the room from starting.
            return self.disable()

    @property
    def model_path(self) -> Path | None:
        if not self.model_name or not self.model_file or self.configuration_error:
            return None
        candidate = self.assets_root / self.model_name / self.model_file
        try:
            if not candidate.resolve().is_relative_to(self.assets_root):
                return None
        except OSError:
            return None
        return candidate

    def prepare(self) -> None:
        self.assets_root.mkdir(parents=True, exist_ok=True)

    def _soullink_requested(self) -> bool:
        return os.environ.get("NEKO_PUBLIC_SOULLINK_ENABLED", "0").strip() == "1"

    def _soullink_motion_style(self) -> str:
        style = os.environ.get("NEKO_PUBLIC_SOULLINK_MOTION_STYLE", "natural").strip()
        return style if style in SOULLINK_MOTION_STYLES else "natural"

    def _soullink_profile(self, model_root: Path) -> dict[str, Any] | None:
        """Return only a small, structurally valid public Soullink profile."""

        profile_path = model_root / SOULLINK_PROFILE_FILE
        try:
            if (
                not profile_path.resolve().is_relative_to(model_root)
                or not profile_path.is_file()
                or profile_path.stat().st_size > 512 * 1024
            ):
                return None
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(profile, dict) or not isinstance(profile.get("parameterMap"), dict):
            return None
        return profile

    def _reference_path(
        self, model_root: Path, value: Any
    ) -> PurePosixPath | None:
        if not isinstance(value, str) or not value or "\\" in value:
            return None
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            return None
        candidate = model_root.joinpath(*relative.parts).resolve()
        if not candidate.is_relative_to(model_root) or not candidate.is_file():
            return None
        return relative

    def _model_assets(self, model_path: Path) -> set[str] | None:
        try:
            if model_path.stat().st_size > 1024 * 1024:
                return None
            descriptor = json.loads(model_path.read_text(encoding="utf-8"))
            if not isinstance(descriptor, dict):
                return None
            references = descriptor.get("FileReferences")
            if not isinstance(references, dict):
                return None
            model_root = model_path.parent.resolve()
            assets = {model_path.name}

            def include(value: Any) -> bool:
                relative = self._reference_path(model_root, value)
                if relative is None:
                    return False
                assets.add(relative.as_posix())
                return True

            if not include(references.get("Moc")):
                return None
            textures = references.get("Textures")
            if (
                not isinstance(textures, list)
                or not textures
                or not all(include(item) for item in textures)
            ):
                return None
            for key in ("Physics", "Pose", "UserData", "DisplayInfo"):
                if key in references and not include(references[key]):
                    return None
            expressions = references.get("Expressions", [])
            if not isinstance(expressions, list):
                return None
            for expression in expressions:
                if not isinstance(expression, dict) or not include(expression.get("File")):
                    return None
            motions = references.get("Motions", {})
            if not isinstance(motions, dict):
                return None
            for group in motions.values():
                if not isinstance(group, list):
                    return None
                for motion in group:
                    if not isinstance(motion, dict) or not include(motion.get("File")):
                        return None
                    if "Sound" in motion and not include(motion["Sound"]):
                        return None
            if self._soullink_requested() and self._soullink_profile(model_root):
                assets.add(SOULLINK_PROFILE_FILE)
            return assets
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def installed_models(self) -> list[dict[str, Any]]:
        """Return safe descriptors found directly below the private model root."""

        models: list[dict[str, Any]] = []
        try:
            model_dirs = sorted(
                (
                    entry
                    for entry in self.assets_root.iterdir()
                    if entry.is_dir() and MODEL_COMPONENT.fullmatch(entry.name)
                ),
                key=lambda entry: entry.name.lower(),
            )[:100]
        except OSError:
            return models
        for model_dir in model_dirs:
            try:
                if not model_dir.resolve().is_relative_to(self.assets_root):
                    continue
            except OSError:
                continue
            try:
                descriptors = sorted(model_dir.glob("*.model3.json"))[:20]
            except OSError:
                continue
            for descriptor in descriptors:
                try:
                    descriptor_inside_model = descriptor.resolve().is_relative_to(
                        model_dir.resolve()
                    )
                except OSError:
                    descriptor_inside_model = False
                valid = bool(
                    descriptor_inside_model and self._model_assets(descriptor) is not None
                )
                models.append(
                    {
                        "model_name": model_dir.name,
                        "model_file": descriptor.name,
                        "valid": valid,
                        "active": bool(
                            valid
                            and model_dir.name == self.model_name
                            and descriptor.name == self.model_file
                            and self.manifest()["enabled"]
                        ),
                    }
                )
        return models

    def public_asset_paths(self) -> set[str]:
        model_path = self.model_path
        if model_path is None or not model_path.is_file():
            return set()
        assets = self._model_assets(model_path)
        if assets is None:
            return set()
        return {f"{self.model_name}/{asset}" for asset in assets}

    def manifest(self) -> dict[str, Any]:
        model_path = self.model_path
        model_exists = bool(model_path and model_path.is_file())
        enabled = bool(model_path and model_exists and self._model_assets(model_path))
        soullink_profile = (
            self._soullink_profile(model_path.parent)
            if model_path is not None and model_exists
            else None
        )
        soullink_enabled = bool(enabled and self._soullink_requested() and soullink_profile)
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
            "soullink": {
                "enabled": soullink_enabled,
                "status": (
                    "ready"
                    if soullink_enabled
                    else "disabled"
                    if not self._soullink_requested()
                    else "missing_profile"
                ),
                "profile_url": (
                    f"/live2d-assets/{self.model_name}/{SOULLINK_PROFILE_FILE}"
                    if soullink_enabled
                    else None
                ),
                "motion_style": self._soullink_motion_style(),
            },
        }
