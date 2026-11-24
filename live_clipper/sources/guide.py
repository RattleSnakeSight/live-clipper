"""What the Sources page tells a first-time customer about each way to watch a platform (GET /api/sources).

One entry per platform: what a connection is for, what works without one, and one `route` per way to connect. A
route names only the fields it needs; every field says what the provider calls it, why Live Clipper asks for it
and where to click to find it. Portal labels were read on the providers' own pages on AS_OF. A step marked
`verified: False` could not be confirmed there and says what to look for instead.
"""

from __future__ import annotations

import copy

AS_OF = "2026-11-23"

GOOGLE_PROJECT = {"label": "Google Cloud: create a project", "url": "https://console.cloud.google.com/projectcreate"}
GOOGLE_API = {
    "label": "YouTube Data API v3 in the API Library",
    "url": "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
}
GOOGLE_CREDENTIALS = {"label": "Google Cloud: Credentials", "url": "https://console.cloud.google.com/apis/credentials"}
GOOGLE_AUDIENCE = {"label": "Google Auth platform: Audience", "url": "https://console.cloud.google.com/auth/audience"}
GOOGLE_CLIENTS = {"label": "Google Auth platform: Clients", "url": "https://console.cloud.google.com/auth/clients"}
TWITCH_CONSOLE = {"label": "Twitch developer console", "url": "https://dev.twitch.tv/console"}
KICK_DEVELOPER = {"label": "Kick developer settings", "url": "https://kick.com/settings/developer"}

# Shared with publish/requirements.py: the posting sign-in uses the same Google Cloud pages.
GOOGLE_AUDIENCE_STEPS = [
    {"text": "Open Google Auth platform. The first time, press \"Get Started\" and fill in an app name and your "
             "e-mail address. For the audience choose \"Personal\" if it is offered and only your own Google account "
             "will sign in: no test users, no 7-day expiry, but the choice cannot be changed later. Otherwise choose "
             "\"External\".",
     "link": {"label": "Google Auth platform", "url": "https://console.cloud.google.com/auth/overview"},
     "verified": True},
    {"text": "Only for External: open Audience and, under \"Test users\", press \"Add users\", enter the Google "
             "account you will sign in with and save. Without it Google answers \"403 access_denied\".",
     "link": GOOGLE_AUDIENCE, "verified": True},
]
GOOGLE_DESKTOP_CLIENT = {
    "text": "Open Clients, press \"Create client\", choose the application type \"Desktop app\", give it a name and "
            "press Create. Copy the Client ID. Copy the Client secret or press \"Download JSON\" now: Google shows a "
            "secret only when the client is created.",
    "link": GOOGLE_CLIENTS, "verified": True,
}

NO_FEE = "No fee is stated on {who}'s pages for a developer application."
GOOGLE_COST = ("Google's YouTube pages describe a daily quota and name no price. Creating a project asks for a "
               "billing account only \"as applicable\".")
APP_KEYS_IDENTITY = "These keys belong to your developer app, not to a channel, so there is no account name to show."


def _step(text: str, link: dict | None = None, verified: bool = True) -> dict:
    return {"text": text, "link": link, "verified": verified}


def _field(name: str, label: str, provider_label: str, why: str, where: str, *, secret: bool = False,
           required: bool = True, link: dict | None = None) -> dict:
    return {"name": name, "label": label, "provider_label": provider_label, "why": why, "where": where,
            "secret": secret, "required": required, "link": link}


def _no_keys(pick: str, summary: str) -> dict:
    return {"id": "none", "method": None, "title": "No account: paste a channel link", "pick": pick,
            "summary": summary, "needs": "None.", "cost": "Free.", "portal": "", "steps": [], "fields": [],
            "callback": None, "button": "Add a channel", "after": "", "agent": False, "more": []}


_TWITCH_REGISTER = [
    _step("Turn on two-factor authentication for your Twitch account (Settings > Security and Privacy). Twitch "
          "requires it before it lets you register an application.",
          {"label": "Twitch security settings", "url": "https://www.twitch.tv/settings/security"}),
    _step("Open the developer console, choose the Applications tab and press \"Register Your Application\".",
          TWITCH_CONSOLE),
    _step("Name: anything that no other Twitch application uses. OAuth Redirect URLs: type http://localhost:3000 "
          "and press Add. Twitch asks for one; this way of connecting never uses it. Category: the closest fit. "
          "Then press Create."),
]
_CLIENT_ID_TWITCH = _field(
    "client_id", "Client ID", "Client ID", "Tells Twitch which application is asking. It is not secret.",
    "Applications tab > Manage (next to your application) > Client ID.", link=TWITCH_CONSOLE)

_KICK_CREATE = [
    _step("Turn on two-factor authentication in your Kick account settings. Kick requires it for its developer "
          "tools."),
    _step("Open your Kick account settings, choose the Developer tab and create an application. Kick's own pages "
          "do not list the form's labels: give it a name and paste the redirect address shown below into "
          "\"Redirect URL\".", KICK_DEVELOPER, verified=False),
    _step("Copy the Client ID and the Client Secret that Kick shows for the application."),
]
_KICK_FIELDS = [
    _field("client_id", "Client ID", "Client ID", "Tells Kick which application is asking.",
           "Account settings > Developer tab > your application.", link=KICK_DEVELOPER),
    _field("client_secret", "Client secret", "Client Secret",
           "Proves the request comes from your application. Stored encrypted on this computer and never shown again.",
           "The same page as the Client ID.", secret=True),
]

GUIDE: dict[str, dict] = {
    "twitch": {
        "about": {
            "what": "Tells Live Clipper when a Twitch channel is live and reads its chat, so busy moments can become "
                    "clips. Nothing is ever posted to Twitch.",
            "without": "No account is needed to start: live status comes from the slower stream probe, chat is read "
                       "anonymously and recording works. Keys add precise live status.",
        },
        "routes": [
            _no_keys("Start here. You can add keys later.",
                     "Add the channel on the Channels page. Live status comes from the stream probe and chat is read "
                     "anonymously and read-only."),
            {
                "id": "credentials", "method": "credentials", "title": "Client ID and secret",
                "pick": "Precise live status from Twitch's official API. No sign-in page.",
                "summary": "You register your own free Twitch application and paste its two values. Live Clipper "
                           "uses them to ask Twitch whether your channels are live.",
                "needs": "A Twitch account with two-factor authentication turned on.",
                "cost": NO_FEE.format(who="Twitch"), "portal": "the Twitch developer console",
                "steps": [
                    *_TWITCH_REGISTER,
                    _step("If the form shows \"Client Type\", choose Confidential: only that type has a secret. "
                          "Twitch's registration page does not describe this field.", verified=False),
                    _step("Back on the Applications tab, press Manage next to your application. Copy the Client ID, "
                          "press \"New Secret\" and copy the secret right away."),
                ],
                "fields": [
                    _CLIENT_ID_TWITCH,
                    _field("client_secret", "Client secret", "",
                           "Proves the request comes from your application. Stored encrypted on this computer and "
                           "never shown again.",
                           "On the same Manage page, press \"New Secret\". Making a new secret cancels the old one.",
                           secret=True),
                ],
                "callback": None, "button": "Save Twitch keys", "agent": True,
                "after": "Live Clipper asks Twitch for an app token with these two values. That checks them. No "
                         "sign-in page opens and nothing is posted.",
                "more": [
                    {"title": "Limits", "items": [
                        "Twitch gives each application a request allowance that refills every minute. The app asks "
                        "about up to 100 channels in one request, once a minute, and waits when Twitch says so."]},
                    {"title": "If Twitch refuses the keys", "items": [
                        "\"invalid client\": the Client ID is not the one on the Manage page. Copy it again.",
                        "\"invalid client secret\": press \"New Secret\" and paste the new one. The old secret stops "
                        "working when a new one is made."]},
                ],
            },
            {
                "id": "device", "method": "device", "title": "Sign in with a code",
                "pick": "Also reads chat as you, through the method Twitch recommends. No secret to store.",
                "summary": "You register a Twitch application without a secret, then approve a short code on "
                           "Twitch's own page.",
                "needs": "A Twitch account with two-factor authentication turned on.",
                "cost": NO_FEE.format(who="Twitch"), "portal": "the Twitch developer console",
                "steps": [
                    *_TWITCH_REGISTER,
                    _step("If the form shows \"Client Type\", choose Public: a public application signs in with a "
                          "code and has no secret. Twitch's registration page does not describe this field.",
                          verified=False),
                    _step("Back on the Applications tab, press Manage next to your application and copy the "
                          "Client ID."),
                ],
                "fields": [_CLIENT_ID_TWITCH],
                "callback": None, "button": "Get a Twitch code", "agent": False,
                "after": "Shows a code and a link to Twitch, where you type the code and approve reading chat "
                         "(permission user:read:chat). This page updates when you are done. Nothing is posted.",
                "more": [
                    {"title": "Good to know", "items": [
                        "The sign-in lasts about 4 hours and the app renews it by itself. If the app did not run for "
                        "30 days, get a new code.",
                        "An application of type Public can only use this code sign-in. Client ID and secret needs a "
                        "Confidential application."]},
                ],
            },
        ],
    },
    "kick": {
        "about": {
            "what": "Tells Live Clipper when a Kick channel is live and, with a public address, reads its chat. "
                    "Nothing is ever posted to Kick.",
            "without": "No account is needed to start: live status comes from the slower stream probe and recording "
                       "works. Kick chat needs keys and a public address.",
        },
        "routes": [
            _no_keys("Start here. You can add keys later.",
                     "Add the channel on the Channels page. Live status comes from the stream probe. Kick chat stays "
                     "off, so clips come from other signals such as speech."),
            {
                "id": "credentials", "method": "credentials", "title": "Client ID and secret",
                "pick": "Precise live status from Kick's official API. Nothing to approve.",
                "summary": "You create your own Kick application and paste its two values.",
                "needs": "A Kick account with two-factor authentication turned on.",
                "cost": NO_FEE.format(who="Kick"), "portal": "Kick",
                "steps": _KICK_CREATE, "fields": _KICK_FIELDS,
                "callback": {
                    "title": "Redirect address for Kick's form",
                    "why": "Kick's form asks for a Redirect URL when you create the application. Use this address of "
                           "your own Live Clipper, so signing in with Kick also works later.",
                },
                "button": "Save Kick keys", "agent": True,
                "after": "Live Clipper asks Kick for an app token with these two values. That checks them. Nothing "
                         "to approve and nothing is posted.",
                "more": [
                    {"title": "Limits", "items": [
                        "Kick's pages list no request limit. The app asks about up to 50 channels in one request, "
                        "once a minute, and waits when Kick says so.",
                        "Kick marks the livestreams list the app uses as deprecated. It still answers; a future Kick "
                        "change may need an app update."]},
                ],
            },
            {
                "id": "oauth", "method": "oauth", "title": "Sign in with Kick",
                "pick": "Only if your app keys alone are refused for chat on your own channel.",
                "summary": "Same Kick application, but you also approve access on Kick's sign-in page.",
                "needs": "A Kick account with two-factor authentication turned on.",
                "cost": NO_FEE.format(who="Kick"), "portal": "Kick",
                "steps": [
                    *_KICK_CREATE,
                    _step("Check that \"Redirect URL\" in the application matches the address below exactly, "
                          "including the port number. Kick refuses the sign-in otherwise."),
                ],
                "fields": _KICK_FIELDS,
                "callback": {
                    "title": "Redirect address to register in Kick",
                    "why": "After you approve, Kick sends your browser back to this address on your own computer so "
                           "Live Clipper can finish the sign-in. Kick only accepts an exact match.",
                },
                "button": "Sign in to Kick", "agent": False,
                "after": "Opens Kick's sign-in page in a new tab. Kick asks you to allow reading your user "
                         "information and subscribing to events (user:read, events:subscribe). Nothing is posted.",
                "more": [
                    {"title": "If Kick shows an error", "items": [
                        "A redirect error means the Redirect URL in the Kick application differs from the address "
                        "above. Copy it again, save the application, then press the button again.",
                        "Kick's examples show a localhost address without a port. This app's address has a port; "
                        "that form is not confirmed on Kick's pages."]},
                ],
            },
        ],
        "chat_setup": {
            "summary": "Kick delivers chat only as web requests (webhooks) to an address on the public internet. A "
                       "program on your own computer has none, so Kick chat needs a tunnel service that gives you a "
                       "public https address and forwards it here. Tunnel services have their own accounts, limits "
                       "and prices. Without one, Kick chat stays off and clips come from other signals.",
            "steps": [
                _step("Connect Kick with a Client ID and secret first (above)."),
                _step("Start a tunnel service (Kick's pages name Cloudflare Tunnel and ngrok) and point it at the "
                      "local address below."),
                _step("In Kick's Developer tab press Edit on your application, switch \"Enable Webhooks\" on and "
                      "paste your public address, ending in /webhooks/kick, into \"Webhook URL\".", KICK_DEVELOPER),
                _step("Paste the same public address in the field below and save. Kick chat starts after Live "
                      "Clipper restarts."),
            ],
            "listener_why": "Kick cannot reach this address itself. Your tunnel forwards its public https address to "
                            "it. Only chat messages signed by Kick are accepted there.",
            "field_help": "The public https address of your tunnel, ending in /webhooks/kick. The same address goes "
                          "into \"Webhook URL\" in your Kick application. Leave empty to turn Kick chat off.",
        },
    },
    "youtube": {
        "about": {
            "what": "Tells Live Clipper when a YouTube channel is live and reads its live chat. This is only for "
                    "watching: posting Shorts is set up separately under Destinations.",
            "without": "No account is needed to start: paste a channel link and the recorder keeps checking the "
                       "channel's live page. An API key adds precise live status and live chat.",
        },
        "routes": [
            _no_keys("Start here. You can add a key later.",
                     "Add the channel on the Channels page. The recorder opens the channel's live page and keeps "
                     "retrying, so a stream is caught without keys, only less precisely. Live chat is not read."),
            {
                "id": "api_key", "method": "credentials", "title": "API key (public data)",
                "pick": "Adds precise live status and live chat. The usual choice for watching.",
                "summary": "An API key lets the app read public YouTube data for your Google Cloud project. It "
                           "cannot see private data and it cannot upload: posting Shorts needs the Google sign-in "
                           "on Destinations > YouTube Shorts.",
                "needs": "Any Google account. No YouTube channel is needed for watching.",
                "cost": GOOGLE_COST,
                "portal": "Google Cloud",
                "steps": [
                    _step("Create a Google Cloud project (\"Project Name\", then Create), or use one you already "
                          "have.", GOOGLE_PROJECT),
                    _step("Enable \"YouTube Data API v3\" for that project: open its page in the API Library and "
                          "press Enable.", GOOGLE_API),
                    _step("Open APIs & Services > Credentials, press \"Create credentials\" and choose \"API key\". "
                          "Google now asks for at least one API restriction: under \"API restrictions\" choose "
                          "\"Restrict key\" and select \"YouTube Data API v3\" (it is only listed once the API is "
                          "enabled). Press Create and copy the key.", GOOGLE_CREDENTIALS),
                    _step("Do not add an application restriction of type Websites or IP addresses: the key is used "
                          "from this computer, and Google does not support localhost or home addresses there."),
                ],
                "fields": [
                    _field("api_key", "API key", "API key",
                           "Identifies your Google Cloud project when the app reads public YouTube data. Stored "
                           "encrypted on this computer.",
                           "APIs & Services > Credentials: the dialog \"API key created\" shows it. Later, open the key by its name.",
                           secret=True, link=GOOGLE_CREDENTIALS),
                ],
                "callback": None, "button": "Save YouTube key", "agent": True,
                "after": "Live Clipper makes one small request to check the key, then stores it encrypted. Nothing "
                         "is posted.",
                "more": [
                    {"title": "Quota", "items": [
                        "A project gets 10,000 units a day for most requests, plus separate allowances of 100 "
                        "searches and 100 uploads a day. All reset at midnight Pacific time.",
                        "To stay inside 100 searches, the app looks for a new live stream on each channel at most "
                        "every 14 minutes with one channel, and less often with several. A found stream is then "
                        "checked every minute.",
                        "Reading live chat costs 5 units per request, so a long stream can use up the day.",
                        "Not confirmed on Google's pages: whether a key alone can read the live chat of every "
                        "stream. If chat stays empty, use the Google sign-in instead."]},
                    {"title": "If Google refuses the key", "items": [
                        "\"API key not valid\": copy the key again from Credentials.",
                        "\"accessNotConfigured\": YouTube Data API v3 is not enabled for the key's project, or the "
                        "key is restricted to other APIs. Press Enable on its API Library page, check the key's API "
                        "restrictions and wait a few minutes.",
                        "\"quotaExceeded\": the day's quota is used up. The app tries again later by itself."]},
                ],
            },
            {
                "id": "oauth", "method": "oauth", "title": "Sign in with Google (view only)",
                "pick": "Instead of an API key. Asks only to view your YouTube account.",
                "summary": "You create a Google OAuth client of type Desktop app and sign in. The app gets view-only "
                           "access. This sign-in is separate from the one for posting Shorts.",
                "needs": "A Google account.",
                "cost": GOOGLE_COST,
                "portal": "Google Cloud",
                "steps": [
                    _step("Create or pick a Google Cloud project.", GOOGLE_PROJECT),
                    _step("Enable \"YouTube Data API v3\" for it.", GOOGLE_API),
                    *GOOGLE_AUDIENCE_STEPS,
                    GOOGLE_DESKTOP_CLIENT,
                ],
                "fields": [
                    _field("client_id", "Client ID", "Client ID",
                           "Tells Google which OAuth client is asking. It ends in .apps.googleusercontent.com.",
                           "Google Auth platform > Clients > your Desktop client.", link=GOOGLE_CLIENTS),
                    _field("client_secret", "Client secret", "Client secret",
                           "Google gives Desktop clients a secret but does not treat it as confidential. Enter it "
                           "if you have it; leave it empty otherwise.",
                           "Shown once when the client is created, and in the JSON file you can download then.",
                           secret=True, required=False),
                ],
                "callback": {
                    "title": "Where Google sends the answer",
                    "why": "After you approve, Google sends your browser back to this address on your own computer. "
                           "Desktop app clients accept it without registering it, so there is nothing to paste "
                           "into Google. It is shown so you recognise it in the address bar.",
                },
                "button": "Sign in to Google", "agent": False,
                "after": "Opens Google's sign-in page in a new tab. Google asks to let the app view your YouTube "
                         "account (youtube.readonly). Nothing is posted.",
                "more": [
                    {"title": "Good to know", "items": [
                        "With audience External and status Testing, Google ends the sign-in after 7 days. Sign in again "
                        "when Sources says so.",
                        "\"redirect_uri_mismatch\" on Google's page means the client is of type Web application. "
                        "Create a client of type Desktop app instead."]},
                ],
            },
        ],
    },
}


def how_connected(platform: str, creds: dict) -> tuple[str, str]:
    """(route id, the customer's words) for the way of connecting the saved values belong to. No way of connecting
    for watching tells the app which account or channel it is, so the words say why no name is shown."""
    signed_in = " The app does not ask which account signed in, so no name is shown."
    if platform == "youtube":
        if creds.get("api_key"):
            return "api_key", ("Connected with an API key, checked with Google when it was saved. A key belongs to "
                               "your Google Cloud project, not to a channel, so there is no account name to show.")
        return "oauth", "Signed in with Google (view only)." + signed_in
    if creds.get("refresh_token") and not (platform == "twitch" and creds.get("client_secret")):
        return ("device", "Signed in with a code." + signed_in) if platform == "twitch" else (
            "oauth", "Signed in with Kick." + signed_in)
    return "credentials", ("Connected with a Client ID and secret, checked when they were saved. " + APP_KEYS_IDENTITY)


def for_platform(platform: str) -> dict:
    return copy.deepcopy(GUIDE[platform])
