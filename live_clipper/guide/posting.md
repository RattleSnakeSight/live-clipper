# Posting

Live Clipper can post finished clips to YouTube Shorts, TikTok and Instagram Reels. It posts to your own
accounts only, through a developer application you create yourself. Nothing goes through a service run by
the makers of Live Clipper. Posting is set up on the **Destinations** page under "Post my clips".

The facts and the names of buttons and fields below were checked against each platform's own pages on
2026-11-23. Each destination card in the app shows the same steps next to the fields. Platforms decide
approvals themselves: nothing here promises that your application is approved, that API access stays free,
or that every account can post. Where a detail could not be confirmed, this page says "not confirmed".

## Who posts where

| Destination | Who sends the clip | What you need |
|---|---|---|
| YouTube Shorts | Live Clipper, directly through Google | Your own Google Cloud project and a Google sign-in. |
| TikTok | Live Clipper, directly through TikTok | Your own TikTok developer app. Drafts or private posts until TikTok audits it. |
| Instagram Reels | Live Clipper, directly through Meta | Your own Meta developer app and a professional account. |
| Test destination | Nobody: it only records the post on your computer | Nothing. |

Metricool, an outside scheduling service, is **not part of this installation**. The maintainers' own tested
setup published Instagram and TikTok clips through it with an extra helper program that Live Clipper does not
include. The Instagram and TikTok cards say so and offer nothing to connect for it. Watching streams, the
test destination and YouTube never need Metricool. See "Metricool" below.

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

## Connecting a destination: four steps

Every destination card walks through the same four steps and opens only the next one. Finished steps can be
reopened to change them.

1. **Developer app keys.** The card lists the clicks on the platform's site and shows only the fields this
   way of connecting needs. Each field says what the platform calls the value, why it is needed and where
   to find it. Links open in a new tab and the card keeps your place.
2. **Sign in.** The platform's own sign-in page opens in a new tab. You approve there; Live Clipper never
   sees your password. The answer comes back to an address on your own computer, shown on the card with a
   **Copy** button where the platform needs it registered.
3. **Check the account and confirm your rights.** The card shows the account the platform reported. Check
   that it is the one you mean. Then tick the acknowledgement: you have the right to post this content, and
   for another streamer's content you have their permission. A streamer's permission does not override a
   platform's terms.
4. **Turn posting on.** A switch. Connecting an account never turns it on for you.

Connecting or checking an account never posts anything: the app only asks the platform which account the
sign-in belongs to. Connecting also leaves your posting mode, clip settings, acknowledgement and the
posting switch as they were. One exception protects you: if a new sign-in lands on a different account than
the one posting was switched on for, the app switches posting off for that destination and says so, and
you switch it on again after checking the account. An upload that was still unconfirmed at that moment is
not sent to the new account by itself: its line says "Check the account" and waits for you (see "How retries
and duplicates work").

What the label on a card means:

| Label | Meaning |
|---|---|
| Not connected | Nothing is saved for this destination. |
| Keys saved, not signed in | Your developer app keys are stored. Nobody has checked them with the platform yet. |
| Checking the account | A new sign-in arrived and the app is asking the platform which account it is. |
| Connected as (name) | The platform confirmed the account. This is the only state in which posting can be switched on. |
| Could not verify | The platform refused. The card shows the reason and what to do next. |
| Sign in again | The sign-in expired or was withdrawn. Your settings are kept; press the sign-in button. |

"Connected" says an account is linked. It does not say a clip was posted: for that, see "Follow a clip".

Your agent can fill in the keys of step 1 for you (see [Let your agent set up keys](agent-setup.md)). Steps
2 to 4 are always yours.

## Post YouTube Shorts

Account: any Google account with a YouTube channel; you post to your own channel. Cost: Google's YouTube
pages describe a daily quota and name no price.

An API key is not enough here. The key from Sources > YouTube only reads public data; Google requires a
signed-in user for every upload. Live Clipper never asks for your Google password and does not make a test
upload.

Step 1, on Google Cloud:

1. Choose the project. [Create a Google Cloud project](https://console.cloud.google.com/projectcreate)
   (**Project Name**, then **Create**), or use the one that already holds your API key.
2. Enable the API. Open
   [YouTube Data API v3 in the API Library](https://console.cloud.google.com/apis/library/youtube.googleapis.com)
   and press **Enable**. Without it the sign-in is refused later with `accessNotConfigured`.
3. Set the audience. Open [Google Auth platform](https://console.cloud.google.com/auth/overview). The first
   time, press **Get Started** and enter an app name and your e-mail address. For the audience:
   - **Personal**, if it is offered and only your own Google account will sign in. Google describes
     [Personal](https://support.google.com/cloud/answer/15549945) as limited to the account that created the
     app, with no test users, no 7-day expiry and no unverified-app screen. It cannot be changed later.
   - **External** otherwise. Then open [Audience](https://console.cloud.google.com/auth/audience) and, under
     **Test users**, press **Add users**, enter the Google account you will sign in with, and save. Without
     it Google answers "403 access_denied".
4. Create the client. Open [Clients](https://console.cloud.google.com/auth/clients), press **Create client**,
   choose the application type **Desktop app**, give it a name and press **Create**. Copy the **Client ID**.
   Copy the **Client secret** or press **Download JSON** now: Google shows a secret only when the client is
   created.
5. In Live Clipper open Destinations > YouTube Shorts, paste the values and press **Save keys**.

| Field in Live Clipper | On Google Cloud | Why it is needed |
|---|---|---|
| Client ID | Google Auth platform > Clients > your Desktop client. It ends in `.apps.googleusercontent.com`. | Tells Google which client is asking. Not secret. |
| Client secret (optional) | Shown once when the client is created, and in the downloaded JSON file. | Google gives Desktop clients a secret but does not treat it as confidential; the sign-in works without it. Enter it if you copied one. Saving with the field empty removes a secret saved earlier. |

Step 2, the sign-in. Press **Sign in with Google**. Google's page opens in a new tab and always asks which
account to use. Choose the account, choose the YouTube channel if Google asks, and press **Allow**. Google
asks for two permissions:

- "Manage your YouTube videos" (`youtube.upload`), to upload a clip.
- "View your YouTube account" (`youtube.readonly`), to read the channel's name and to check that an upload
  arrived.

Google then sends your browser back to `http://127.0.0.1` at the app's own port, for example
`http://127.0.0.1:8765/api/oauth/youtube/callback`. That is how the app receives the sign-in; it only works
on your own computer. A Desktop app client accepts this address without registering it, so there is nothing
to paste into Google. The card shows the address so you recognise it.

Step 3, the channel. The card shows the channel Google reported, with a link to open it. A Google account
can manage several channels, and Google decides which one a sign-in belongs to: check the name before you
switch posting on. To use another channel press **Use a different channel**, then sign in again and pick
the other account or channel. If Google does not ask, remove Live Clipper under
[your Google Account's linked apps](https://myaccount.google.com/linkedapps) first, or change your default
channel at youtube.com/account_advanced.

If something goes wrong:

| What you see | What to do |
|---|---|
| Google shows `redirect_uri_mismatch` instead of a sign-in page | The client is of type Web application. Create a client of type Desktop app and save its Client ID. |
| "403 access_denied" or "Access blocked" | The app is in test mode and your Google account is not a listed test user. Add it under Audience > Test users. |
| You pressed Cancel | Nothing was changed. Press **Sign in with Google** again. |
| `accessNotConfigured` | YouTube Data API v3 is not enabled for the client's project. Press Enable, wait a few minutes, sign in again. |
| "this Google account has no YouTube channel" | Create a channel with that account, or sign in with the account that owns your channel. |
| `invalid_client` | Copy the Client ID again; enter the client secret if you have one; make sure the client is a Desktop app. |
| `client_user_mismatch` | The project uses the Personal audience: sign in with the account that created it. |
| The wrong channel in step 3 | Press **Use a different channel** and sign in again. |
| "Sign in again" after about a week | With audience External and status Testing, Google ends a sign-in after 7 days. Sign in again; settings stay. |

Verification and what works before it:

- Audience External with status Testing: only your listed test users (up to 100) can sign in, and Google
  ends the sign-in after 7 days.
- In production: anyone can authorize, but an unverified app is capped at 100 users. For your own use that
  is plenty.
- Raising the quota needs Google's quota audit. Other Google reviews, such as OAuth verification for an app
  used by others, can still apply.
- Public or private: Google's [API revision history](https://developers.google.com/youtube/v3/revision_history)
  (2026-11-02) says uploads from unverified projects are no longer restricted to private. The app sets the
  privacy itself (public by default, changeable in the destination's settings). Check your first upload in
  YouTube Studio.
- Since 2026-11-16 Google no longer assumes "no paid product placement" when a video does not say. The app
  declares it explicitly on every upload.

Limits: 100 uploads a day per project, in their own allowance, reset at midnight Pacific time. The app counts
uploads and waits when the day is full. A channel also has its own daily upload limit, separate from the
project's. Shorts must be square or vertical and up to 3 minutes. The app adds "#Shorts" to the title and
description as a hint; Google's pages do not say it is needed.

Retries: see "How retries and duplicates work" below. If Google says the day's uploads are used up
(`uploadLimitExceeded`), the app waits until the next day. If the sign-in is refused (`invalid_grant`), sign
in again.

Terms: YouTube's [developer policies](https://developers.google.com/youtube/terms/developer-policies) require
your express consent before an automated upload, and that you keep final control. The app posts only after you
set it up and acknowledge your rights, and you can switch to manual mode at any time. Docs:
[authentication](https://developers.google.com/youtube/data/guides/authentication),
[desktop-app sign-in](https://developers.google.com/identity/protocols/oauth2/native-app),
[uploading a video](https://developers.google.com/youtube/v3/guides/uploading_a_video).

## TikTok

Account: a TikTok account you own, plus a [TikTok developer account](https://developers.tiktok.com). Cost:
TikTok's developer pages name no fee.

What to expect: until TikTok has audited your app, clips arrive as drafts in your TikTok inbox or as private
posts. TikTok's guidelines make an audit unlikely for a personal upload tool.

1. Sign up at [TikTok for Developers](https://developers.tiktok.com), click your profile icon, choose
   **Manage apps** and press **Connect an app**.
2. In the app's Products section press **Add products** and add **Login Kit** and **Content Posting API**.
   Without them, sign-in or posting is refused.
3. In the Login Kit settings, register the redirect address exactly as the card shows it, for example
   `http://127.0.0.1:8765/api/oauth/tiktok/callback`. TikTok sends your browser back to it after you approve.
   TikTok allows only `localhost` or `127.0.0.1` with a fixed path and a port (a wildcard port is allowed).
   Without it TikTok rejects the sign-in. Docs: [Login Kit for desktop](https://developers.tiktok.com/doc/login-kit-desktop).
4. Under Scopes press **Add Scopes** and add `video.upload`, which sends drafts to your inbox. Add
   `video.publish` only for direct posts; TikTok must approve that scope.
5. For direct posts, enable Direct Post in the Content Posting API settings. Without it, uploads go to your
   TikTok inbox as drafts that you finish in the TikTok app. Live Clipper starts in inbox (draft) mode.
6. To try the app before TikTok reviews it, switch it to Sandbox and, under Sandbox settings > Target users,
   press **Add account** for your own TikTok account. Sandbox cannot post public videos.
7. Open App details > **Credentials**, copy the values, paste them on Destinations > TikTok and press
   **Save keys**. Then press **Sign in with TikTok** and approve.

| Field in Live Clipper | On TikTok for Developers | Why it is needed |
|---|---|---|
| Client key | Manage apps > your app > App details > Credentials > Client key | Tells TikTok which app is asking. |
| Client secret | The same Credentials section | Proves the request comes from your app. |

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

Account: an Instagram professional account (Business or Creator), set to public. Facebook Login also needs a
Facebook Page linked to it. Cost: Meta's developer pages name no fee.

Read this first. Meta changed its setup: its
[migration guide](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/migration-guide)
says a newly created app cannot set up "Instagram API with Instagram Login" any more and is set up with
"Instagram API with Unified Login" instead. Live Clipper's Instagram connection was written for the earlier
setups (Instagram Login, and Facebook Login for Business) and has **not been tried with a Meta app created
today**. The steps below apply when your Meta app shows "API setup with Instagram login". A published Reel is
public at once: Instagram has no draft or private mode.

1. Switch your Instagram account to a professional account in the Instagram app.
2. Open [Meta for Developers](https://developers.facebook.com/apps), press **Create App** and choose the use
   case "Manage messaging & content on Instagram". Without an app there is nothing to connect.
3. Keep the app in Development mode and add your own Instagram account. For an account you own, Meta's
   [app review page](https://developers.facebook.com/docs/instagram-platform/app-review) says App
   Review is not required (Standard Access). Serving other people's accounts needs Advanced Access, which
   needs App Review.
4. Connect the account in one of two ways, chosen on the card:
   - **Paste a token from the Meta dashboard** (easiest). In the app dashboard open Instagram > "API setup
     with Instagram login". Under "Generate access tokens" press **Add account**, log in, then press
     **Generate token** and copy it. Meta shows it only once. Paste it on Destinations > Instagram Reels and
     press **Connect with token**. It stays in the local vault, and this also completes the sign-in step.
   - **Sign in with your Meta app.** Under "Business login settings" add the redirect address from the card
     to "OAuth redirect URIs", then copy the two values below, press **Save keys** and, in the next step,
     **Sign in with Instagram**. Not confirmed: whether Meta accepts a plain `http://127.0.0.1` redirect. If
     the sign-in fails, paste a token instead.

| Way | Field in Live Clipper | On Meta for Developers | Why it is needed |
|---|---|---|---|
| Token | Access token | Instagram > API setup with Instagram login > Generate access tokens > Generate token | Lets the app publish to that one account for 60 days; the app renews it while valid. |
| Sign-in | Instagram app ID | Instagram > API setup with Instagram login > Business login settings | Tells Instagram which app is asking. Not the App ID under App settings > Basic. |
| Sign-in | Instagram app secret | The same Business login settings | Proves the request comes from your app. |

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
page could not be read, so other limits (length, frame rate, size) are not confirmed here.

Tokens: a long-lived token lasts 60 days and can be renewed only while it is still valid. If it lapses, the
destination says "Sign in again" and you paste a new token. Error 190 means the token expired.

Retries: error codes 4 and 17 mean Instagram is throttling requests, and 368 a temporary block; the app waits.

Terms: Meta's Platform Terms require the necessary consents for content you post. Confirm the streamer's
permission once per destination.

## Metricool

[Metricool](https://metricool.com) is an outside online service, run by a company in Spain, that schedules
and publishes posts to social accounts you connect to it. The maintainers' own tested setup used it to
publish finished clips to Instagram and TikTok publicly, which sidesteps the limits of a personal TikTok or
Meta developer app described above.

**It is not available in this installation.** That setup relies on an extra helper program on the same
computer that passes each finished clip to Metricool. Live Clipper does not include that program, so it
cannot hand clips to Metricool, and the app asks for no Metricool sign-in, key or other value. Whether the
helper is ever packaged is a separate decision.

What you can do today:

- Post directly from Live Clipper with your own developer app (the TikTok and Instagram sections above).
- Keep the clip files and upload them to Metricool, or to the platforms, yourself.
- Use only one of the two for an account. If you upload a clip by hand, leave posting to that destination
  switched off in Live Clipper, so the same clip is not sent twice.

How the tested setup works, for reference:

1. **Your own Metricool account.** You sign in at
   [app.metricool.com](https://app.metricool.com/login) with Google or with an e-mail address and password.
   The helper used a browser profile kept only for this, signed in once by the account's owner. No Metricool
   API key was used; Metricool's API is part of its Advanced and Custom plans only. Each person signs in to
   their own account on their own computer: a browser profile or sign-in is never copied from someone else.
2. **Your brand.** Metricool calls a group of social accounts that belong to one business or identity a
   brand. You switch brands with the dropdown next to the brand name. A brand holds one account per network.
3. **Your Instagram and TikTok accounts.** Click your profile picture, open **Connections**, choose the
   brand, and connect each network. A pop-up of the network opens; you log in there and accept the
   permissions. Instagram must be a professional account (Business or Creator), connected through a
   Facebook Page or with the Instagram login. TikTok accounts can be personal or business. See
   [Metricool's connection guide](https://help.metricool.com/how-to-connect-social-media-and-ad-platforms-to-metricool-qrkvn).
4. **The right handles.** The Connections page shows the name of each connected account. Check that they are
   the accounts you mean before anything is scheduled.
5. **Automatic publishing.** A scheduled post is published by Metricool only when automatic publishing is
   switched on for it. When it is off, Metricool sends you a reminder and shows the post as "Sent", which is
   not a published post. A video sent to Instagram is always a Reel. TikTok offers the visibility choices
   your TikTok account has enabled (Public, Friends, Only me).

Accounts and plans that matter (Metricool's pages, read 2026-11-23): the Free plan has one brand and 20
posts a month and includes Instagram and TikTok. Paid plans start at about 20 US dollars a month; check
[Metricool's pricing](https://metricool.com/pricing/). Videos can be up to 500 MB; TikTok takes 3 seconds to
10 minutes with automatic publishing, Reels 3 seconds to 15 minutes. Metricool advises at least 5 minutes
between TikTok posts.

What leaves your computer on this route: the finished clip's video file, its caption with title and
hashtags, the networks and accounts to post to, the post type, the visibility and the planned time. They go
to Metricool, which says it stores data on servers in the European Union, and from there to Instagram or
TikTok. Recordings, other clips, chat and your settings stay on your computer. Not confirmed: how long
Metricool keeps a scheduled video. See [Metricool's privacy policy](https://metricool.com/privacy-policy/).

When a sign-in ends: Metricool's connection to Instagram lasts 60 days and to TikTok 365 days, and a platform
can end it sooner. Metricool sends an e-mail; open Connections and connect the account again (for Instagram,
the same way as before). Posts scheduled for an account that is still disconnected at their time fail.
Metricool's own website asks for a code by e-mail on a new device. In the tested setup the owner then signs
in again in the helper's browser profile.

What each word means on this route:

| Word | Meaning | Evidence |
|---|---|---|
| Connected | The social account is linked to the brand. Nothing was sent. | The account name on Metricool's Connections page. |
| Scheduled | Metricool accepted a post for a planned time. | The post shows as "Pending" in Metricool's planner. |
| Awaiting confirmation | The post was handed to the network, which has not confirmed it yet. Usual for TikTok. Not published yet. | The status Metricool reports to other programs; its help pages do not explain it. |
| Published | The network confirmed the post. | Metricool's "Published" status together with the link to the post on Instagram or TikTok. |

Only "Published" with the post's link is a public post. Open the link to be sure.

## Destination settings

Each destination card has a **Settings** section. Every field explains itself, and a value the app cannot use
is refused with a message under the field. An empty field (or Default) brings back the built-in behaviour.

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

Four things are separate, and each has its own place in the app: chat is being read (Sources and the
channel's page), video is being recorded (the channel's page), a clip was rendered and selected (the Clips
page), and a post was confirmed by the platform (the clip's page, with a link).

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
| Posted | Public on the platform. The platform confirmed it; where the platform gives one, the line has a link to the post. |
| Posted privately | Posted, but not public (for example TikTok "only me", or a private YouTube upload). |
| Saved as draft | In your TikTok inbox. Finish it in the TikTok app. |
| Checking | The upload may have landed (for example after a network drop). The app checks before anything else. |
| Check the account | The check could not settle it. You say whether the clip is on the platform (see below). |
| Paused | The sign-in expired. Reconnect and it continues. |
| Blocked | The destination is off, or the acknowledgement is not ticked. Nothing is sent. |
| Not allowed | This channel may not post to this destination. |
| Failed | The platform refused it for good, or the retries ran out. The message says why. Fix the cause, then Retry. |
| Cancelled | You cancelled it. |

A post on the test destination also says published. It is marked as a test and is on your computer only.

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
- Connecting, reconnecting or checking an account never retries a delivery that is waiting on "Check the
  account", and never clears what the app remembers about earlier posts.
- With notifications on, you are told when a clip is published.
