# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
# Licensed under the Apache License, Version 2.0.

"""Character ``_reserved`` helpers for the Live2D-only public runtime."""

from __future__ import annotations

from config import RESERVED_FIELD_SCHEMA
from utils.voice_config import read_legacy_voice_id


# Values from the retired desktop renderers/marketplace are discarded when an
# existing character file is first loaded. Keeping this denylist is a migration
# safety boundary, not support for those features.
REMOVED_LEGACY_CHARACTER_FIELDS = (
    "live3d_sub_type",
    "live2d_item_id",
    "item_id",
    "vrm",
    "vrm_animation",
    "idleAnimation",
    "idleAnimations",
    "lighting",
    "vrm_rotation",
    "mmd",
    "mmd_animation",
    "mmd_idle_animation",
    "mmd_idle_animations",
    "pngtuber",
    "pngtuber_idle_image",
    "pngtuber_talking_image",
    "pngtuber_happy_image",
    "pngtuber_sad_image",
    "pngtuber_angry_image",
    "pngtuber_surprised_image",
    "原始数据",
    "文件路径",
    "创意工坊物品ID",
)


def get_reserved(
    data: dict,
    *path,
    default=None,
    legacy_keys: tuple[str, ...] | None = None,
):
    """Read a nested reserved value, optionally falling back to flat v1 keys."""

    if not isinstance(data, dict):
        return default
    current = data.get("_reserved")
    if isinstance(current, dict):
        for key in path:
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            return current
    for legacy_key in legacy_keys or ():
        if data.get(legacy_key) is not None:
            return data[legacy_key]
    return default


def set_reserved(data: dict, *path_and_value) -> bool:
    """Write a nested reserved value and report whether it changed."""

    if not isinstance(data, dict) or len(path_and_value) < 2:
        return False
    *path, value = path_and_value
    reserved = data.setdefault("_reserved", {})
    if not isinstance(reserved, dict):
        reserved = {}
        data["_reserved"] = reserved
    current = reserved
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    if current.get(path[-1]) == value and path[-1] in current:
        return False
    current[path[-1]] = value
    return True


def delete_reserved(data: dict, *path) -> bool:
    """Delete a nested value and prune empty parents."""

    reserved = data.get("_reserved") if isinstance(data, dict) else None
    if not isinstance(reserved, dict) or not path:
        return False
    current = reserved
    parents: list[tuple[dict, str]] = []
    for key in path[:-1]:
        if not isinstance(current, dict) or key not in current:
            return False
        parents.append((current, key))
        current = current[key]
    if not isinstance(current, dict) or path[-1] not in current:
        return False
    current.pop(path[-1])
    for parent, key in reversed(parents):
        if isinstance(parent.get(key), dict) and not parent[key]:
            parent.pop(key)
        else:
            break
    if not reserved:
        data.pop("_reserved", None)
    return True


def _legacy_live2d_to_model_path(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.endswith(".model3.json"):
        return raw
    return f"{raw}/{raw}.model3.json"


def _legacy_live2d_name_from_model_path(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.endswith(".model3.json"):
        parent = raw.rsplit("/", 1)[0] if "/" in raw else ""
        if parent:
            return parent.rsplit("/", 1)[-1]
        return raw.rsplit("/", 1)[-1][: -len(".model3.json")]
    return raw.rsplit("/", 1)[-1]


def validate_reserved_schema(reserved: dict) -> list[str]:
    """Validate known reserved values; migration removes retired renderer nodes."""

    errors: list[str] = []

    def walk(value, schema, path: str) -> None:
        if isinstance(schema, dict):
            if not isinstance(value, dict):
                errors.append(f"{path} 需要 dict，实际 {type(value).__name__}")
                return
            for key, sub_schema in schema.items():
                if value.get(key) is not None:
                    walk(value[key], sub_schema, f"{path}.{key}")
            return
        if isinstance(schema, tuple):
            if not isinstance(value, schema):
                expected = ",".join(item.__name__ for item in schema)
                errors.append(f"{path} 需要类型({expected})，实际 {type(value).__name__}")
            return
        if not isinstance(value, schema):
            errors.append(f"{path} 需要 {schema.__name__}，实际 {type(value).__name__}")

    if reserved is None:
        return errors
    walk(reserved, RESERVED_FIELD_SCHEMA, "_reserved")
    voice_id = reserved.get("voice_id") if isinstance(reserved, dict) else None
    if isinstance(voice_id, dict):
        for field in ("source", "provider", "ref"):
            if not isinstance(voice_id.get(field), str):
                errors.append(
                    f"_reserved.voice_id.{field} 需要 str，实际 "
                    f"{type(voice_id.get(field)).__name__}"
                )
    return errors


def migrate_catgirl_reserved(catgirl_data: dict) -> bool:
    """Move retained fields to v2 and permanently discard removed renderer data."""

    if not isinstance(catgirl_data, dict):
        return False
    changed = False
    if not isinstance(catgirl_data.get("_reserved"), dict):
        catgirl_data["_reserved"] = {}
        changed = True

    voice_id = get_reserved(catgirl_data, "voice_id", default="", legacy_keys=("voice_id",))
    changed |= set_reserved(
        catgirl_data,
        "voice_id",
        voice_id if isinstance(voice_id, dict) else str(voice_id or ""),
    )
    system_prompt = get_reserved(
        catgirl_data,
        "system_prompt",
        default=None,
        legacy_keys=("system_prompt",),
    )
    if system_prompt is not None:
        changed |= set_reserved(catgirl_data, "system_prompt", str(system_prompt))

    changed |= set_reserved(catgirl_data, "avatar", "model_type", "live2d")
    changed |= set_reserved(catgirl_data, "avatar", "asset_source", "local")
    for retired_node in (
        "live3d_sub_type",
        "asset_source_id",
        "vrm",
        "mmd",
        "pngtuber",
    ):
        changed |= delete_reserved(catgirl_data, "avatar", retired_node)

    model_path = get_reserved(
        catgirl_data,
        "avatar",
        "live2d",
        "model_path",
        default="",
        legacy_keys=("live2d",),
    )
    if model_path:
        changed |= set_reserved(
            catgirl_data,
            "avatar",
            "live2d",
            "model_path",
            _legacy_live2d_to_model_path(str(model_path)),
        )
    idle = get_reserved(
        catgirl_data,
        "avatar",
        "live2d",
        "idle_animation",
        default=None,
        legacy_keys=("live2d_idle_animation",),
    )
    if isinstance(idle, list):
        idle = idle[0] if idle else None
    if idle is not None:
        changed |= set_reserved(
            catgirl_data,
            "avatar",
            "live2d",
            "idle_animation",
            str(idle) if idle else None,
        )

    for key in (
        "voice_id",
        "system_prompt",
        "model_type",
        "live2d",
        "live2d_idle_animation",
        *REMOVED_LEGACY_CHARACTER_FIELDS,
    ):
        if key in catgirl_data:
            catgirl_data.pop(key, None)
            changed = True
    return changed


def flatten_reserved(catgirl_data: dict) -> dict:
    """Expose only retained legacy flat fields to compatibility callers."""

    if not isinstance(catgirl_data, dict):
        return catgirl_data
    result = dict(catgirl_data)
    voice_id = read_legacy_voice_id(get_reserved(result, "voice_id", default=""))
    if voice_id:
        result["voice_id"] = voice_id
    system_prompt = get_reserved(result, "system_prompt", default=None)
    if system_prompt is not None:
        result["system_prompt"] = system_prompt
    result["model_type"] = "live2d"
    model_path = get_reserved(result, "avatar", "live2d", "model_path", default="")
    if model_path:
        result["live2d"] = _legacy_live2d_name_from_model_path(str(model_path))
    idle = get_reserved(result, "avatar", "live2d", "idle_animation", default=None)
    if idle is not None:
        result["live2d_idle_animation"] = idle
    touch_set = get_reserved(result, "touch_set", default=None)
    if touch_set:
        result["touch_set"] = touch_set
    return result
