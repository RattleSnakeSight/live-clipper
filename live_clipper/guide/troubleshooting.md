# Troubleshooting

## First checks

- `live-clipper doctor` checks Python, ffmpeg, ffprobe, the caption filters, the data folder, free disk space
  and the port. "Port 8765: in use" is only a problem if you registered that port as a redirect address.
- `live-clipper status` says whether the app is running, on which port, and its update state.
- `live-clipper logs` prints recent log lines (add `-f` to follow them). Secrets are masked in the logs.
- The Status page in the app shows the same, plus service states and the restart count.

## The browser says unauthorized

Open the app with `live-clipper start`. It opens the page with a fresh sign-in link.

## The page says Update pending or Can't check for updates

**Update pending** means a newer version is out. Live Clipper pauses its work until you update; your data
waits. Run `live-clipper update`. **Can't check for updates** means the check got no answer, for example
without internet; the app retries by itself. Both are explained on [Updating](updating.md).

## `live-clipper` is not found

The folder pipx installs programs into is not on your PATH yet. Run `python -m pipx ensurepath`, open a new
terminal, and try again. Meanwhile `python -m live_clipper start` works too.

## The app keeps restarting

Check `live-clipper logs` and the restart list on the Status page, then run `live-clipper doctor`.

## Startup or shutdown cannot verify the running instance

If startup says the instance owns its lock but is not responding, check `live-clipper logs` and try
`live-clipper stop`. If stopping times out, it reports failure and keeps the lock. Update and uninstall also
stop at that point. The command never kills another program just because its process ID or name looks right.

An older installation may have a PID-only lock. If the new copy cannot verify that older instance, stop it
using its original installation first, then retry. Do not delete lock files while an instance might still run.
For the new lock format, the operating system releases ownership after a crash; a leftover record does not
prevent a later start. Start-at-login still requires signing in, and nothing records while the computer sleeps.

If startup reports a surviving worker without its supervisor, use `live-clipper stop`, then start again.
The worker keeps its own lock until it exits, so another worker cannot use the same data folder in the meantime.
If that worker cannot respond to stop, restarting the computer releases its OS locks. Start again after signing in.

## ffmpeg or ffprobe is missing

`doctor` says "not found: install ffmpeg". Install it and run `doctor` again:

- Windows: `winget install Gyan.FFmpeg`
- macOS: `brew install ffmpeg-full`, then add `export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"` to
  `~/.zprofile`
- Linux: `sudo apt install ffmpeg`

If `doctor` says the caption filters are missing, your FFmpeg build lacks libass or freetype. Install one of
the builds above; on macOS the plain `ffmpeg` formula is not enough.

Open a new terminal afterwards so the new program is found. If it is installed but not on your path, you can
point the app at it with the ffmpeg path setting.

## The port is busy

Live Clipper prefers port 8765. If something else uses it, `live-clipper start` picks another free port and
prints the address. That is fine, but a redirect address you registered with Kick or another platform includes
the port, so register the new one shown in the app. To free the usual port, close the other program.

## The disk is full or recording paused

Live Clipper stops starting new clips when free space is low or when it has used the disk cap you set. If
space gets critically low, recordings pause and resume once there is room. It cleans up in this order:

1. Temporary render files older than an hour.
2. Trash past its keep time.
3. Posted clips older than your retention period move to trash.
4. If space is still short, trash is emptied early, then original recordings of posted clips (or whole
   posted clips first, depending on your answer), oldest first.

Clips that are not posted yet are never deleted by this automatic cleanup. If the cap is full of unposted
clips, post or discard some, or raise the cap. The Status page lists what would be cleaned next.

You can also free space yourself. **Reject** on a clip deletes its video files at once. **Clean up now** in
Settings deletes, at once and without the keep time, every fully posted clip and every rejected clip, and old
trash and temporary files. **Preview cleanup** shows the same list with the bytes first: posted clips,
rejected clips, other items, and what is kept. Kept: clips that are waiting, approved, being posted, shared by
another clip, or not decided yet, and any clip that a separate sender program (such as the Metricool sender)
has not finished with. Nothing is posted or re-posted by cleaning up, and the record of what was sent stays.

## Platform problems

Look at the message on the channel's dashboard row or the delivery line first. It names the cause and the
fix. Setup steps for each platform are on [Platforms](platforms.md) and [Posting](posting.md).

| Message or sign | Cause | What to do |
|---|---|---|
| A destination needs a reconnect, or a delivery says Paused | The sign-in expired or was revoked. On YouTube this happens every 7 days while your Google consent screen is in Testing, and when you change your password or remove access. Instagram tokens last 60 days and cannot be refreshed once expired. | Click Connect again (or paste a new Instagram token). Waiting clips continue. |
| A Twitch, Kick or YouTube channel says needs action, credentials rejected | Wrong or expired client ID, secret, key or token. | Fix them on the Sources page, or sign in again. |
| "rate limit" or "too many requests" | The platform said slow down. | Nothing. The app waits for the time given and tries again. |
| YouTube "quota exceeded" | The daily quota is used up (it resets at midnight Pacific time). Chat reading uses a lot of it. | Wait, turn off YouTube chat, or watch fewer channels. |
| YouTube upload limit | 100 uploads a day per project. | Wait for the next day. |
| TikTok posts are private or only drafts | Your app is not audited, or inbox mode is on. | See [Posting](posting.md). Finish drafts in the TikTok app. |
| TikTok `unaudited_client_can_only_post_to_private_accounts` | Unaudited apps can only post privately. | Make the account private, or use drafts. |
| Instagram upload says it needs a public address | Instagram Login cannot take local files. | Set the public video folder address, or use Facebook Login. |
| A YouTube upload is private | The destination's privacy setting, or a rule on Google's side. | Check the destination's settings and YouTube Studio. |
| YouTube "Sign in to confirm you're not a bot" when recording | YouTube refuses playback to this computer's network. | Use a feed address from the creator, or run the app from a network YouTube accepts. |
| Channel stays "waiting" with YouTube | The app searches YouTube rarely to save quota, so it can notice a stream late. | Wait up to 15 minutes, or use a feed address. |
| Kick chat unavailable | Kick sends chat only to a public https address. | Set one, or leave chat off. |
| A delivery says Check the account | The app cannot tell whether an upload landed. | Check the account, then press "Confirm posted" or "Confirm not posted" on the clip's page. |
| No clips appear | The channel is not live, a gated channel's title does not match the format, or no moment passed the format's checks yet. | Check the channel's state on the Dashboard, and see [Monitoring](monitoring.md). |
| Nothing records overnight | The computer slept or was turned off. | See "Keep the computer on" on [Monitoring](monitoring.md). |

## Webhook notifications do not arrive

Check the address in Settings. A failed webhook is logged as a warning and does not stop the app. Browser
notifications only show while the app is open in a tab.

## Still stuck

Run `live-clipper logs`, find the first line with ERROR or warning, and read it. Do not post logs publicly
without reading them first, even though secrets are masked.
