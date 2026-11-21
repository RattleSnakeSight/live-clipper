// Plain-language labels for states shared by several screens.

import { h } from './ui.js';

export const PLATFORMS = { twitch: 'Twitch', kick: 'Kick', youtube: 'YouTube' };

export const CHANNEL_STATES = {
  offline: ['Offline', 'neutral', 'The stream is not live.'],
  waiting: ['Waiting', 'accent', 'Watching for the stream to start.'],
  live: ['Live', 'accent', 'The stream is live; recording starts when a format matches.'],
  recording: ['Recording', 'pos', 'Capturing the stream now.'],
  reconnecting: ['Reconnecting', 'warn', 'The stream dropped. Trying again.'],
  needs_action: ['Needs action', 'warn', 'Something needs you before watching can continue.'],
  disabled: ['Disabled', 'neutral', 'Turned off. Nothing is watched or recorded.'],
};

export const CLIP_STATES = {
  rendering: ['Rendering', 'accent', 'The video is being made.'],
  held: ['Held', 'warn', 'Waiting for your review before posting.'],
  queued: ['Queued', 'neutral', 'Waiting its turn to be posted.'],
  posted: ['Posted', 'pos', 'Posted to at least one destination.'],
  failed: ['Failed', 'neg', 'Something went wrong. Open the clip to see why.'],
};

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
  draft: ['Saved as draft', 'pos', 'Finish it in the site's own app.'],
  failed: ['Failed', 'neg', 'The upload failed. See the error.'],
  cancelled: ['Cancelled', 'neutral', ''],
  skipped: ['Skipped', 'neutral', ''],
};

export function pill(map, state) {
  const [label, tone, desc] = map[state] || [state || 'Unknown', 'neutral', ''];
  return h('span', { class: `pill ${tone}`, title: desc || null }, h('span', { class: 'dot', 'aria-hidden': 'true' }), label);
}

export function statusSource(c) {
  const p = PLATFORMS[c.platform] || c.platform;
  if (c.status_source === 'stream_probe') return 'Status from a stream check (no API connection, slower)';
  if (c.status_source === 'official_api') return `Status from the official ${p} API`;
  return 'Status source unknown';
}

export const channelName = (c) => c.display_name || c.login || c.url;
