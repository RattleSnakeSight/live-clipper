# Deviations from the original design

## ops: storage, retention and cleanup (`live_clipper/maintenance.py`)

- **Google Drive cold tier dropped.** The original archived `source.ts` to Drive, verified it, then released
  the local copy. Drive access needs a private OAuth client and a hosted redirect, which a local-only public
  app does not have. Instead, when the disk cap is hit, originals (`source.ts`) of fully posted clips are
  deleted locally, oldest first (`storage.clean_first = "originals"`). They are not archived anywhere.
- **Thresholds kept**: `min_free_mb` 4096, `critical_free_mb` 1536, `max_active_clips` 6, disk cap default
  20 GB (the original `clips_ceiling_mb` 20480), trash keep 168 h clamped to 24-720 h, trash name pattern
  `<id>-<epoch>`, temp patterns `*.part.mp4`, `hook_*.txt`, `audio_pre.wav`, `audio_tail.wav`.
- **Admission order kept**: low free space, captures paused, over the clips ceiling, too many active clips.
- **Cap now triggers cleanup of posted clips.** The original never deleted ready clips by size ("nothing is
  deleted automatically"). Here the wizard's cleanup rules (`expire_posted`, `clean_when_full`) allow it, but
  only for clips whose deliveries are all finished with at least one `published`. Clips with no delivery,
  or any queued/held/uploading/other non-terminal delivery, are never touched. Protection is re-checked at
  delete time. Turn both rules off to get the original behavior.
- **Posted trash** is driven by `storage.retention_days` (original: `posted_trash_hours`).
- **Freshness trash (24 h unposted)** is not ported: unposted clips are always kept.
- **Queue-only retirement / clip ledger** is not ported (no historical retirement API).
- **Clip/delivery knowledge** comes from bus topics `edit.clip_ready` and `publish.delivery` into
  `maintenance_clip` / `maintenance_delivery`, not from the clip tables of other subsystems.
- **Ring buffer sweeping** belongs to the `edit` branch (`buffer`).
