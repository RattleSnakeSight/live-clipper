"""Machine-readable setup requirements per destination (rendered by the UI and the docs).

Source: official provider documentation read on AS_OF (see each `docs` URL). Items marked `"verified": False`
could not be confirmed from an official page and must be re-checked before promising them to users.
`{redirect_uri}` placeholders are filled by `requirements_for(provider, port)`.
"""
from __future__ import annotations

import copy

AS_OF = "2026-11-22"
TEST_NOTICE = "Test destination — nothing is posted"


def _step(text: str, verified: bool = True) -> dict:
    return {"text": text, "verified": verified}


REQUIREMENTS: dict[str, dict] = {
    "youtube": {
        "label": "YouTube Shorts",
        "account": "Any Google account with a YouTube channel (post to your own channel).",
        "free": True,
        "developer_app_steps": [
            _step("Sign in to Google Cloud and create a project."),
            _step("Enable \"YouTube Data API v3\" for it."),
            _step("Set up the OAuth consent screen (External). Add yourself as a test user, or choose Personal mode "
                  "if offered (console wording not verified).", verified=False),
            _step("Create an OAuth client of type \"Desktop app\"."),
            _step("Paste the client ID (and the client secret, if shown) into the app, then click Connect and approve "
                  "in the browser."),
        ],
        "scopes": ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"],
        "redirect_uri": "http://127.0.0.1:{port}/api/oauth/youtube/callback",
        "redirect_note": "Desktop clients accept any loopback port; PKCE (S256) is always used. Client secret is optional.",
        "verification_audit": [
            _step("Consent screen in \"Testing\": only listed test users, and refresh tokens expire after 7 days "
                  "(reconnect weekly). \"In production\": anyone can authorize; unverified apps are capped at 100 users."),
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
                  "false, so the app sends false explicitly."],
        "docs": ["https://developers.google.com/youtube/v3/guides/uploading_a_video",
                 "https://developers.google.com/identity/protocols/oauth2/native-app",
                 "https://developers.google.com/youtube/v3/determine_quota_cost",
                 "https://developers.google.com/youtube/v3/revision_history",
                 "https://support.google.com/cloud/answer/15549945"],
        "as_of": AS_OF,
    },
    "tiktok": {
        "label": "TikTok",
        "account": "A TikTok account you own; developer account at developers.tiktok.com.",
        "free": True,
        "developer_app_steps": [
            _step("Create an app in the TikTok developer portal and add the products Login Kit and Content Posting API."),
            _step("Enable Direct Post in the Content Posting API product if you want direct posting (otherwise uploads "
                  "go to your TikTok inbox as drafts)."),
            _step("Register the redirect URI below under Login Kit for desktop (loopback http://127.0.0.1 is allowed)."),
            _step("Paste client key and client secret into the app, click Connect and approve."),
            _step("Whether a never-reviewed app can use inbox upload at all is not stated on the pages read.",
                  verified=False),
        ],
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
        "docs": ["https://developers.tiktok.com/doc/login-kit-desktop",
                 "https://developers.tiktok.com/doc/content-posting-api-get-started",
                 "https://developers.tiktok.com/doc/content-sharing-guidelines",
                 "https://developers.tiktok.com/doc/oauth-user-access-token-management"],
        "as_of": AS_OF,
    },
    "instagram": {
        "label": "Instagram Reels",
        "account": "An Instagram professional account (Business or Creator). Facebook Login also needs a linked "
                   "Facebook Page.",
        "free": True,
        "developer_app_steps": [
            _step("Create a Meta developer app of type Business and add Instagram."),
            _step("Easiest: in the app dashboard (Instagram API setup) click \"Generate token\" for your account and paste "
                  "the long-lived token into the app's token form (stored only in the local vault)."),
            _step("Or use OAuth: enter app id/secret in the app and register the redirect URI. Meta's acceptance of "
                  "http://127.0.0.1 redirects is not verified; Facebook Login enforces HTTPS (localhost over HTTP only in "
                  "development mode, 2018 notice). Use the configurable redirect or the token form.", verified=False),
            _step("Keep the app in Development mode and add your own account as a role user; no app review is needed "
                  "for your own account (Standard Access)."),
        ],
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
        "docs": ["https://developers.facebook.com/docs/instagram-platform/content-publishing",
                 "https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/"
                 "business-login",
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
