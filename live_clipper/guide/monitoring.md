# Monitoring streams

Add a channel, then turn monitoring on. While a channel is live, Live Clipper records it into a short rolling
buffer (about 90 seconds) and watches for moments worth clipping. When one qualifies, it cuts a clip from the
buffer. The rest of the recording is not kept unless a clip needs it.

You can add a channel by pasting its address:

- Twitch: `twitch.tv/name`
- Kick: `kick.com/name`
- YouTube: `youtube.com/@handle` or `youtube.com/channel/UC...`
- A feed address from a creator who gives you one (rtmp, rtmps, srt, rtsp or an http(s) stream address).

Only record channels you own or whose owner gave you permission. See [Platforms](platforms.md).

## Channel states

Each channel shows one state.

| State | What it means | What to do |
|---|---|---|
| Disabled | Monitoring is off for this channel. | Turn it on when you want it watched. |
| Offline | The channel is not live. | Nothing. The app keeps checking. |
| Waiting | Armed and waiting. The note says why: for a free recording slot, for the title to be on topic, or just for the stream to start. | Nothing, unless it waits for a slot for a long time. Then raise "streams recorded at the same time" or switch another channel off. |
| Recording | The channel is live and being recorded. | Nothing. |
| Reconnecting | The recording dropped (a network problem, for instance) and the app is restarting it. | Wait. It retries by itself and keeps growing its wait between tries. |
| Needs action | Something blocks it. Its row on the dashboard says what and how to fix it. | Follow the fix shown. Common ones are below. |

Common "needs action" messages:

- The platform rejected your credentials, or a token expired or was revoked. Reconnect it on the Sources page.
- A channel was not found. Check the address.
- ffmpeg or Streamlink is missing. Install it (see [Troubleshooting](troubleshooting.md)).
- YouTube says it is live but refuses playback to this computer ("Sign in to confirm you're not a bot"). Use a
  feed address from the creator, or try from a network YouTube accepts.
- The platform's quota or rate limit ran out. The app waits and tries again. See [Platforms](platforms.md).

## Watch modes

Each channel has one of two modes.

- Always watch (ungated). The app records whenever the channel is live.
- Only when the title matches (topic gated). The app watches the channel but records only while the live title
  matches the active format. With the built-in format, that means the title names GTA 6, GTA VI or Grand Theft
  Auto VI. The platform's category is shown but never starts or continues a recording on its own. If the
  platform gives neither a title nor a category, the app treats the topic as unverified and does not record. It
  re-checks every 60 to 900 seconds (default 120).

## Watching several streamers

You can add as many channels as you like. How many record at the same moment is the "streams recorded at the
same time" answer from setup. A channel over that limit shows Waiting until a slot frees up. Separately,
the app works on at most 6 clips at once. Each recording uses processor, memory and disk.

If free disk space gets low, the app stops starting new clips. If it gets critically low, recordings pause and
resume once space is free again. See [Troubleshooting](troubleshooting.md) for the cleanup order.

## How moments are chosen

With the built-in GTA VI format, the app looks at chat bursts, what the streamer says, and whether the picture
shows gameplay. A burst in chat counts only when enough different people take part, and it must be a clear step
up from the channel's normal chat. Greetings and "starting soon" chatter are filtered out. A clip needs a setup
and a payoff, and it must be fresh, not a repeat of an earlier clip. The format sets how sensitive this is
(low, medium or high). See [Formats and presets](formats-and-presets.md).

Each clip starts 18 seconds before the moment and ends 12 seconds after it by default.

## Keep the computer on

Live Clipper can only record while it runs, and it runs on your computer. Nothing records while the
computer is off, asleep or hibernating, or while you are logged out, and the app does not keep the computer
awake by itself. If you want streams caught around the clock:

- Set your power settings so the computer does not sleep while plugged in, at least during the hours your
  streamers are live.
- Turn on start at login (Settings, or `live-clipper service install`) so the app comes back after a restart.
- Leave the browser closed if you like: the app keeps working in the background. Browser notifications need
  an open tab.

A stream that went live while the computer slept is not recorded afterwards. The app resumes watching when the
computer wakes up.

## Crash recovery

A small supervisor program watches the app and restarts it if it stops. The Status page shows each service,
how many times it restarted, free disk space, ffmpeg and the latest log lines.

After a crash or restart:

- Clips that were still being made are put back in a queue and finished. Only a few run at a time, and clips
  from live streams go first.
- The app never makes a duplicate of a clip that was already finished.
- Uploads that were in flight are checked against the platform before anything is uploaded again (see
  [Posting](posting.md)).
- A clip that lost its source before it could be finished is marked failed, with the reason.

Use `live-clipper status` to see whether the app is running and `live-clipper logs` for recent lines.
