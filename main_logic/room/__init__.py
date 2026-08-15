"""Public-room runtime built on the existing N.E.K.O providers."""

__all__ = ["PublicRoomService"]


def __getattr__(name: str):
    if name == "PublicRoomService":
        from .service import PublicRoomService

        return PublicRoomService
    raise AttributeError(name)
