# Let your agent set up keys

If an AI coding agent (Claude Code or Codex) installed Live Clipper for you, it can also fill in the keys
that Twitch, Kick, YouTube, TikTok and Instagram need. You stay in charge: you choose the platform, you
choose where the agent looks, and nothing happens until you press **Ask my agent**.

## How it works

1. **Pairing.** The agent asks you, then runs `live-clipper agent pair --agent claude` (or `codex`). That pairs
   one small helper with this copy of Live Clipper and its data folder. Settings > Agent setup shows which
   agent is paired.
2. **The helper.** The agent starts `live-clipper agent serve`. It is a plain program, not an AI: it waits for
   your requests and does exactly what is described below, nothing else.
3. **Your request.** On Sources, Destinations, Settings > Agent setup, or the last page of setup, open
   **Let your agent set this up**, pick where the keys are and press **Ask my agent**:
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

| Platform | Names |
|---|---|
| Twitch (watching) | `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` |
| Kick (watching) | `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET` |
| YouTube (watching) | `YOUTUBE_API_KEY` |
| YouTube Shorts (posting) | `YOUTUBE_CLIENT_ID`, optional `YOUTUBE_CLIENT_SECRET` |
| TikTok (posting) | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` |
| Instagram Reels (posting) | `INSTAGRAM_CLIENT_ID`, `INSTAGRAM_CLIENT_SECRET` |

## What the agent does not do

- It does not look anywhere else: no other folders, no browser or password stores, no other apps' accounts.
- It does not create developer apps, sign in for you, approve access on a platform's consent screen or accept
  terms. You do those in the browser. The guide pages Platforms and Posting say where to get each key.
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
