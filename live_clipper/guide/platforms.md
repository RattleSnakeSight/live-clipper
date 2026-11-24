# Platforms

Live Clipper watches streams on Twitch, Kick and YouTube Live. These are its **sources**, set up on the
**Sources** page under "Watch a stream". It posts to YouTube Shorts, TikTok and Instagram Reels, its
**destinations**; those are on [Posting](posting.md). A feed address from a creator (rtmp, rtmps, srt, rtsp or
an http(s) stream) can be added as a channel too; it needs no keys.

The platform facts and the names of buttons and fields on this page and on Posting were checked against each
platform's own pages on 2026-11-23. Platforms change their sites and rules, and their approval decisions are
their own: nothing here promises that an application is approved, that API access stays free, or that every
account can post. Where a detail could not be confirmed, the page says "not confirmed" and tells you what to
look for.

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

| Platform | What you paste for watching | Where it comes from |
|---|---|---|
| Twitch | Nothing, or a Client ID with a secret, or a Client ID and a code sign-in | [Twitch developer console](https://dev.twitch.tv/console) |
| Kick | Nothing, or a Client ID and Client Secret | [Kick developer settings](https://kick.com/settings/developer) |
| YouTube | Nothing, or an API key, or a Google sign-in | [Google Cloud console](https://console.cloud.google.com) |

## How the Sources page works

Each platform has one card. The card says what the connection is for, what already works without an
account, and shows three lines: Live status, Chat and Capture.

- Open **Connect** on a card and answer "How do you want to watch it?". Only the steps and fields for that
  one way are shown. The first choice, **No account: paste a channel link**, needs nothing.
- Each field says what the platform calls the value, why Live Clipper asks for it, and where to click to
  find it. Links open the platform's own page in a new tab; the card keeps your place, and the way you
  picked is remembered when you come back.
- Where a platform needs an address of your own Live Clipper, the card shows the exact address with a
  **Copy** button and says why the platform needs it.
- **Limits, permissions and uncommon errors** is one click away under each way to connect.

The label next to the platform name means:

| Label | Meaning |
|---|---|
| No account connected | Nothing is saved. Watching works with the limits shown on the card. |
| Connected | The values are saved and the platform accepted them when they were saved. |
| Needs attention | The platform now refuses the saved values. The card says what to do. |

Keys for watching belong to your developer application, not to a channel, so the card shows no account
name. A connection for watching never posts anything.

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

The Sources page shows, for each platform, which of these works and how. Healthy chat does not mean video is
being recorded, and a recording does not mean a clip was made: the channel's page and the Clips page show
those separately.

## The stream probe

Without a platform's official API, the Sources page labels live status as "stream probe (no official API
connected)". For Twitch and Kick the app asks Streamlink's public lookup whether the channel is live, with no
login and no keys. It works, but it notices changes more slowly and can fail without a clear reason. For
YouTube there is no separate probe: the recorder opens the channel's live page itself and keeps retrying.

## Twitch

Cost: Twitch's pages name no fee for registering an application.

Register the application once:

1. Turn on two-factor authentication for your Twitch account (Settings > Security and Privacy). Twitch
   requires it, and a verified e-mail address, before you can register an application.
2. Open the [Twitch developer console](https://dev.twitch.tv/console), choose the **Applications** tab and
   press **Register Your Application**.
3. **Name**: anything no other Twitch application uses. **OAuth Redirect URLs**: type
   `http://localhost:3000` and press **Add**. Twitch asks for one; Live Clipper never uses it.
   **Category**: the closest fit. Press **Create**.
4. Not confirmed on Twitch's registration page: a **Client Type** choice. If the form shows it, choose
   Confidential for a Client ID with a secret, or Public for the code sign-in. A Public application has no
   secret and can only use the code sign-in.

Then pick one way on Sources > Twitch:

| Way | Field in Live Clipper | On Twitch | Why it is needed |
|---|---|---|---|
| Client ID and secret | Client ID | Applications > **Manage** > **Client ID** | Tells Twitch which application is asking. Not secret. |
| Client ID and secret | Client secret | The same page, **New Secret** | Proves the request comes from your application. A new secret cancels the old one. |
| Sign in with a code | Client ID | Applications > **Manage** > **Client ID** | The only value needed. You approve a code on Twitch's page. |

- **Client ID and secret**: press **Save Twitch keys**. The app asks Twitch for an app token, which checks
  the two values. No sign-in page, no redirect address.
- **Sign in with a code**: press **Get a Twitch code**. The card shows a code and a link to Twitch, where you
  type it and approve.

What each connection enables:

- Live status needs no permission; either way works.
- Chat: without a sign-in, the app reads chat anonymously and read-only over Twitch's chat (IRC) interface.
  Twitch's current documentation does not describe anonymous reading, so treat it as working today rather
  than promised. With the code sign-in, the app asks for the `user:read:chat` permission and reads chat
  through EventSub, the method Twitch recommends.
- Capture needs no Twitch account.

Limits: Twitch gives each application a request allowance that refills every minute. The app asks about up to
100 channels in one request, once a minute, and waits when Twitch says "too many requests". Code sign-in
tokens last about 4 hours and the app renews them. If the app could not renew them for 30 days (for example,
because it was not running), get a new code.

If it fails: a wrong Client ID or Secret shows "Needs attention" with the fix. Docs:
[registering an app](https://dev.twitch.tv/docs/authentication/register-app/),
[getting tokens](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/),
[Get Streams](https://dev.twitch.tv/docs/api/reference/streams).

## Kick

Cost and approval: Kick's pages name no fee and no approval step for creating an application.

1. Turn on two-factor authentication in your Kick account settings. Kick requires it for its developer tools.
2. Open your account settings, choose the **Developer** tab
   ([Kick developer settings](https://kick.com/settings/developer)) and create an application. Kick's own
   pages do not list the labels of this form: give the application a name and paste the redirect address
   from Sources > Kick into **Redirect URL**.
3. Copy the **Client ID** and **Client Secret** Kick shows for the application.

The redirect address is an address of your own Live Clipper. Kick asks for one when you create the
application, and for "Sign in with Kick" it must match exactly, including the port. With the usual port it is:

```text
http://localhost:8765/api/oauth/source/kick/callback
```

If the app runs on another port, copy the address Sources > Kick shows now.

| Way | Field in Live Clipper | On Kick | Why it is needed |
|---|---|---|---|
| Client ID and secret | Client ID | Developer tab > your application > Client ID | Tells Kick which application is asking. |
| Client ID and secret | Client secret | The same page, Client Secret | Proves the request comes from your application. |
| Sign in with Kick | Client ID and Client secret | The same two values | You also approve access on Kick's sign-in page. |

- **Client ID and secret**: press **Save Kick keys**. The app asks Kick for an app token, which checks the
  values. Nothing to approve.
- **Sign in with Kick**: only needed if your keys alone are refused for chat on your own channel. Press
  **Sign in to Kick**; Kick asks you to allow `user:read` and `events:subscribe`. If Kick shows a redirect
  error, the Redirect URL in the application differs from the address on the card: copy it again.

What each connection enables:

- Live status: the app asks Kick's livestreams list about up to 50 channels per request, once a minute,
  after looking each channel up by name once. Kick marks this endpoint as deprecated; it still answers, but a
  future Kick change may need an app update.
- Capture needs no Kick account. Read the terms warning above.

### Kick chat (optional)

Kick delivers chat only as web requests (webhooks) to an address on the public internet. A program on your
own computer has none. To use Kick chat:

1. Connect Kick with a Client ID and secret.
2. Start a tunnel service that gives you a public https address and forwards it to the local address shown
   under Sources > Kick > **Kick chat (optional)**. Kick's pages name Cloudflare Tunnel and ngrok. Tunnel
   services have their own accounts, limits and prices.
3. In Kick's Developer tab press **Edit** on your application, switch **Enable Webhooks** on and paste your
   public address, ending in `/webhooks/kick`, into **Webhook URL**.
4. Paste the same public address into Live Clipper and press **Save chat address**. Kick chat starts after
   the app restarts.

With the usual settings the local address is:

```text
http://127.0.0.1:8788/webhooks/kick
```

Only chat messages signed by Kick are accepted there. Without a public address, Kick chat is unavailable and
the app clips from other signals. Kick unsubscribes an app whose webhook keeps failing for more than a day.
Unverified apps can have at most 1,000 chat subscriptions.

Limits: Kick's documentation lists no rate limit, so the app makes one batched request per minute and waits
for the time Kick gives if it answers "too many requests". Not confirmed on Kick's pages: a redirect address
with a port number; their example has none. Docs:
[Kick app setup](https://docs.kick.com/getting-started/kick-apps-setup),
[OAuth flow](https://docs.kick.com/getting-started/generating-tokens-oauth2-flow),
[events](https://docs.kick.com/events/introduction).

## Watch YouTube Live

This section is about watching. Posting Shorts uses a different Google sign-in, described under "Post YouTube
Shorts" on [Posting](posting.md). An API key can never upload.

Cost: Google's YouTube pages describe a daily quota and name no price. Creating a Google Cloud project asks
for a billing account only "as applicable".

**With no account.** Add the channel on the Channels page by pasting its link. The recorder opens the
channel's live page and keeps retrying, so a stream is caught, only less precisely, and live chat is not
read.

**With an API key (the usual choice).** A key lets the app read public YouTube data for your project:
whether a channel is live, and its live chat. It cannot see private data.

1. Create a Google Cloud project: [create a project](https://console.cloud.google.com/projectcreate),
   **Project Name**, then **Create**. Or use one you already have.
2. Enable the API: open
   [YouTube Data API v3 in the API Library](https://console.cloud.google.com/apis/library/youtube.googleapis.com)
   and press **Enable**.
3. Create the key: open [Credentials](https://console.cloud.google.com/apis/credentials), press
   **Create credentials** and choose **API key**. Google now requires at least one API restriction: under
   **API restrictions** choose **Restrict key** and select **YouTube Data API v3** (it is only listed once
   the API is enabled). Press **Create** and copy the key from the "API key created" dialog.
4. Do not add an application restriction of type Websites or IP addresses. The key is used from your own
   computer, and Google does not support localhost or home network addresses there.
5. On Sources > YouTube choose **API key (public data)**, paste the key and press **Save YouTube key**. The
   app makes one small request to check it, then stores it encrypted.

| Field in Live Clipper | On Google Cloud | Why it is needed |
|---|---|---|
| API key | APIs & Services > Credentials, the key's name | Identifies your project when the app reads public YouTube data. |

Not confirmed on Google's pages: whether a key alone can read the live chat of every stream. If chat stays
empty, use the sign-in instead.

**With a Google sign-in (view only).** Instead of a key. Create an OAuth client of type **Desktop app** as
described under "Post YouTube Shorts" on [Posting](posting.md), then choose **Sign in with Google (view
only)** on Sources > YouTube, enter the Client ID (and the secret, if you copied one) and press **Sign in to
Google**. Google asks to let the app "View your YouTube account" (`youtube.readonly`). The answer comes back
to `127.0.0.1` at the app's own port, which Google's desktop-app flow accepts without registering the
address. This sign-in is separate from the one for posting.

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
OAuth verification, can still apply.

If Google refuses the key:

| Message | What to do |
|---|---|
| API key not valid | Copy the key again from Credentials. |
| accessNotConfigured | YouTube Data API v3 is not enabled for the key's project, or the key is restricted to other APIs. Enable it, check the key's API restrictions and wait a few minutes. |
| quotaExceeded | The day's quota is used up. The app tries again later by itself. |

Docs: [authentication](https://developers.google.com/youtube/data/guides/authentication),
[API keys](https://docs.cloud.google.com/docs/authentication/api-keys),
[quota costs](https://developers.google.com/youtube/v3/determine_quota_cost).

If YouTube refuses playback: some networks get "Sign in to confirm you're not a bot" when the app tries to
record. The channel then shows "needs action"; see [Troubleshooting](troubleshooting.md).

## What is free

Live Clipper is free and has no paid tier. Twitch, Kick and Google named no fee for a developer application
at their default limits at the time of checking. Destinations are covered on [Posting](posting.md).
