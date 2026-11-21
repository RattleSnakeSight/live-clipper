# Platforms

Twitch, Kick and YouTube Live are the places Live Clipper watches. They are sources. To post finished clips,
see Posting.

Facts on this page were checked against each platform's own documentation on 2026-11-20. Platforms change
their rules. Where we could not confirm something, the page says "not confirmed".

## Read this first: monitoring is not permission to record

Connecting a platform lets Live Clipper ask whether a streamer is live and, if you allow it, read their chat.
That is all it grants. It does not give you the right to record a stream, cut clips or post them anywhere.

- Recording is done by Streamlink and ffmpeg, which are separate tools. No platform API hands out the video.
- You need the streamer's permission to record and clip their stream. If the channel is yours, you have it.
- A platform's terms may forbid recording even with the streamer's permission. The terms we read:
  - Kick's terms of service forbid copying content from the service, using automated systems to access it,
    and reaching content by means other than those Kick provides: <https://kick.com/terms-of-service>.
    They do not mention recording by name. The separate Kick developer terms page could not be read.
  - The YouTube developer policies forbid downloading or storing copies of YouTube video without YouTube's
    written approval: <https://developers.google.com/youtube/terms/developer-policies>. The API terms are at
    <https://developers.google.com/youtube/terms/api-services-terms-of-service>.
  - Twitch: read the Twitch Terms of Service at <https://www.twitch.tv/p/legal/terms-of-service/> and the
    developer agreement before you record. We could not read their full text, so we make no claim about them.
- You are responsible for following the terms of each platform you use.

## The three permissions, kept apart

For each platform, Live Clipper treats three things separately. Each can be on or off without the others.

| Permission | What it lets the app do | Without it |
|---|---|---|
| Live status | Ask the platform's official API whether a channel is live, and read its title and category. | The app falls back to the stream probe (below). |
| Chat | Read the chat so busy moments can trigger clips. | The app clips by other signals only, such as speech. |
| Capture | Record the stream with Streamlink and ffmpeg. | Nothing can be recorded. |

The Sources page shows, for each platform, which of these is working and how.

## The stream probe

If you do not connect a platform's official API, the Sources page labels live status as "stream probe (no
official API connected)". For Twitch and Kick the app then asks Streamlink's public lookup whether the channel
is live. It uses no login and no keys. It works, but it is slower to notice changes and it can fail without a
clear reason. For YouTube there is no separate probe: the recorder opens the channel's live page itself and
keeps retrying.

## Twitch

What you need for live status (free to set up; the Twitch pages we read mention no fee):

1. Sign in at <https://dev.twitch.tv/console> and register an application. This is your own developer
   application. You do not share it.
2. Copy its Client ID. Choose one of two ways to connect:
   - Client ID and Client Secret. The app asks Twitch for an app token. There is no sign-in step and no
     redirect address. Keep the secret private.
   - Client ID only, with the application's client type set to Public. The app shows you a code and a Twitch
     page. You type the code there and approve. This is the device sign-in. It needs no secret and no
     redirect address.
3. Paste the values into Sources > Twitch.

Scopes and what each connection enables:

- Live status needs no scope. Either connection works.
- Chat: without signing in, the app reads chat anonymously, read-only. If you use the device sign-in, the app
  asks for the `user:read:chat` scope and reads chat through Twitch's event service instead.
- Capture needs no Twitch account at all, only Streamlink and ffmpeg.

Limits: Twitch gives each application a request allowance that refills every minute. The app asks about up to
100 channels in one request, once a minute. If Twitch answers "too many requests", the app waits and tries
again. Twitch tokens from the device sign-in last about 4 hours and renew themselves. A refresh token that goes
unused for 30 days stops working, and then you sign in again.

If it fails: wrong Client ID or Secret shows a "needs action" message with the fix. Not confirmed: whether
Twitch accepts a local `http://localhost` redirect address. That is why the app uses the device sign-in and
no redirect address.

Terms: <https://www.twitch.tv/p/legal/terms-of-service/>.

## Kick

What you need (the Kick pages we read do not state a cost or an approval step):

1. Turn on two-factor sign-in on your Kick account.
2. Open <https://kick.com/settings/developer> and create an application. Copy its Client ID and Client Secret.
3. Choose one of two ways to connect:
   - Client ID and Client Secret. The app asks Kick for an app token. Nothing to approve.
   - Sign in with Kick. In your Kick app settings, register the redirect address exactly as Sources > Kick
     shows it. It looks like `http://localhost:8765/api/oauth/source/kick/callback`. Kick needs an exact match,
     including the port, and recommends `localhost`. If the app started on a different port, the shown address
     changes, so register the one the page shows now. The app asks for the scopes `user:read` and
     `events:subscribe`. The sign-in uses a secure code exchange (PKCE).
4. Paste the values into Sources > Kick.

What each connection enables:

- Live status: the app asks Kick's public `livestreams` list about up to 50 channels per request, once a
  minute. It looks each channel up by name once to get its numeric ID.
- Chat: Kick delivers chat only as webhooks, and Kick must be able to reach a public https address. A program
  on your own computer is not reachable that way. To use chat, you give the app a public https address that
  forwards to its local listener (port 8788 by default). A tunnel service can do this. Without it, chat is
  unavailable for Kick and the app clips from other signals. Kick stops sending if delivery keeps failing for
  more than a day. Unverified apps can have at most 1,000 chat subscriptions.
- Capture needs no Kick account, only Streamlink and ffmpeg. See the terms warning above.

Limits: Kick's documentation lists no rate limit, so the app keeps to one batched request per interval. If
Kick answers "too many requests", the app waits for the time Kick gives.

Terms: <https://kick.com/terms-of-service>.

## YouTube Live

What you need (free; Google's pages we read mention no fee for this):

1. Open the Google Cloud console, create a project and enable "YouTube Data API v3" for it.
2. Choose one of two ways to connect:
   - An API key. Create one in the console and paste it into Sources > YouTube. Whether a key alone is enough
     for every live check is not confirmed in Google's pages.
   - Sign in with Google. Create an OAuth client of type "Desktop app", paste its client ID (and secret if
     shown), and click Sign in. The redirect address is `http://127.0.0.1:` plus the app's port plus
     `/api/oauth/source/youtube/callback`. Google accepts any port on the loopback address for desktop
     clients, so you do not need to register it. The scope is `youtube.readonly`.
3. Add yourself as a test user on the consent screen while it is in "Testing". In Testing,
   Google expires sign-ins after 7 days for the upload scope. Whether the same applies to the read-only
   scope is not confirmed. If YouTube asks you to sign in again, do so. See Posting for the consent screen
   options.

What each connection enables:

- Live status: the app finds a channel's live video with a search, then follows that video until it ends.
- Chat: the app reads live chat by asking YouTube again and again.
- Capture needs no Google account, only Streamlink and ffmpeg. See the terms warning above.

Limits and quota. Google gives a project 10,000 units a day, which reset at midnight Pacific time. Searches
sit in a small separate allowance of about 100 a day. Because of that, the app searches each channel at most
every 14 minutes with one channel, and less often when you watch several. So the app can notice a YouTube
stream late. A found stream is then checked every minute at 1 unit per check. Reading chat costs 5 units per
request, so a long stream can use up the day's quota. When the quota runs out, the app waits and tries again
later. Raising the quota means asking Google and may need a compliance audit.

If YouTube refuses playback: some networks get "Sign in to confirm you're not a bot" when the app tries to
record a live stream. The app then shows "needs action". See Troubleshooting.

Terms: <https://developers.google.com/youtube/terms/developer-policies>.

## What is free

Live Clipper itself is free. Registering a developer application on Twitch, Kick or Google is free as far as
the pages we read say. Posting accounts are covered in Posting.
