"""Engine settings (`engine.settings`): capture, ring buffer, disk and topic-observation values, with the
original defaults.

Values come from the settings KV (`ctx.settings`): keys `monitor.<field>`, plus the wizard's
`processing.max_concurrent_recordings`, `storage.dir` and `ffmpeg_path`. The clip window and detector values
are selection-format data (`live_clipper.formats`, resolved per moment) and the encode values are editing-
preset data (`live_clipper.presets`); they are not engine settings any more.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

RING_MARKER = ".live-clipper-ring"  # ownership marker inside a transient ring directory (never swept)


@dataclass
class Settings:
    data_dir: Path
    buffer_seconds: float = 90.0
    segment_seconds: float = 2.0
    disk_ceiling_mb: int = 2048  # whole ring, all channels
    min_free_mb: int = 4096  # below: new clips refused; captures restart at this level
    critical_free_mb: int = 1536  # below: captures paused
    max_active_clips: int = 6
    max_concurrent_recordings: int = 0  # 0 = no limit (wizard: processing.max_concurrent_recordings)
    topic_observe_seconds: int = 300
    topic_category_evidence: bool = True
    public_url: str = ""
    kick_webhook_url: str = ""  # public HTTPS URL forwarding to the Kick webhook listener
    kick_webhook_port: int = 8788
    ffmpeg_path: str = "ffmpeg"

    @property
    def default_buffer_dir(self) -> Path:
        return self.data_dir / "buffer"

    @property
    def buffer_dir(self) -> Path:
        return self.default_buffer_dir

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.buffer_dir, self.clips_dir):
            p.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_kv(cls, kv, media_dir: Path) -> Settings:
        s = cls(data_dir=Path(kv.get("storage.dir") or media_dir))
        for f in fields(cls):
            v = kv.get(f"monitor.{f.name}")
            if v is not None and f.name != "data_dir":
                setattr(s, f.name, type(getattr(s, f.name))(v))
        s.max_concurrent_recordings = int(
            kv.get("processing.max_concurrent_recordings") or s.max_concurrent_recordings
        )
        s.ffmpeg_path = kv.get("ffmpeg_path") or s.ffmpeg_path
        return s
