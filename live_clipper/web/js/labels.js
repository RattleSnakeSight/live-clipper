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
  queued: ['Queued', 'neutral'], posting: ['Posting', 'accent'], posted: ['Posted', 'pos'],
  published: ['Posted', 'pos'], failed: ['Failed', 'neg'], held: ['Held', 'warn'], cancelled: ['Cancelled', 'neutral'],
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
