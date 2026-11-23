# Platforms

Live Clipper watches streams on Twitch, Kick and YouTube Live. These are its **sources**. It posts to YouTube
Shorts, TikTok and Instagram Reels, its **destinations**; those are on [Posting](posting.md). A feed address
from a creator (rtmp, rtmps, srt, rtsp or an http(s) stream) can be added as a channel too; it needs no keys.

The platform facts on this page and on Posting were checked against each platform's own documentation on
2026-11-22. Platforms change their rules and their approval decisions are their own: nothing here promises
that an application is approved, that API access stays free, or that every account can post. Where a detail
could not be confirmed, the page says "not confirmed".

## What works without any keys

You can start without registering anything:

- Live status for Twitch and Kick channels, through the stream probe (below).
- Twitch chat, read anonymously and read-only.
- Recording, with Streamlink and FFmpeg, of any stream Streamlink can play, or a feed address the creator
  gives you. A platform can refuse playback to some networks (see YouTube below).
- YouTube channels: the recorder opens the channel's live page and keeps retrying, so a stream is caught
  without keys, only less precisely.
- Clips, captions, editing, and the test destination.

Keys add precise live status, chat on Kick and YouTube, and posting. Typing them in is always possible; the
agent that installed the app can also fill them in, see [Let your agent set up keys](agent-setup.md).

| Platform | Keys | Where to get them |
|---|---|---|
| Twitch | Client ID, plus a secret or a device sign-in | [Twitch developer console](https://dev.twitch.tv/console) |
| Kick | Client ID and Client Secret | [Kick developer settings](https://kick.com/settings/developer) |
| YouTube | An API key, or a Google sign-in | [Google Cloud console](https://console.cloud.google.com) |

## Read this first: watching is not permission to record

Connecting a platform lets Live Clipper ask whether a streamer is live and, if you allow it, read their chat.
That is all it grants. It does not give you the right to record a stream, cut clips or post them anywhere.

- Recording is done by Streamlink and FFmpeg, separate tools. No platform API hands out the video.
- You need the streamer's permission to record and clip their stream. If the channel is yours, you have it.
- A platform's terms can forbid recording even with the streamer's permission:
  - [Kick's terms of service](https://kick.com/terms-of-service) forbid copying content from the service,
    using automated systems to access it, and reaching content by means other than those Kick provides.
    They do not mention recording by name.
  - [YouTube's developer policies](https://developers.google.com/youtube/terms/developer-policies) forbid
    downloading or storing copies of YouTube audiovisual content without YouTube's prior written approval.
    See also the [YouTube API terms of service](https://developers.google.com/youtube/terms/api-services-terms-of-service).
  - Twitch: read the [Twitch terms of service](https://www.twitch.tv/p/legal/terms-of-service/) and the
    developer agreement before you record. We make no claim about their text.
- You are responsible for following the terms of each platform you use.

## Three separate permissions

For each platform, Live Clipper keeps three things apart. Each can work without the others.

| Permission | What it does | Without it |
|---|---|---|
| Live status | Asks the official API whether a channel is live, with its title and category. | The stream probe is used. |
| Chat | Reads chat so busy moments can trigger clips. | Clips come from other signals, such as speech. |
| Capture | Records the stream with Streamlink and FFmpeg. | Nothing can be recorded. |

The Sources page shows, for each platform, which of these works and how.

## The stream probe

Without a platform's official API, the Sources page labels live status as "stream probe (no official API
connected)". For Twitch and Kick the app asks Streamlink's public lookup whether the channel is live, with no
login and no keys. It works, but it notices changes more slowly and can fail without a clear reason. For
YouTube there is no separate probe: the recorder opens the channel's live page itself and keeps retrying.

## Twitch

Cost: Twitch's documentation states no fee for registering an application.

1. Turn on two-factor authentication on your Twitch account; Twitch requires it for developers.
2. In the [Twitch developer console](https://dev.twitch.tv/console), register an application. It is your own;
   you do not share it.
3. Copy its Client ID and pick one way to connect:
   - **Client ID and Client Secret.** The app gets an app token from Twitch. No sign-in, no redirect address.
     Keep the secret private.
   - **Client ID only**, with the application's client type set to Public. Press "Get a Twitch code": the app
     shows a code and a Twitch page where you type it and approve (the device sign-in). No secret, no
     redirect address.
4. Enter the values on Sources > Twitch.

What each connection enables:

- Live status needs no scope; either connection works.
- Chat: without a sign-in, the app reads chat anonymously and read-only over Twitch's chat (IRC) interface.
  Twitch's current documentation does not describe anonymous reading, so treat it as working today rather
  than promised. With the device sign-in, the app asks for the `user:read:chat` scope and reads chat through
  EventSub, the method Twitch recommends.
- Capture needs no Twitch account.

Limits: Twitch gives each application a request allowance that refills every minute. The app asks about up to
100 channels in one request, once a minute, and waits when Twitch says "too many requests". Device sign-in
tokens last about 4 hours and the app renews them. If the app could not renew them for 30 days (for example,
because it was not running), sign in again.

If it fails: a wrong Client ID or Secret shows a "needs action" message with the fix. Docs:
[getting tokens](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/),
[Get Streams](https://dev.twitch.tv/docs/api/reference/streams).

## Kick

Cost and approval: Kick's documentation states no fee and no approval step for creating an application.

1. Turn on two-factor authentication on your Kick account.
2. In [Kick developer settings](https://kick.com/settings/developer), create an application and copy its
   Client ID and Client Secret.
3. Pick one way to connect:
   - **Client ID and Client Secret.** The app gets an app token from Kick. Nothing to approve.
   - **Sign in with Kick.** Register the redirect address shown on Sources > Kick in your Kick app (see
     below). The app asks for the scopes `user:read` and `events:subscribe` and uses the PKCE code exchange.
4. Enter the values on Sources > Kick.

The redirect address for Sign in with Kick must match exactly, including the port. With the usual port it is:

```text
http://localhost:8765/api/oauth/source/kick/callback
```

If the app runs on another port, register the address Sources > Kick shows now.

What each connection enables:

- Live status: the app asks Kick's livestreams list about up to 50 channels per request, once a minute,
  after looking each channel up by name once. Kick marks this endpoint as deprecated; it still answers, but a
  future Kick change may need an app update.
- Chat: Kick delivers chat only as webhooks to a public https address, which a program on your own computer
  does not have. To use chat, give the app a public https address that forwards to its local listener
  (port 8788 by default), for example through a tunnel service. Without one, Kick chat is unavailable and the
  app clips from other signals. Kick unsubscribes an app whose webhook keeps failing for more than a day.
  Unverified apps can have at most 1,000 chat subscriptions.
- Capture needs no Kick account. Read the terms warning above.

Limits: Kick's documentation lists no rate limit, so the app makes one batched request per minute and waits
for the time Kick gives if it answers "too many requests". Docs:
[Kick app setup](https://docs.kick.com/getting-started/kick-apps-setup),
[OAuth flow](https://docs.kick.com/getting-started/generating-tokens-oauth2-flow),
[events](https://docs.kick.com/events/introduction).

## YouTube Live

Cost: Google's documentation states no fee for the YouTube Data API at its default quota.

1. In the [Google Cloud console](https://console.cloud.google.com), create a project and enable "YouTube Data
   API v3" for it.
2. Pick one way to connect:
   - **An API key.** Create one in the console and enter it on Sources > YouTube. Not confirmed: whether a
     key alone is enough for every live check.
   - **Sign in with Google.** Create an OAuth client of type "Desktop app", enter its client ID (and secret,
     if shown) and press "Sign in to Google". The app listens on `127.0.0.1` at its own port for the answer;
     Google's desktop-app flow accepts that without registering the address. The scope is
     `youtube.readonly`.
3. While the consent screen is in "Testing", add yourself as a test user. Google then ends the sign-in after
   7 days; sign in again when asked. Posting explains the other consent screen choices.

What each connection enables:

- Live status: the app finds a channel's live video with a search, then follows that video until it ends.
- Chat: the app reads live chat by asking YouTube repeatedly.
- Capture needs no Google account. Read the terms warning above.

Quota: a project gets 10,000 units a day for most requests, plus separate allowances of 100 searches and 100
uploads a day; all reset at midnight Pacific time. To stay inside the 100 searches, the app searches each
channel at most every 14 minutes with one channel, and less often with several, so it can notice a YouTube
stream late. A found stream is then checked every minute at 1 unit. Reading chat costs 5 units per request,
so a long stream can use up the day's units. When quota runs out, the app waits and tries again later.
Raising the quota means applying to Google, which requires its quota audit; Google's other reviews, such as
OAuth verification, can still apply. Docs:
[quota costs](https://developers.google.com/youtube/v3/determine_quota_cost).

If YouTube refuses playback: some networks get "Sign in to confirm you're not a bot" when the app tries to
record. The channel then shows "needs action"; see [Troubleshooting](troubleshooting.md).

## What is free

Live Clipper is free and has no paid tier. Registering developer applications on Twitch, Kick and Google cost
nothing at the time of checking, at their default limits. Destinations are covered on [Posting](posting.md).
