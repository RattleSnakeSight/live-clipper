"""Shared event format. Every chat adapter emits ChatEvent; the detector emits Trigger.

Time conventions (all float seconds, Unix epoch, local worker clock unless noted):
  sent_at      platform-reported send time (platform clock), or None if unavailable
  received_at  time the worker received the message (local clock)
  event_time   normalized time used by the detector, see ChatEvent.timeline_time()
Media time mapping lives in live_clipper.timeline: the "stream timeline" is the local wall-clock
time at which the worker's capture saw a frame (comparable with received_at).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Platform = Literal["twitch", "kick", "youtube", "replay", "custom"]


@dataclass(frozen=True)
class ChatEvent:
    platform: Platform
    channel: str              # platform channel login / id as configured by operator
    msg_id: str               # platform message id (used for de-duplication)
    user_id: str
    user_name: str
    text: str
    received_at: float        # local clock
    sent_at: float | None = None   # platform clock (may be skewed)
    emotes: tuple[str, ...] = ()
    # where sent_at comes from: "platform" (provider timestamp), "fixture" (synthesized by a replay/test),
    # "" = derive: platform when sent_at is set, otherwise the receipt time is used (inferred)
    ts_source: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["emotes"] = list(self.emotes)
        return d

    @staticmethod
    def from_dict(d: dict) -> ChatEvent:
        d = dict(d)
        d["emotes"] = tuple(d.get("emotes") or ())
        return ChatEvent(**d)


@dataclass
class ChatHealth:
    """Status reported by a chat adapter. state: connecting|healthy|degraded|unavailable|stopped"""
    state: str = "stopped"
    detail: str = ""
    last_message_at: float | None = None
    reconnects: int = 0
    action_needed: str = ""   # human-readable fix, e.g. "Connect Twitch account with user:read:chat"


@dataclass
class Trigger:
    """A detected moment. action_time is on the stream timeline (local clock)."""
    channel_id: int
    reason: str               # human-readable explanation
    kind: Literal["chat_spike", "manual", "audio_spike"]
    burst_start: float        # timeline time where chat activity first crossed threshold
    action_time: float        # estimated time of the on-screen action (burst_start - delay)
    delay: float              # reaction delay applied (seconds)
    score: float = 0.0        # peak ratio vs baseline
    peak_rate: float = 0.0    # messages/10s at peak (per-user capped)
    unique_users: int = 0
    baseline: float = 0.0
    sample_messages: list[str] = field(default_factory=list)
    first_chat_received_at: float | None = None
    detected_at: float | None = None
    burst_end: float | None = None
    chat_time_basis: str = ""   # how burst message times were obtained (platform/fixture/inferred receipt)

    def to_dict(self) -> dict:
        return asdict(self)
