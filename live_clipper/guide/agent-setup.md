# Let your agent set up keys

If Claude Code or Codex installed Live Clipper for you, it can also fill in the keys that Twitch, Kick,
YouTube, TikTok and Instagram need. You stay in charge: you choose the platform, you choose where the helper
looks, and nothing happens until you press **Ask my agent**.

Only Claude Code and Codex can pair. Other agents, such as Qwen Code, can install and start the app, but
then you type the keys in yourself on Sources and Destinations. That always works, with or without an agent.

This only fills in keys. Creating developer applications, signing in, approving access on a platform's
consent screen and accepting terms stay with you, in your browser: see [Platforms](platforms.md) and
[Posting](posting.md) for each step.

## What the helper fills in, and what you do yourself

| | The helper can do it | You do it in your browser |
|---|---|---|
| Create a developer application or a Google Cloud project | No | Yes |
| Copy a Client ID, secret or API key out of a platform's site | No. You put the values in a place you choose first. | Yes |
| Put those values into Live Clipper | Yes, from the place you chose | Or type them in |
| Sign in, pick the account or channel, press Allow | No | Yes |
| Tick the rights acknowledgement and switch posting on | No | Yes |

On Sources and Destinations the helper sits under **Or let my agent fill these in**, next to the fields it
fills. It never fills in a pasted Instagram token, a sign-in or a Metricool value: Live Clipper has no
Metricool field at all (the Metricool route is a sender program that reports in, see [Posting](posting.md)).

## How it works

1. **Pairing.** The agent asks you, then runs `live-clipper agent pair --agent claude` (or `codex`). That pairs
   one small helper with this copy of Live Clipper and its data folder. Settings > Agent setup shows which
   agent is paired.
2. **The helper.** The agent starts `live-clipper agent serve`. It is a plain program, not an AI: it waits for
   your requests and does exactly what is described below, nothing else.
3. **Your request.** On Sources or Destinations open **Or let my agent fill these in** under the fields; on
   the last page of setup open **Let my agent fill in the keys**; or use Settings > Agent setup. Pick where
   the keys are and press **Ask my agent**:
   - **Environment variables**: the helper reads only the names shown, for example `TWITCH_CLIENT_ID` and
     `TWITCH_CLIENT_SECRET`, from the environment it was started in.
   - **A file I choose**: give the full path of a file with `KEY=VALUE` lines or a JSON object. Only the names
     shown are read; everything else in the file is ignored.
   - **A credential manager entry**: give the service name of an entry in your system's credential manager,
     with one item per name shown. This needs the optional `keyring` Python package next to Live Clipper.
4. **The result.** The helper sends the keys straight to Live Clipper on this computer. Live Clipper saves
   them the same way as when you type them: source keys are checked with the platform, destination keys go
   to the encrypted vault. The panel shows **Saved** with the field names. The values are never shown.

## Names the helper looks for

| Platform | Names | The fields they fill |
|---|---|---|
| Twitch (watching) | `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | Client ID and secret |
| Kick (watching) | `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET` | Client ID and secret |
| YouTube (watching) | `YOUTUBE_API_KEY` | API key (public data) |
| YouTube Shorts (posting) | `YOUTUBE_CLIENT_ID`, optional `YOUTUBE_CLIENT_SECRET` | Client ID and Client secret of your Desktop app client |
| TikTok (posting) | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` | Client key and Client secret |
| Instagram Reels (posting) | `INSTAGRAM_CLIENT_ID`, `INSTAGRAM_CLIENT_SECRET` | Instagram app ID and Instagram app secret |

For watching, the helper's keys are checked with the platform as they are saved, so the card says
"Connected". For posting they are only stored: the card says "Keys saved, not signed in" until you sign in
yourself.

## What the agent does not do

- It does not look anywhere else: no other folders, no browser or password stores, no other apps' accounts.
- It does not create developer apps, sign in for you, approve access on a platform's consent screen or accept
  terms. You do those in the browser. [Platforms](platforms.md) and [Posting](posting.md) say where to get
  each key.
- It does not post anything or turn posting on.
- The keys do not pass through the AI chat: the helper reads them and hands them to Live Clipper directly.

## When it does not work

- **Not paired**: ask the agent that installed Live Clipper to pair, or enter the keys yourself.
- **Helper not running**: ask the agent to start it again with `live-clipper agent serve`. This also works
  after the app was closed and reopened, or in a new agent session. A request waits 10 minutes.
- **Keys missing**: the panel lists what was not found and nothing is saved. Add the missing names to that
  place and ask again, or type the keys in yourself.
- **Not accepted**: the platform rejected the keys. Check them in your developer app.

You can always type the keys in yourself on the same page, whatever happened with the agent.

## Cancel or withdraw

- **Cancel** next to a waiting request withdraws it; nothing is saved after that.
- **Settings > Agent setup > Revoke**, or `live-clipper agent unpair`, ends the pairing. Keys already saved
  stay; remove them on Sources or Destinations.
