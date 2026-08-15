"""Lean public-room server entry point."""

from .web_app import app, create_app

__all__ = ["app", "create_app"]
