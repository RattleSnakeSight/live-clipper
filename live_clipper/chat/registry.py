"""Adapter factory."""
from __future__ import annotations

from .base import ChatAdapter, Emit


def make_adapter(platform: str, channel: str, emit: Emit, creds: dict | None = None,
                 options: dict | None = None) -> ChatAdapter:
    if platform == "twitch":
        from .twitch import TwitchChat as C
    elif platform == "kick":
        from .kick import KickChat as C
    elif platform == "youtube":
        from .youtube import YouTubeChat as C
    elif platform == "replay":
        from .replay import ReplayChat as C
    else:
        raise ValueError(f"unknown chat platform: {platform!r}")
    return C(channel, emit, creds, options)
