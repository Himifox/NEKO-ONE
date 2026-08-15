"""Minimal dependency bindings shared by the public and memory services."""

from __future__ import annotations

_installed = False


def install_runtime_bindings() -> None:
    """Bind language and token helpers required by prompt construction."""

    global _installed
    if _installed:
        return
    from config._runtime import (
        register_global_language_resolver,
        register_language_normalizer,
        register_truncate_to_tokens,
    )
    from utils.language_utils import (
        get_global_language_full,
        normalize_language_code,
    )
    from utils.tokenize import truncate_to_tokens

    register_global_language_resolver(get_global_language_full)
    register_language_normalizer(normalize_language_code)
    register_truncate_to_tokens(truncate_to_tokens)
    _installed = True
