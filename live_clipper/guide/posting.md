# Posting

Live Clipper can post finished clips to YouTube Shorts, TikTok and Instagram Reels. It posts to your own
accounts only, with a developer application that you create yourself. Nothing goes through a service run by the
makers of Live Clipper.

Facts here come from each platform's own documentation, read on 2026-11-20. The same facts are built into the
app and shown next to each destination, so the two always agree. Where we could not confirm a detail, this
page says "not confirmed".

## Two modes

- Automatic. A clip posts to every connected, enabled destination as soon as it is ready. A destination
  counts only when you have connected the account, switched it on and ticked the box that says you have the
  right to post. This is the default, in the setup wizard and in the posting engine: until you connect and
  turn on a destination, nothing is posted. Change the mode at any time under Destinations > Posting mode or
  Settings > Posting. Clips already waiting stay waiting.
- Manual. Each finished clip is held under "Waiting for your approval" on the Clips page. Review it, edit it
  if you like (see Editing), then click "Post to" and the destination. Nothing leaves your computer until you
  click.

Use automatic mode only for channels you own or have permission to post from.

You can also limit which destinations each channel posts to. A channel with no limit uses all of them.

Storage cleanup never deletes clips that are queued, held or uploading, or clips that are not posted yet.

## Before you connect anything

For every real destination you tick one acknowledgement: you have the right to post this content, and for
another streamer's content you have their permission. Until you tick it, the app sends nothing to that
destination, even in automatic mode. Platform rules also matter: a streamer's permission does not override a
platform's terms.

Connecting an account takes a few steps on the platform's website. They are listed below, destination by
destination. Do the steps in order. Each destination also says what fails if you skip a step.

## YouTube Shorts

Account: any Google account with a YouTube channel. You post to your own channel. Cost: free.

1. Sign in to Google Cloud at <https://console.cloud.google.com> and create a project. Fails without it:
   there is nothing to connect.
2. Enable "YouTube Data API v3" for the project. Fails without it: every request is refused.
3. Set up the OAuth consent screen. Choose External. Add your own Google account as a test user, or choose
   Personal mode if the console offers it. We could not read the console's click-by-click pages, so the exact
   wording is not confirmed. Google describes Personal mode as skipping verification, the user cap and the
   7-day expiry, and as allowing only the Google account that created the app. You choose it once and cannot
   change it. Details: <https://support.google.com/cloud/answer/15549945>.
4. Create an OAuth client of type "Desktop app". Fails without it: Google rejects the sign-in request. Google
   treats desktop clients as public, so the client secret is optional. The app always uses the secure code
   exchange (PKCE).
5. Redirect address: Google accepts any port on the loopback address for desktop clients, so you register
   nothing. The app uses `http://127.0.0.1:PORT/api/oauth/youtube/callback`, where PORT is the app's port.
   Docs: <https://developers.google.com/identity/protocols/oauth2/native-app>.
6. In Live Clipper open Destinations > YouTube Shorts, paste the client ID (and the secret if you have one), click
   Connect and approve in the browser.

Scopes the app asks for: `https://www.googleapis.com/auth/youtube.upload` (to upload) and
`https://www.googleapis.com/auth/youtube.readonly` (to check the result and to find an upload that may have
landed). Fails without `youtube.upload`: the upload is refused (error 403).

Verification, review and what works before it:

- Consent screen in Testing: only the test users you listed can sign in, and Google expires the sign-in after 7
  days. The destination then shows "needs reconnect". Click Connect again each week.
- Consent screen In production: anyone can authorize, but an unverified app is capped at 100 users. For your
  own use that is plenty.
- A compliance audit is only needed to raise the quota.
- Private or public: Google's developer notes dated 2026-11-02 say uploads from unverified projects are no
  longer forced to private. YouTube's help page still says they are locked private. We could not reconcile
  the two. The app always states the privacy setting itself (public by default). After the first upload,
  check the result in YouTube Studio. If the video shows as locked private, that is the older rule: post it
  from YouTube Studio instead. This is not confirmed either way.
- Since 2026-11-16 the app sends "no paid product placement" explicitly, as Google now requires.

Limits: about 100 uploads a day per project. This is a separate allowance for uploads, and it resets at
midnight Pacific time. The app counts uploads itself and waits when the day is full. Shorts must be vertical
and up to 3 minutes. The app adds "#Shorts" to the title and description as a hint. Google's pages do not
confirm that this is needed.

Retries and rate limits: see "How retries and duplicates work" below. Google says to retry server errors and
network errors with growing waits and to resume the same upload, and the app does so. If Google says you are
out of uploads for the day (`uploadLimitExceeded`), the app stops until the next day. When the sign-in is
refused (`invalid_grant`), you reconnect.

Terms: YouTube's developer policies require your clear consent before an automated upload and that you keep
final control. The app posts only after you set it up and acknowledge your rights, and you can switch to manual
mode at any time. Policies: <https://developers.google.com/youtube/terms/developer-policies>. Quota and
upload docs:
<https://developers.google.com/youtube/v3/determine_quota_cost> and
<https://developers.google.com/youtube/v3/guides/uploading_a_video>.

## TikTok

Account: a TikTok account you own, plus a developer account at <https://developers.tiktok.com>. Cost: free.

1. Create a developer account. TikTok's docs advise an organization account for real products. Fails without
   it: there is no app to connect.
2. Create an app in the TikTok developer portal. Add two products: Login Kit and Content Posting API.
   Fails without them: sign-in or posting is refused.
3. In the Content Posting API settings, turn on Direct Post if you want to post directly. Without it, uploads
   go to your TikTok inbox as drafts and you finish them in the TikTok app. Live Clipper starts in the inbox
   (draft) mode.
4. Register the redirect address under Login Kit for desktop. TikTok allows only `localhost` or
   `127.0.0.1`, a fixed path, and a port (a wildcard port is allowed). Register it exactly as the app shows
   it: `http://127.0.0.1:PORT/api/oauth/tiktok/callback`. Fails without it: TikTok rejects the sign-in.
   Docs: <https://developers.tiktok.com/doc/login-kit-desktop>.
5. Copy the client key and client secret into Live Clipper (Destinations > TikTok), click Connect and approve. The
   app uses the secure code exchange that TikTok requires, in TikTok's own variant.

Scopes: `user.info.basic`, `video.upload` (inbox drafts) and `video.publish` (direct posts). Fails without
`video.publish`: direct posts fail with `scope_not_authorized`. Use inbox mode instead.

Verification, audit and what works before it:

- An app that TikTok has not audited can only post privately. Every direct post is forced to "only me", the
  account may need to be private, and only about 5 users can post per 24 hours. A public attempt returns
  `unaudited_client_can_only_post_to_private_accounts`.
- Public posting needs TikTok's audit. TikTok's guidelines list tools that upload to accounts you manage, and
  apps that copy content from other platforms, as unacceptable. An app that you register for yourself is
  therefore unlikely to pass. Plan on private posts and drafts only.
  Guidelines: <https://developers.tiktok.com/doc/content-sharing-guidelines>.
- App review takes several days to two weeks. TikTok's review rules also say apps must not be for private or
  personal use (<https://developers.tiktok.com/doc/app-review-guidelines>). Until an app is approved it can
  only use the sandbox, and the sandbox does not offer public posting.
- Not confirmed: whether an app that was never reviewed can use inbox upload at all. The pages we read do
  not say.

Limits: about 15 direct posts a day per creator account, and 5 waiting inbox drafts per 24 hours. TikTok also
limits each user token to 6 starts a minute and 30 status checks a minute. The app counts the daily limits
itself. Inbox uploads end up as "draft": open the TikTok notification to finish.

Tokens: the access token lasts 2 hours. The refresh token lasts 180 days and TikTok gives you a new one every
time. The app saves the new one each time.

Retries: the app retries errors TikTok marks as temporary. It does not retry `auth_removed`, spam-risk
errors or a banned account. For format, length or frame-rate errors, fix the clip first.

Terms: TikTok expects original content that you have the right to post. Copying content from other platforms
is listed as unacceptable. Confirm the streamer's permission once per destination. We could not read TikTok's
full developer terms page.

## Instagram Reels

Account: an Instagram professional account (Business or Creator). Facebook Login also needs a Facebook Page
linked to it. Cost: free.

1. Switch your Instagram account to a professional account in the Instagram app (Business or Creator).
2. Create a developer application at <https://developers.facebook.com>. Choose the Business type and add
   Instagram. Fails without it: there is nothing to connect.
3. Keep the application in Development mode and add your own Instagram account as a role user. For your own
   account, Meta says App Review is not required (Standard Access). Serving other people's accounts needs
   Advanced Access, which needs App Review and Business Verification. Docs:
   <https://developers.facebook.com/documentation/instagram-platform/app-review>.
4. Connect the account in one of two ways:
   - Token paste (easiest). In the application dashboard, open Instagram API setup and click "Generate token"
     for your account. Paste the long-lived token into Live Clipper (Destinations > Instagram Reels). The token stays in
     the local vault.
   - Sign-in. Enter the application ID and secret and register the redirect address. The address is
     configurable in the app. Not confirmed: whether Meta accepts a plain `http://127.0.0.1` redirect.
     Facebook Login insists on https, and a 2018 notice allowed plain http for localhost only in Development
     mode. If sign-in fails, use token paste.

Scopes:

- Instagram Login: `instagram_business_basic` and `instagram_business_content_publish`.
- Facebook Login: `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
  `pages_read_engagement` and `business_management`. The app asks for the last one, but the Meta pages we read
  do not confirm that it is needed.

Where the video comes from (this matters):

- With Instagram Login, Instagram cannot take a file from your computer. It needs a public https address that
  serves the clip. In the destination settings, set `video_base_url` to a public https folder holding your
  clips. Without it, uploads fail with a message saying so.
- With Facebook Login, the app uploads the file directly in pieces, so no public address is needed.

Limits: Meta's page says 100 posts per rolling 24 hours in one place and 50 in another. The app asks Instagram
for the live number and does not keep its own count. Reels must be MP4 or MOV, 3 seconds to 15 minutes, 23 to
60 frames per second, and at most 300 MB. We took part of that list from a search summary of Meta's page, so
treat it as not fully confirmed.

Tokens: a long-lived token lasts 60 days and can be refreshed only while it is still valid. If it lapses, the
destination shows "needs reconnect" and you paste a new token. Error 190 means the token expired or was
revoked.

Retries: error codes 4, 17 and 368 mean Instagram is throttling you, and the app waits. Codes 32 and 613 are
probably the same but are not confirmed.

Terms: the Meta Platform Terms say you need the necessary consents for content you post. Confirm the
streamer's permission once per destination. Instagram has no private or draft mode. A published Reel is
public.

## Test destination

Turn on test mode to try everything without posting anywhere. The app creates a destination labelled "Test
destination — nothing is posted". Turn it on under Destinations > Use the test destination. It is a local stand-in on your computer. It needs no account, no developer
application and no acknowledgement. Test deliveries are never counted as real posts. Use it to check your
mode, your titles and the "Where it was posted" list on each clip's page.

## What a delivery can say

Each clip going to each destination has its own line under "Where it was posted" on the clip's page, with a state:

| State | Meaning |
|---|---|
| held | Manual mode: waiting for you to click "Post to". |
| queued, uploading, processing | On its way. Processing means the platform is still working on it. |
| published | Posted publicly. |
| private | Posted, but not public (for example TikTok "only me", or a private YouTube upload). |
| draft | In your TikTok inbox. Finish it in the TikTok app. |
| uncertain | The app cannot tell whether an upload landed, for example after a network drop. It checks before it does anything else. |
| needs_confirmation | The check could not settle it. You say whether the clip is on the platform (see below). |
| paused | The sign-in expired. Reconnect and it continues. |
| blocked | You turned the destination off, or have not ticked the acknowledgement. Nothing is sent. |
| disallowed | A rule stopped it, such as a channel that may not post to this destination. |
| failed | The platform refused it for good, or the retries ran out. The message says why. Fix the cause, then Retry. |
| cancelled | You cancelled it. |

## How retries and duplicates work

- The app makes up to 6 attempts. The first wait is about 10 seconds. It doubles after each attempt, up to
  15 minutes, with some randomness. When a platform tells the app how long to wait, the app waits that long.
- A temporary error or a network drop is retried. A permanent refusal, such as a missing scope, is not.
- A sign-in that no longer works pauses the destination instead of failing the clip. After you reconnect, the
  clip continues.
- The app never posts the same clip twice by accident. It saves the platform's own ID for each upload (and a
  marker on YouTube) before it goes on. If it cannot tell whether an earlier attempt landed, it asks the
  platform first. If that is still unclear, the delivery goes to needs_confirmation and shows two buttons:
  "Confirm posted" and "Confirm not posted", which lets the app upload again.
  Check the account yourself before you press either.
- Manual Retry on a failed delivery also checks the platform before it uploads.
- You get a notification when a clip is published, if notifications are on.
