"""Machine-readable setup requirements per destination (rendered by the UI and the docs).

Source: official provider documentation read on AS_OF (see each `docs` URL). Items marked `"verified": False`
could not be confirmed from an official page and must be re-checked before promising them to users.
`{redirect_uri}` placeholders are filled by `requirements_for(provider, port)`.

The Destinations page walks through four steps per destination (developer app keys, sign-in, check the account and
confirm rights, switch on). `methods` lists the ways to do the first step with only the fields each one needs;
`sign_in`, `identity` and `recover` are the words for the next steps and for errors. `routes` says who sends the
clip: this app itself, or an outside service this installation does not include (see METRICOOL_AVAILABLE).
"""
from __future__ import annotations

import copy

from ..sources.guide import (
    GOOGLE_API,
    GOOGLE_AUDIENCE,
    GOOGLE_AUDIENCE_STEPS,
    GOOGLE_CLIENTS,
    GOOGLE_COST,
    GOOGLE_DESKTOP_CLIENT,
    GOOGLE_PROJECT,
)

AS_OF = "2026-11-23"
TEST_NOTICE = "Test destination — nothing is posted"

TIKTOK_PORTAL = {"label": "TikTok for Developers", "url": "https://developers.tiktok.com/"}
META_APPS = {"label": "Meta for Developers: your apps", "url": "https://developers.facebook.com/apps"}
GOOGLE_LINKED_APPS = {"label": "Google Account: linked apps", "url": "https://myaccount.google.com/linkedapps"}
METRICOOL_HELP = {
    "label": "Metricool: how to connect social networks",
    "url": "https://help.metricool.com/how-to-connect-social-media-and-ad-platforms-to-metricool-qrkvn",
}

# Metricool is an outside service that can publish a finished clip instead of this app. It does not ship here: the
# maintainers' own tested setup used Metricool through a separate helper program that is not part of this package.
# A stock installation therefore reports the route as not available and offers nothing to connect for it.
METRICOOL_AVAILABLE = False


def _step(text: str, verified: bool = True, link: dict | None = None) -> dict:
    return {"text": text, "verified": verified, "link": link}


def _field(name: str, label: str, provider_label: str, why: str, where: str, *, secret: bool = False,
           required: bool = True, link: dict | None = None) -> dict:
    return {"name": name, "label": label, "provider_label": provider_label, "why": why, "where": where,
            "secret": secret, "required": required, "link": link}


def _fix(when: str, action: str, match: tuple[str, ...] = (), link: dict | None = None) -> dict:
    """An error the customer can meet and the next thing to do. `match`: text of the app's own status message."""
    return {"when": when, "action": action, "match": list(match), "link": link}


def _routes(network: str, native_title: str, native_needs: list[str]) -> list[dict]:
    return [
        {"id": "native", "available": True, "title": native_title,
         "summary": "This installation posts here itself. The steps below set it up.", "needs": native_needs},
        {"id": "metricool", "available": METRICOOL_AVAILABLE,
         "title": "Through Metricool, an outside scheduling service",
         "summary": "Metricool is a separate online service that schedules and publishes posts to social accounts "
                    f"you connect to it. The maintainers' own tested setup used it to publish finished clips to "
                    f"{network}. That setup relies on an extra helper program that is not part of Live Clipper, so "
                    "this installation cannot hand clips to Metricool and there is nothing to connect or enter here.",
         "needs": [
             f"What it would take: your own Metricool account with your {network} account connected to a brand, and "
             "a helper program that passes each finished clip to Metricool. Live Clipper does not include one: "
             "Metricool's route for programs (its API) is on its paid Advanced and Custom plans only.",
             "What works today: post directly (above), or keep the clip files and upload them to Metricool "
             "yourself. If you upload by hand, leave posting to this destination switched off so the same clip is "
             "not sent twice.",
         ],
         "guide": {"page": "posting", "label": "Guide: Posting, \"Metricool\""}, "link": METRICOOL_HELP},
    ]


STEP_TITLES = {"rights_acknowledgement": "Check the account and confirm your rights", "enable": "Turn posting on"}
SAVE_KEYS_AFTER = ("Stores the values encrypted on this computer. They are never shown again. An optional field "
                   "left empty removes its saved value. Nothing is checked with the provider yet: that happens when "
                   "you sign in, in the next step.")
SECRET_WHY = "Proves the request comes from your app. Stored encrypted on this computer and never shown again."

REQUIREMENTS: dict[str, dict] = {
    "youtube": {
        "label": "YouTube Shorts",
        "what": "Uploads finished clips to your own YouTube channel as Shorts. It posts directly through Google, "
                "with your own Google Cloud project. No other service is involved, and Metricool is not needed.",
        "account": "Any Google account with a YouTube channel (post to your own channel).",
        "free": True,
        "cost": GOOGLE_COST,
        "portal": "Google Cloud",
        "key_note": "The API key from Sources > YouTube only reads public data and cannot upload. Posting always "
                    "needs the Google sign-in set up here, with an OAuth client of type Desktop app.",
        "step_titles": {**STEP_TITLES, "app_credentials": "Create a Desktop app client in Google Cloud",
                        "connect": "Sign in with Google and choose your channel",
                        "rights_acknowledgement": "Check the channel and confirm your rights"},
        "developer_app_steps": [
            _step("Create a Google Cloud project (\"Project Name\", then Create), or use one you already have.",
                  link=GOOGLE_PROJECT),
            _step("Enable \"YouTube Data API v3\" for that project: open its page in the API Library and press "
                  "Enable.", link=GOOGLE_API),
            *GOOGLE_AUDIENCE_STEPS,
            GOOGLE_DESKTOP_CLIENT,
        ],
        "methods": [{
            "id": "oauth", "title": "Desktop app client", "pick": "", "recommended": True, "submit": "credentials",
            "steps": [], "register_redirect": False, "agent": True,
            "fields": [
                _field("client_id", "Client ID", "Client ID",
                       "Tells Google which OAuth client is asking. It ends in .apps.googleusercontent.com and is "
                       "not secret.",
                       "Google Auth platform > Clients > your Desktop client.", link=GOOGLE_CLIENTS),
                _field("client_secret", "Client secret", "Client secret",
                       "Google gives Desktop clients a secret but does not treat it as confidential, and the "
                       "sign-in works without it. Enter it if you copied one; leave it empty otherwise.",
                       "Shown once when the client is created, and in the JSON file you can download then. Later "
                       "the Clients page only offers \"Add Secret\" to make a new one.",
                       secret=True, required=False),
            ],
            "button": "Save keys", "after": SAVE_KEYS_AFTER,
        }],
        "sign_in": {
            "button": "Sign in with Google",
            "summary": "Google's sign-in page opens in a new tab. Choose the Google account, choose the YouTube "
                       "channel if Google asks, and press Allow. Google then sends your browser back to this "
                       "computer and Live Clipper reads which channel it was. Live Clipper never sees your Google "
                       "password, and nothing is uploaded.",
            "asks": ["\"Manage your YouTube videos\" (youtube.upload): needed to upload a clip.",
                     "\"View your YouTube account\" (youtube.readonly): needed to read the channel's name and to "
                     "check that an upload arrived."],
            "callback_why": "After you press Allow, Google sends your browser back to this address on your own "
                            "computer. That is how Live Clipper receives the sign-in. Desktop app clients accept "
                            "it without registering it, so there is nothing to paste into Google.",
        },
        "identity": {
            "noun": "Channel", "open": "Open the channel to check", "switch_label": "Use a different channel",
            "check": "Uploads go to this channel. A Google account can manage several channels and Google decides "
                     "which one a sign-in belongs to, so check the name before you switch posting on.",
            "change": "To choose another channel, disconnect, then sign in again and pick the other Google account "
                      "or channel. If Google does not ask, remove Live Clipper under your Google Account's linked "
                      "apps first, or change the default channel at youtube.com/account_advanced.",
        },
        "recover": [
            _fix("Google shows \"redirect_uri_mismatch\" instead of a sign-in page",
                 "The client is of type Web application. Under Clients, create a new client of type Desktop app, "
                 "save its Client ID in step 1 and sign in again.", ("redirect_uri_mismatch",), GOOGLE_CLIENTS),
            _fix("Google says \"403 access_denied\" or \"Access blocked\"",
                 "The app is in test mode and this Google account is not a listed test user. Add it under Audience "
                 "> Test users, then press the sign-in button again.", link=GOOGLE_AUDIENCE),
            _fix("You pressed Cancel on Google's page (access_denied)",
                 "Nothing was changed. Press the sign-in button again when you are ready.", ("access_denied",)),
            _fix("YouTube Data API v3 is not enabled (accessNotConfigured)",
                 "Open the API's page for the same project, press Enable, wait a few minutes, then press the "
                 "sign-in button again.", ("accessnotconfigured",), GOOGLE_API),
            _fix("The Google account has no YouTube channel (youtubeSignupRequired)",
                 "Create a channel on YouTube with that account, or sign in again with the account that owns your "
                 "channel.", ("no youtube channel",),
                 {"label": "YouTube Help: create a channel", "url": "https://support.google.com/youtube/answer/1646861"}),
            _fix("Google refuses the client (invalid_client)",
                 "Copy the Client ID again. If you have a client secret, enter it too. A client of type Web "
                 "application also causes this: use a Desktop app client.",
                 ("invalid_client", "client_secret is missing"), GOOGLE_CLIENTS),
            _fix("Google says \"client_user_mismatch\"",
                 "The Google project uses the Personal audience, which only lets the account that created it sign "
                 "in. Sign in with that account.", ("client_user_mismatch",)),
            _fix("Step 3 shows the wrong channel",
                 "Press \"Use a different channel\", then sign in again and pick the other account or channel. If "
                 "Google does not ask, remove Live Clipper under linked apps first.", link=GOOGLE_LINKED_APPS),
            _fix("The card says \"Sign in again\" after about a week (invalid_grant)",
                 "With audience External and status Testing, Google ends a sign-in after 7 days. Press the sign-in "
                 "button again; your settings stay as they are.", ("invalid_grant",)),
        ],
        "scopes": ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"],
        "redirect_uri": "http://127.0.0.1:{port}/api/oauth/youtube/callback",
        "redirect_note": "Desktop clients accept any loopback port; PKCE (S256) is always used. Client secret is optional.",
        "verification_audit": [
            _step("Audience \"External\" with status \"Testing\": only listed test users (up to 100), and a sign-in "
                  "expires after 7 days (reconnect weekly). \"In production\": anyone can authorize; unverified apps "
                  "are capped at 100 users. Audience \"Personal\": only the Google account that created the app, no "
                  "expiry, and the choice cannot be changed."),
            _step("Raising the quota needs Google's quota audit; other reviews, such as OAuth verification, can "
                  "still apply."),
            _step("Google's API revision history (2026-11-02) and the videos.insert reference say uploads from "
                  "unverified projects are no longer restricted to private. The app sends privacyStatus explicitly "
                  "(public by default); check your first upload in YouTube Studio."),
        ],
        "quotas": {"summary": "videos.insert has its own bucket: about 100 uploads per day (resets midnight Pacific).",
                   "per_day": 100, "tracked_locally": True},
        "fails_without": {
            "client_id": "Connect cannot start: Google rejects the authorization request.",
            "consent_screen_testing": "Refresh token dies after 7 days: destination becomes needs_reconnect.",
            "scope_youtube.upload": "Upload is refused (403).",
        },
        "tos_rights": "Posting is automated; YouTube developer policies require the user's express consent and final "
                      "control. Third-party streamer content needs the streamer's permission; you confirm that once "
                      "per destination.",
        "notes": ["Shorts: vertical, up to 3 minutes; \"#Shorts\" is added as a hint (not confirmed in Google's docs).",
                  "Since 2026-11-16 Google no longer defaults paidProductPlacementDetails.hasPaidProductPlacement to "
                  "false, so the app sends false explicitly.",
                  "uploadLimitExceeded is the channel's own daily upload limit, separate from the project's quota."],
        "docs": ["https://developers.google.com/youtube/data/guides/authentication",
                 "https://developers.google.com/youtube/v3/guides/uploading_a_video",
                 "https://developers.google.com/identity/protocols/oauth2/native-app",
                 "https://developers.google.com/youtube/v3/determine_quota_cost",
                 "https://developers.google.com/youtube/v3/revision_history",
                 "https://support.google.com/cloud/answer/15549945"],
        "as_of": AS_OF,
    },
    "tiktok": {
        "label": "TikTok",
        "what": "Sends finished clips to your own TikTok account. Until TikTok has audited your developer app, "
                "clips arrive as drafts in your TikTok inbox or as private posts.",
        "account": "A TikTok account you own; developer account at developers.tiktok.com.",
        "free": True,
        "cost": "No fee is stated on TikTok's developer pages.",
        "portal": "TikTok for Developers",
        "step_titles": {**STEP_TITLES, "app_credentials": "Create a TikTok developer app and save its keys",
                        "connect": "Sign in with TikTok"},
        "developer_app_steps": [
            _step("Sign up at TikTok for Developers, click your profile icon, choose \"Manage apps\" and press "
                  "\"Connect an app\".", link=TIKTOK_PORTAL),
            _step("In the app's Products section press \"Add products\" and add Login Kit and Content Posting API."),
            _step("In the Login Kit settings, add the redirect address below as a Redirect URI (loopback "
                  "http://127.0.0.1 with a port is allowed for desktop apps)."),
            _step("Under Scopes press \"Add Scopes\" and add video.upload, which sends drafts to your inbox. Add "
                  "video.publish only for direct posts; TikTok must approve that scope."),
            _step("Enable Direct Post in the Content Posting API product if you want direct posting (otherwise uploads "
                  "go to your TikTok inbox as drafts)."),
            _step("To try the app before TikTok reviews it, switch it to Sandbox and, under Sandbox settings > Target "
                  "users, press \"Add account\" for your own TikTok account. Sandbox cannot post public videos."),
            _step("Open App details > Credentials and copy the Client key and the Client secret."),
            _step("Whether a never-reviewed app can use inbox upload at all is not stated on the pages read.",
                  verified=False),
        ],
        "methods": [{
            "id": "oauth", "title": "TikTok developer app", "pick": "", "recommended": True, "submit": "credentials",
            "steps": [], "register_redirect": True, "agent": True,
            "redirect_why": "After you approve, TikTok sends your browser back to this address on your own computer. "
                            "TikTok only accepts addresses registered in Login Kit, and only on localhost or "
                            "127.0.0.1 with a port.",
            "fields": [
                _field("client_key", "Client key", "Client key", "Tells TikTok which app is asking.",
                       "Manage apps > your app > App details > Credentials.", link=TIKTOK_PORTAL),
                _field("client_secret", "Client secret", "Client secret", SECRET_WHY,
                       "The same Credentials section as the Client key.", secret=True),
            ],
            "button": "Save keys", "after": SAVE_KEYS_AFTER,
        }],
        "sign_in": {
            "button": "Sign in with TikTok",
            "summary": "TikTok's sign-in page opens in a new tab. Log in to the TikTok account that should receive "
                       "the clips and approve. Live Clipper then reads the account's name. Nothing is uploaded.",
            "asks": ["Read your basic profile (user.info.basic): the account's name.",
                     "Send drafts to your inbox (video.upload).",
                     "Post directly to your profile (video.publish): only used in direct mode."],
            "callback_why": "",
        },
        "identity": {
            "noun": "TikTok account", "open": "", "switch_label": "Use a different account",
            "check": "Clips are sent to this account. Check the name before you switch posting on.",
            "change": "To choose another account, disconnect, switch account on tiktok.com in your browser, then "
                      "sign in again. TikTok reports a different account number for every developer app, so after "
                      "you replace your app's keys the same account can look like a different one: posting is then "
                      "switched off until you have looked and switched it on again.",
        },
        "recover": [
            _fix("TikTok shows a redirect error instead of a sign-in page",
                 "The Redirect URI in Login Kit differs from the address in step 1. Copy it again, save the app, "
                 "then press the sign-in button again.", ("redirect_uri",)),
            _fix("You pressed Cancel on TikTok's page (access_denied)",
                 "Nothing was changed. Press the sign-in button again when you are ready.", ("access_denied",)),
            _fix("A direct post fails with scope_not_authorized",
                 "The app has no approved video.publish scope. Use inbox mode (Settings > How clips are sent) or add "
                 "the scope in the TikTok developer portal and sign in again.", ("scope_not_authorized",)),
            _fix("A post fails with unaudited_client_can_only_post_to_private_accounts",
                 "TikTok has not audited your app. Set the TikTok account to private, or use inbox mode so clips "
                 "arrive as drafts.", ("unaudited_client",)),
        ],
        "routes": _routes("TikTok", "Directly from Live Clipper, with your own TikTok developer app", [
            "A TikTok developer account and your own app (steps below).",
            "Until TikTok audits the app: drafts in your TikTok inbox, or private posts only, and the account must "
            "be private for direct posts.",
            "TikTok's guidelines list upload tools for your own accounts as not acceptable for an audit, so plan on "
            "drafts or private posts.",
        ]),
        "scopes": ["user.info.basic", "video.upload", "video.publish"],
        "redirect_uri": "http://127.0.0.1:{port}/api/oauth/tiktok/callback",
        "redirect_note": "Only localhost / 127.0.0.1 hosts; a static path with a port (a wildcard port may be "
                         "registered). PKCE S256 is mandatory and the code challenge is HEX-encoded SHA-256.",
        "verification_audit": [
            _step("Unaudited apps: every direct post is forced to private (SELF_ONLY), the posting account must be "
                  "private, and at most 5 users per 24 h can post."),
            _step("Public posting requires TikTok's audit. TikTok's guidelines list tools that upload to accounts you "
                  "manage and apps that copy content from other platforms as unacceptable: a self-registered "
                  "personal app is unlikely to pass. Plan for private posts and drafts only."),
        ],
        "quotas": {"summary": "TikTok: typically around 15 direct posts a day per creator account, shared by all apps; "
                              "6 upload starts a minute per sign-in. The app counts 15 direct and, as a cautious "
                              "default, 5 inbox uploads a day (TikTok states no inbox number).", "per_day": 15,
                   "pending_drafts_per_day": 5, "tracked_locally": True},
        "fails_without": {
            "client_key": "Connect cannot start.",
            "video.publish": "Direct post fails (scope_not_authorized); use inbox mode.",
            "audit": "Posts are SELF_ONLY (private); a public attempt returns "
                     "unaudited_client_can_only_post_to_private_accounts.",
        },
        "tos_rights": "Original/authentic content intent; copying arbitrary content from other platforms is listed as "
                      "unacceptable, and app review expects apps not to be for private or personal use. Confirm the "
                      "streamer's permission once per destination.",
        "notes": ["Access token lasts 2 hours; the refresh token (180 days) is replaced on every refresh and the app "
                  "stores the new one.", "Inbox uploads finish in the TikTok app (status draft)."],
        "docs": ["https://developers.tiktok.com/doc/getting-started-create-an-app",
                 "https://developers.tiktok.com/doc/login-kit-desktop",
                 "https://developers.tiktok.com/doc/content-posting-api-get-started",
                 "https://developers.tiktok.com/doc/add-a-sandbox",
                 "https://developers.tiktok.com/doc/content-sharing-guidelines",
                 "https://developers.tiktok.com/doc/oauth-user-access-token-management"],
        "as_of": AS_OF,
    },
    "instagram": {
        "label": "Instagram Reels",
        "what": "Publishes finished clips as Reels on your own Instagram professional account. A published Reel is "
                "public at once: Instagram has no draft or private mode.",
        "account": "An Instagram professional account (Business or Creator). Facebook Login also needs a linked "
                   "Facebook Page.",
        "free": True,
        "cost": "No fee is stated on Meta's developer pages.",
        "portal": "Meta for Developers",
        "key_note": "Meta changed this setup. A Meta app created today is set up with \"Instagram API with Unified "
                    "Login\" and can no longer add \"API setup with Instagram login\". Live Clipper's Instagram "
                    "connection was written for the earlier setups and has not been tried with a newly created Meta "
                    "app. The two ways below apply when your Meta app shows \"API setup with Instagram login\".",
        "step_titles": {**STEP_TITLES, "app_credentials": "Create a Meta developer app and connect it",
                        "connect": "Sign in with Instagram"},
        "developer_app_steps": [
            _step("In the Instagram app, switch your account to a professional account (Business or Creator). "
                  "Personal accounts cannot be used, and the account must be public."),
            _step("Open Meta for Developers, press \"Create App\" and choose the use case \"Manage messaging & "
                  "content on Instagram\".", link=META_APPS),
            _step("Keep the app in Development mode and add your own account; no app review is needed "
                  "for your own account (Standard Access)."),
        ],
        "methods": [
            {
                "id": "token", "title": "Paste a token from the Meta dashboard",
                "pick": "Easiest. No redirect address to register.", "recommended": True, "submit": "token",
                "register_redirect": False, "agent": False,
                "steps": [
                    _step("In the app dashboard open Instagram > \"API setup with Instagram login\". Under "
                          "\"Generate access tokens\" press \"Add account\" and log in to your Instagram account. "
                          "Then press \"Generate token\" next to it and copy the token. Meta shows it only once."),
                ],
                "fields": [
                    _field("access_token", "Access token", "Generate token",
                           "Lets Live Clipper publish to that one Instagram account. It lasts 60 days and the app "
                           "renews it while it is still valid. Stored encrypted on this computer.",
                           "Instagram > API setup with Instagram login > Generate access tokens.", secret=True),
                ],
                "button": "Connect with token",
                "after": "Live Clipper asks Instagram which account the token belongs to and shows it in step 3. "
                         "This also completes step 2. Nothing is posted.",
            },
            {
                "id": "oauth", "title": "Sign in with your Meta app", "pick": "If you prefer a sign-in page.",
                "recommended": False, "submit": "credentials", "register_redirect": True, "agent": True,
                "redirect_why": "After you approve, Instagram sends your browser back to this address. It must match "
                                "a registered address exactly. Not confirmed: whether Meta accepts a plain http "
                                "address on 127.0.0.1. If Meta refuses it, paste a token instead.",
                "steps": [
                    _step("In the app dashboard open Instagram > \"API setup with Instagram login\" > \"3. Set up "
                          "Instagram business login\" > \"Business login settings\". Add the redirect address below "
                          "under \"OAuth redirect URIs\" and save."),
                    _step("Copy the \"Instagram app ID\" and the \"Instagram app secret\" from the same place. They "
                          "are not the App ID and App secret under App settings > Basic."),
                ],
                "fields": [
                    _field("client_id", "Instagram app ID", "Instagram app ID", "Tells Instagram which app is asking.",
                           "Instagram > API setup with Instagram login > Business login settings.", link=META_APPS),
                    _field("client_secret", "Instagram app secret", "Instagram app secret", SECRET_WHY,
                           "The same Business login settings as the Instagram app ID.", secret=True),
                ],
                "button": "Save keys", "after": SAVE_KEYS_AFTER,
            },
        ],
        "sign_in": {
            "button": "Sign in with Instagram",
            "summary": "If you pasted a token, this step is already done. Otherwise Instagram's sign-in page opens "
                       "in a new tab: log in to the professional account and allow access. Nothing is posted.",
            "asks": ["See your account's name and type (instagram_business_basic).",
                     "Publish content to it (instagram_business_content_publish)."],
            "callback_why": "",
        },
        "identity": {
            "noun": "Instagram account", "open": "Open the profile to check",
            "switch_label": "Use a different account",
            "check": "Reels are published on this account, publicly. Check the name before you switch posting on.",
            "change": "To choose another account, disconnect, then paste a token generated for the other account or "
                      "sign in again with it.",
        },
        "recover": [
            _fix("Instagram says the token expired (error 190)",
                 "Generate a new token in the Meta dashboard and paste it in step 1. Your settings stay as they "
                 "are.", ("190", "expired")),
            _fix("\"no Facebook Page with a linked Instagram professional account\"",
                 "With Facebook Login the Instagram account must be linked to a Facebook Page you manage. Link it, "
                 "or set Settings > Login type to Instagram Login.", ("no facebook page",)),
            _fix("\"this sign-in manages several Instagram accounts\"",
                 "Enter the number of the account to post to under Settings > Instagram account number, then check "
                 "the connection again.", ("several instagram accounts", "not among this sign-in")),
            _fix("Uploads fail because Instagram cannot fetch the video",
                 "With Instagram Login, Instagram downloads each clip from a public https address. Set Settings > "
                 "Public video folder address, or use Facebook Login for Business.", ("video_base_url",)),
            _fix("You pressed Cancel on the sign-in page (access_denied)",
                 "Nothing was changed. Press the sign-in button again when you are ready.", ("access_denied",)),
        ],
        "routes": _routes("Instagram", "Directly from Live Clipper, with your own Meta developer app", [
            "An Instagram professional account and your own Meta developer app (steps below).",
            "Instagram downloads each clip from a public https address that you provide (Settings > Public video "
            "folder address), unless your Meta app uses Facebook Login for Business with a linked Facebook Page.",
            "Not tried with a Meta app created today: Meta now sets new apps up with Unified Login, which this "
            "version was not written for.",
        ]),
        "scopes": {"instagram_login": ["instagram_business_basic", "instagram_business_content_publish"],
                   "facebook_login": ["instagram_basic", "instagram_content_publish", "pages_show_list",
                                      "pages_read_engagement", "business_management"]},
        "redirect_uri": "http://127.0.0.1:{port}/api/oauth/instagram/callback",
        "redirect_note": "Configurable per destination (setting redirect_uri) because loopback is unverified for Meta.",
        "verification_audit": [
            _step("Own account only: Standard Access, App Review not required. Serving other people's accounts needs "
                  "Advanced Access, which needs App Review."),
        ],
        "quotas": {"summary": "API-published posts per rolling 24 h: Meta's page says 100 in one place and 50 in another; "
                              "the app reads the live value from content_publishing_limit.", "per_day": 50,
                   "tracked_locally": False},
        "fails_without": {
            "facebook_login": "Instagram Login cannot upload local files: set video_base_url to a public https folder "
                              "serving the clips, or use Facebook Login (resumable upload).",
            "token_refresh": "Long-lived tokens last 60 days and refresh only while valid: destination becomes "
                             "needs_reconnect if it lapses.",
        },
        "tos_rights": "Meta Platform Terms: you need the necessary consents for content you post. Confirm the streamer's "
                      "permission once per destination. No private/draft mode exists: a published Reel is public.",
        "notes": ["Reels: MP4 or MOV. Meta's Reels specification page could not be read on the check date, so "
                  "length, frame-rate and size limits are not confirmed here.",
                  "Errors 190 = token expired; 4 and 17 = throttled; 368 = temporarily blocked.",
                  "Facebook Login scope business_management is requested by the app; Meta lists it only for "
                  "Advanced Access, so whether your own account needs it is not confirmed."],
        "docs": ["https://developers.facebook.com/docs/instagram-platform/overview",
                 "https://developers.facebook.com/docs/instagram-platform/content-publishing",
                 "https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/"
                 "business-login",
                 "https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/"
                 "migration-guide",
                 "https://developers.facebook.com/documentation/instagram-platform/app-review"],
        "as_of": AS_OF,
    },
    "test": {
        "label": TEST_NOTICE,
        "account": "None: a local fake provider on this machine.",
        "free": True,
        "developer_app_steps": [],
        "scopes": [],
        "redirect_uri": "",
        "redirect_note": "",
        "verification_audit": [],
        "quotas": {"summary": "none", "tracked_locally": False},
        "fails_without": {},
        "tos_rights": "Nothing leaves this machine; no acknowledgement needed.",
        "notes": ["Never counted as a real post."],
        "docs": [],
        "as_of": AS_OF,
    },
}


def requirements_for(provider: str, port: int | None = None) -> dict:
    r = copy.deepcopy(REQUIREMENTS[provider])
    if port is not None:
        r["redirect_uri"] = r["redirect_uri"].replace("{port}", str(port))
    return r
