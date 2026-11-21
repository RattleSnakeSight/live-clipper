# Deviations from the original design

Each section is owned by one branch. Format: what changed, why, evidence.

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

## publish

Ported unchanged: the delivery state machine, idempotency (unique key + unique clip/destination/language/version),
backoff `min(900, 10*2^(n-1)) * U(0.5, 1.0)`, 6 attempts, timeout -> uncertain -> reconcile before any re-upload,
8 reconcile attempts -> needs_confirmation, a known provider id never becomes `failed`, restart turns
uploading/processing into uncertain, 429/401/refresh/revoked handling, the adapters' request shapes.

Public-product changes:

| Change | Reason |
|---|---|
| Dropped the `operator-runtime.json` allow-list, `native_publishing` flag, pinned `expected_account_id`, fresh-start cutoff / history gate, one-sender-per-account rule, Metricool egress and receipts, and the global social-publishing environment switch. | Owner-specific deployment gates; a public app has one user posting to their own accounts. |
| New gates: per-destination connected + enabled, a one-time rights/permission acknowledgement per destination, global `publish.mode` (`auto` default, `manual` holds finished clips as `held` until the user clicks Post). Disabling a destination moves unfinished deliveries to `blocked` (no provider call), enabling resumes them. | Product requirement. Without a connected, enabled, acknowledged destination nothing is posted. |
| Dropped freshness hard cutoff, creator spacing / moments conflict, auto-post rules and min score, language-policy and gameplay holds, `vod_only` and `reserved_for_channels`. | Owned by the select/monitor subsystems or specific to the owner's channels. Selection decides what becomes a clip; publish posts every finished clip to every eligible destination. |
| Kept the per-channel `publish_platforms` allowlist, stored as settings key `publish.channel_platforms` (`{channel_id: [provider]}`), API `PUT /api/publish/channel-platforms/{channel_id}`. | The channel table belongs to the monitor branch; this avoids a cross-branch schema dependency. |
| `token_expired` is now `needs_reconnect`; the destination notifies via `ctx.notify`. | Spec wording. |
| Clips are not read from a clips table: `edit.clip_ready` payloads are cached in `publish_clips` (one row per clip and language); `idempotency key = sha256("clip:<id>\|<destination>\|<language>")`. Language is the payload's `language`; there is no `waiting_language` flow (the file exists when the event fires). | The public edit contract carries path/title/description/hashtags. |
| Marker prefix `lc-` instead of `lsc-` (tags / caption / description marker used by reconcile). | Neutral naming. |
| Sandbox provider is the `test` destination, label "Test destination — nothing is posted", outcome `test`, never notifies, `publish.delivery` carries `test: true`, API deliveries carry `real: false`. | Must never be mistaken for a real post. State lives under `<data>/sandbox/`. |
| Added daily quota tracking (`publish_quota`): YouTube 100/day (midnight Pacific; fixed UTC-8 if no tz database), TikTok 15/day direct or 5/day inbox (UTC day), override with setting `daily_limit`. A delivery over the limit waits for the reset instead of attempting. Instagram keeps reading the live `content_publishing_limit`. | Research notes: YouTube insert bucket and TikTok caps. |
| `retry_after` also understands HTTP-date and `X-RateLimit-Reset`. | Rate-limit headers respected. |
| YouTube: `privacyStatus` always sent (default `public`, setting `privacy`), `paidProductPlacementDetails.hasPaidProductPlacement: false` always sent (`part=snippet,status,paidProductPlacementDetails`), no legacy `audited` flag, client secret optional (Desktop OAuth client). | Research: 2026-11-16 revision-history entry; the 2026-11-02 private-lock reversal conflicts with the Help page, so the result is labelled from the real `privacyStatus`. |
| TikTok: Login Kit for desktop with PKCE S256 and a HEX challenge (`Adapter.challenge()` hook), scopes `user.info.basic,video.upload,video.publish`, `code_verifier` in the token exchange. Default mode stays `inbox` (drafts); unaudited direct posts stay SELF_ONLY. | Research notes. |
| Instagram: `target_account_id` (optional, picks one account when a Facebook sign-in manages several) replaces the pin. Instagram Login cannot upload local files, so it uses `video_url` built from setting `video_base_url` (a public https folder the user serves); Facebook Login keeps the resumable `rupload` path. Redirect URI configurable (`redirect_uri` setting) and a paste-a-token form `POST /api/destinations/instagram/token` stores the token in the vault. | Meta loopback redirects are unverified; research notes. |
| OAuth state + PKCE verifier live in the vault (`publish/oauth/<state>`), single use, 900 s TTL, provider bound. Vault names: `publish/app/<provider>`, `publish/dest/<id>`, `publish/ref/<delivery>`. The callback registers the returned code as a secret before the token exchange. | Spec; credential hygiene. |
| Dropped `probe()`, `sweep_unsent`, `receipts`, `render_hook`, `orphan_deliveries`; added `cancel`, `confirm_posted`, `post`, `post_clip`, `disconnect`. | Owner-specific or replaced by the new API. |
| ruff: `E501` ignored for `live_clipper/publish/*.py` and `tests/*publish*.py` (ported code keeps the reference's line layout). | Avoid risky reformatting of ported code. |
