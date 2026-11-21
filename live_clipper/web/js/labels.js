// Plain-language labels for states shared by several screens.

import { h } from './ui.js';

export const PLATFORMS = { twitch: 'Twitch', kick: 'Kick', youtube: 'YouTube' };

export const CHANNEL_STATES = {
  offline: ['Offline', 'neutral', 'The stream is not live.'],
  waiting: ['Waiting', 'accent', 'Watching for the stream to start.'],
  recording: ['Recording', 'pos', 'Capturing the stream now.'],
  reconnecting: ['Reconnecting', 'warn', 'The stream dropped. Trying again.'],
  needs_action: ['Needs action', 'warn', 'Something needs you before watching can continue.'],
  disabled: ['Disabled', 'neutral', 'Turned off. Nothing is watched or recorded.'],
};

// Clip states as the clip list shows them (clipStatus() derives them from the clip row).
export const CLIP_STATES = {
  cutting: ['Cutting', 'accent', 'The moment is being cut from the recording.'],
  rendering: ['Rendering', 'accent', 'Captions and the finished video are being made.'],
  ready: ['Ready', 'pos', 'Finished and handed to posting.'],
  held: ['Held for review', 'warn', 'A rule or the gameplay check held it. Approve it or post it anyway.'],
  rejected: ['Not selected', 'neutral', 'The format or a rule decided not to post it. You can still post it.'],
  failed: ['Failed', 'neg', 'Something went wrong. Open the clip to see why.'],
  discarded: ['Discarded', 'neutral', 'Removed before it was finished.'],
  expired: ['Expired', 'neutral', 'Its files were cleaned up.'],
};

// Row status (pending, cutting, preview_ready, captioning, rendering, ready, failed, ...) plus the selection
// decision per language -> one CLIP_STATES key.
export function clipStatus(c) {
  const s = c.status;
  if (s === 'pending' || s === 'cutting') return 'cutting';
  if (s === 'preview_ready' || s === 'captioning' || s === 'rendering' || c.rendering) return 'rendering';
  if (s !== 'ready') return CLIP_STATES[s] ? s : 'failed';
  const d = Object.values((c.trigger && c.trigger.selection) || {}).map((x) => x && x.decision);
  if (d.includes('qualified') || !d.length) return 'ready';
  return d.includes('held') ? 'held' : 'rejected';
}

export function clipTitle(c) {
  const caps = c.captions || {};
  const t = c.title || (caps[c.source_lang] || {}).title || Object.values(caps).map((x) => x && x.title).find(Boolean);
  return t || `Clip ${c.id}`;
}

export const DELIVERY_STATES = {
  held: ['Waiting for you', 'warn', 'Manual mode: press Post to send it.'],
  waiting_language: ['Waiting for captions', 'neutral', 'Posting starts when the captions are ready.'],
  queued: ['Queued', 'neutral', 'Waiting its turn.'],
  uploading: ['Uploading', 'accent', 'Sending the video now.'],
  processing: ['Processing', 'accent', 'The site is processing the upload.'],
  uncertain: ['Checking', 'warn', 'The upload may have worked; the app is checking.'],
  paused: ['Paused', 'warn', 'The account needs to be reconnected.'],
  needs_confirmation: ['Check the account', 'warn', 'Look at the account and confirm whether it was posted.'],
  blocked: ['Blocked', 'warn', 'The destination is disabled or not allowed for this channel.'],
  disallowed: ['Not allowed', 'neutral', 'This channel may not post here.'],
  published: ['Posted', 'pos', 'Public on the site.'],
  private: ['Posted privately', 'pos', 'Uploaded as private; only you can see it.'],
  draft: ['Saved as draft', 'pos', "Finish it in the site's own app."],
  failed: ['Failed', 'neg', 'The upload failed. See the error.'],
  cancelled: ['Cancelled', 'neutral', ''],
  skipped: ['Skipped', 'neutral', ''],
};

export function pill(map, state) {
  const [label, tone, desc] = map[state] || [state || 'Unknown', 'neutral', ''];
  return h('span', { class: `pill ${tone}`, title: desc || null }, h('span', { class: 'dot', 'aria-hidden': 'true' }), label);
}

// status_source is the monitor's own label: an official API name, the stream probe, or a creator feed.
export function statusSource(c) {
  const src = String(c.status_source || '');
  if (src.startsWith('stream probe')) return 'Status from the stream probe (no official API connected; slower)';
  if (src === 'creator-provided feed') return 'Records a creator-provided feed';
  if (src) return `Status from the official API (${src})`;
  return 'Status source unknown';
}

export const channelName = (c) => c.display_name || c.login || c.url;
export const platformName = (p) => PLATFORMS[p] || (p === 'custom' ? 'Creator feed' : p);
// A service reports a failure as {error} (status() raised) or {state: "error", detail} (monitor engine).
export const svcError = (v) => (v && (v.error || (v.state === 'error' && (v.detail || 'failed to start')))) || null;
export const isTopicMode = (c) => c.watch_mode === 'topic_gated';
