# Posting

Live Clipper can post finished clips to YouTube Shorts, TikTok and Instagram Reels. It posts to your own
accounts only, through a developer application you create yourself. Nothing goes through a service run by
the makers of Live Clipper.

The facts below were checked against each platform's own documentation on 2026-11-22. Each destination card
in the app shows the same setup steps. Platforms decide approvals themselves: nothing here promises that your
application is approved, that API access stays free, or that every account can post. Where a detail could
not be confirmed, this page says "not confirmed".

## Two modes

- **Automatic** is the default. A finished clip posts to every destination that is connected, switched on
  and acknowledged (you ticked that you have the right to post). Until one is, nothing is posted.
- **Manual** holds each finished clip under "Waiting for your approval" on the Clips page. Review it, edit it
  if you like (see [Editing](editing.md)), then press **Post to** and the destination. Nothing leaves your
  computer until you press it.

Change the mode at any time under Destinations > Posting mode or in Settings. Clips that already wait keep
waiting. Use automatic mode only for channels you own or have permission to post from. For a first test,
manual mode with the test destination (below) posts nothing; see [First run](first-run.md).

Each channel can also be limited to some destinations ("Where its clips may be posted" on the channel's page).
A channel with none ticked uses every destination you switched on.

Storage cleanup never deletes clips that are queued, held, uploading or not posted yet.

## Before you connect anything

For every real destination you tick one acknowledgement: you have the right to post this content, and for
another streamer's content you have their permission. Until you tick it, the app sends nothing to that
destination, even in automatic mode. A streamer's permission does not override a platform's terms.

Connecting an account takes a few steps on the platform's website, listed below in order, with what fails if
you skip a step. Your agent can fill in the keys for you (see [Let your agent set up keys](agent-setup.md)),
but the sign-in and approval steps are yours.

## YouTube Shorts

Account: any Google account with a YouTube channel; you post to your own channel. Cost: free at the default
quota.

1. In the [Google Cloud console](https://console.cloud.google.com), create a project. Without it there is
   nothing to connect.
2. Enable "YouTube Data API v3" for the project. Without it every request is refused.
3. Set up the OAuth consent screen. Choose External and add your own Google account as a test user, or choose
   Personal mode if the console offers it. Google describes
   [Personal mode](https://support.google.com/cloud/answer/15549945) as skipping verification, the user cap
   and the 7-day expiry, allowing only the Google account that created the app, and as a choice you cannot
   change later. The console's exact wording is not confirmed.
4. Create an OAuth client of type "Desktop app". Without it Google rejects the sign-in. Google treats desktop
   clients as public, so the client secret is optional; the app always uses PKCE.
5. Register nothing for the redirect: the app receives the answer on `127.0.0.1` at its own port, which
   Google's [desktop-app flow](https://developers.google.com/identity/protocols/oauth2/native-app) allows.
6. In Live Clipper open Destinations > YouTube Shorts, enter the client ID (and the secret, if you have one),
   press Connect and approve in the browser.

Scopes: `youtube.upload` to upload and `youtube.readonly` to check the result and find an upload that may have
landed (both under `https://www.googleapis.com/auth/`). Without `youtube.upload` the upload is refused
(error 403).

Verification and what works before it:

- Consent screen in Testing: only your listed test users can sign in, and Google ends the sign-in after 7
  days. The destination then says it needs a reconnect: press Connect again.
- Consent screen in production: anyone can authorize, but an unverified app is capped at 100 users. For your
  own use that is plenty.
- Raising the quota needs Google's quota audit. Other Google reviews, such as OAuth verification for an app
  used by others, can still apply.
- Public or private: Google's [API revision history](https://developers.google.com/youtube/v3/revision_history)
  (2026-11-02) says uploads from unverified projects are no longer restricted to private. The app sets the
  privacy itself (public by default, changeable in the destination's settings). Check your first upload in
  YouTube Studio.
- Since 2026-11-16 Google no longer assumes "no paid product placement" when a video does not say. The app
  declares it explicitly on every upload.

Limits: 100 uploads a day per project, in their own allowance, reset at midnight Pacific time. The app counts
uploads and waits when the day is full. Shorts must be square or vertical and up to 3 minutes. The app adds
"#Shorts" to the title and description as a hint; Google's pages do not say it is needed.

Retries: see "How retries and duplicates work" below. If Google says the day's uploads are used up
(`uploadLimitExceeded`), the app waits until the next day. If the sign-in is refused (`invalid_grant`),
reconnect.

Terms: YouTube's [developer policies](https://developers.google.com/youtube/terms/developer-policies) require
your express consent before an automated upload, and that you keep final control. The app posts only after you
set it up and acknowledge your rights, and you can switch to manual mode at any time. Docs:
[quota costs](https://developers.google.com/youtube/v3/determine_quota_cost),
[uploading a video](https://developers.google.com/youtube/v3/guides/uploading_a_video).

## TikTok

Account: a TikTok account you own, plus a [TikTok developer account](https://developers.tiktok.com). Cost:
free.

1. Create a developer account. Without it there is no app to connect.
2. Create an app in the TikTok developer portal and add Login Kit and the Content Posting API. Without them,
   sign-in or posting is refused.
3. In the Content Posting API settings, turn on Direct Post if you want to post directly. Without it, uploads
   go to your TikTok inbox as drafts that you finish in the TikTok app. Live Clipper starts in inbox (draft)
   mode.
4. Under Login Kit for desktop, register the redirect address exactly as the app shows it, for example
   `http://127.0.0.1:8765/api/oauth/tiktok/callback`. TikTok allows only `localhost` or `127.0.0.1` with a
   fixed path and a port (a wildcard port is allowed). Without it TikTok rejects the sign-in.
   Docs: [Login Kit for desktop](https://developers.tiktok.com/doc/login-kit-desktop).
5. Enter the client key and client secret on Destinations > TikTok, press Connect and approve. The app uses
   the PKCE variant TikTok requires.

Scopes: `user.info.basic`, `video.upload` (inbox drafts) and `video.publish` (direct posts). Without
`video.publish`, direct posts fail with `scope_not_authorized`; use inbox mode instead.

Audit and what works before it:

- An app TikTok has not audited can only post privately: every direct post is "only me", the posting account
  must be private, and at most 5 users can post through the app per 24 hours. A public attempt returns
  `unaudited_client_can_only_post_to_private_accounts`.
- Public posting needs TikTok's audit. TikTok's
  [content sharing guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines) list a utility
  tool for uploading to accounts you manage, and apps that copy content from other platforms, as not
  acceptable, and the [app review guidelines](https://developers.tiktok.com/doc/app-review-guidelines) say
  apps must not be for private or personal use. An app you register for yourself is therefore unlikely to
  pass. Plan on private posts and drafts.
- Not confirmed: whether an app that was never reviewed can use inbox upload. The pages read do not say.

Limits: TikTok says direct posting is "typically around 15 posts per day" per creator account, shared by all
apps. Each sign-in can start at most 6 uploads a minute. The app counts uploads per day itself: 15 in direct
mode and, as a cautious default, 5 in inbox mode (TikTok's current pages state no inbox number). Change the
number under the destination's settings.

Tokens: the access token lasts 2 hours. The refresh token lasts 180 days and is replaced at each renewal; the
app saves the new one.

Retries: the app retries errors TikTok marks as temporary. It does not retry `auth_removed`, spam-risk errors
or a banned account. For format, length or frame-rate errors, fix the clip first.

Terms: TikTok expects original content you have the right to post. Confirm the streamer's permission once per
destination.

## Instagram Reels

Account: an Instagram professional account (Business or Creator). Facebook Login also needs a Facebook Page
linked to it. Cost: free.

1. Switch your Instagram account to a professional account in the Instagram app.
2. Create an app at [Meta for Developers](https://developers.facebook.com). Choose the Business type and add
   Instagram. Without it there is nothing to connect.
3. Keep the app in Development mode and add your own Instagram account as a role user. For an account you own,
   Meta's [app review page](https://developers.facebook.com/docs/instagram-platform/app-review) says App
   Review is not required (Standard Access). Serving other people's accounts needs Advanced Access, which
   needs App Review.
4. Connect the account in one of two ways:
   - **Token paste** (easiest). In the app dashboard, open Instagram API setup and press "Generate token" for
     your account. Paste the long-lived token on Destinations > Instagram Reels. It stays in the local vault.
   - **Sign-in.** Enter the app ID and secret and register the redirect address (you can change it in the
     destination's settings). Not confirmed: whether Meta accepts a plain `http://127.0.0.1` redirect.
     Facebook Login insists on https except for localhost in Development mode. If sign-in fails, use token
     paste.

Scopes:

- Instagram Login: `instagram_business_basic` and `instagram_business_content_publish`.
- Facebook Login: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`
  and `business_management`. Meta's pages list the last one only for Advanced Access; whether it is needed for
  your own account is not confirmed.

Where the video comes from (this matters):

- With Instagram Login, Instagram cannot take a file from your computer. It downloads the clip from a public
  https address. Set the destination's public video folder address (`video_base_url`) to a public https folder
  that serves your clips. Without it, uploads fail with a message saying so.
- With Facebook Login, the app uploads the file itself in pieces, so no public address is needed.

Limits: Meta's [content publishing page](https://developers.facebook.com/docs/instagram-platform/content-publishing)
says 100 posts per rolling 24 hours in one place and 50 in another. The app asks Instagram for the current
number before posting. Reels must be MP4 or MOV; the app's clips are MP4. Meta's full Reels specification
page could not be read this time, so other limits (length, frame rate, size) are not confirmed here.

Tokens: a long-lived token lasts 60 days and can be renewed only while it is still valid. If it lapses, the
destination says it needs a reconnect and you paste a new token. Error 190 means the token expired.

Retries: error codes 4 and 17 mean Instagram is throttling requests, and 368 a temporary block; the app waits.

Terms: Meta's Platform Terms require the necessary consents for content you post. Confirm the streamer's
permission once per destination. Instagram has no private or draft mode: a published Reel is public.

## Destination settings

Each destination card has a Settings form. Every field explains itself, and a value the app cannot use is
refused with a message under the field. An empty field (or Default) brings back the built-in behaviour.

| Destination | Settings |
|---|---|
| YouTube Shorts | Who can watch uploads (public, link only or private), category number, extra tags, uploads per day. |
| TikTok | Inbox drafts or direct posting, who can watch direct posts, whether your app passed TikTok's audit, comments, duets and stitches off, uploads per day. |
| Instagram Reels | Login type (Instagram Login or Facebook Login for Business), public video folder address, show in the profile feed, account number to pin. |
| All three | Redirect address override, only for providers that refuse the default loopback address. |

## Test destination

On Destinations, switch on **Use the test destination** to try the whole flow without posting anywhere. The
card is titled "Test destination: nothing is posted". It is a stand-in on your computer: no account, no
developer application and no acknowledgement. Test deliveries never count as real posts. Use it to check your
posting mode, your titles and the "Where it was posted" list on each clip's page.

## Follow a clip

- The **Dashboard** lists what waits on you: clips waiting to be posted, posts that failed in the last 24
  hours (with "See what failed") and parts of the app that did not start.
- The **Clips** page has the review queue on top and the library below. Filter the library by channel or
  state: Cutting, Rendering, Ready, Held for review, Not selected, Failed, Discarded or Expired.
- A clip's page shows each destination on its own line under "Where it was posted", with the state below and
  a Retry or Cancel button where it applies.

| Shown as | Meaning |
|---|---|
| Waiting for you | Manual mode: press Post to send it. |
| Waiting for captions | Posting starts when the captions are ready. |
| Queued, Uploading, Processing | On its way. Processing means the platform is still working on it. |
| Posted | Public on the platform. |
| Posted privately | Posted, but not public (for example TikTok "only me", or a private YouTube upload). |
| Saved as draft | In your TikTok inbox. Finish it in the TikTok app. |
| Checking | The upload may have landed (for example after a network drop). The app checks before anything else. |
| Check the account | The check could not settle it. You say whether the clip is on the platform (see below). |
| Paused | The sign-in expired. Reconnect and it continues. |
| Blocked | The destination is off, or the acknowledgement is not ticked. Nothing is sent. |
| Not allowed | This channel may not post to this destination. |
| Failed | The platform refused it for good, or the retries ran out. The message says why. Fix the cause, then Retry. |
| Cancelled | You cancelled it. |

## How retries and duplicates work

- The app makes up to 6 attempts. The first wait is about 10 seconds and doubles after each attempt, up to 15
  minutes, with some randomness. When a platform says how long to wait, the app waits that long.
- A temporary error or a network drop is retried. A permanent refusal, such as a missing scope, is not.
- A sign-in that stopped working pauses the destination instead of failing the clip. After you reconnect, the
  clip continues.
- The app does not post the same clip twice by accident. It saves the platform's ID for each upload (and a
  marker on YouTube) before it goes on. If it cannot tell whether an earlier attempt landed, it asks the
  platform first. If that is still unclear, the line says "Check the account" and shows **Confirm posted** and
  **Confirm not posted**. Look at the account before you press either. Confirm posted can take the post's
  address, which then shows as the Open post link; Confirm not posted lets the app upload again.
- A manual Retry on a failed delivery also checks the platform before it uploads.
- With notifications on, you are told when a clip is published.
