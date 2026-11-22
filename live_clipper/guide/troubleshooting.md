# Troubleshooting

## First checks

- `live-clipper doctor` checks Python, ffmpeg, ffprobe, the data folder, free disk space and the port.
- `live-clipper status` says whether the app is running.
- `live-clipper logs` prints recent log lines (add `-f` to follow them). Secrets are masked in the logs.
- The Status page in the app shows the same, plus service states and the restart count.

## The browser says unauthorized

Open the app with `live-clipper start`. It opens the page with a fresh sign-in link.

## The app keeps restarting

Check `live-clipper logs` and the restart list on the Status page, then run `live-clipper doctor`.

## ffmpeg or ffprobe is missing

`doctor` says "not found: install ffmpeg". Install it and run `doctor` again:

- Windows: `winget install Gyan.FFmpeg`
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg`

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

Clips that are not posted yet are never deleted. If the cap is full of unposted clips, post or discard some,
or raise the cap. The Status page lists what would be cleaned next.

## Platform problems

Look at the message on the channel's dashboard row or the delivery line first. It names the cause and the fix.

| Message or sign | Cause | What to do |
|---|---|---|
| "needs reconnect" on a posting destination | The sign-in expired or was revoked. On YouTube this happens every 7 days while your Google consent screen is in Testing, and when you change your password or remove access. Instagram tokens last 60 days and cannot be refreshed once expired. | Click Connect again (or paste a new Instagram token). Waiting clips continue. |
| A Twitch, Kick or YouTube channel says needs action, credentials rejected | Wrong or expired client ID, secret, key or token. | Fix them on the Sources page, or sign in again. |
| "rate limit" or "too many requests" | The platform said slow down. | Nothing. The app waits for the time given and tries again. |
| YouTube "quota exceeded" | The daily quota is used up (it resets at midnight Pacific time). Chat reading uses a lot of it. | Wait, turn off YouTube chat, or watch fewer channels. |
| YouTube upload limit | About 100 uploads a day. | Wait for the next day. |
| TikTok posts are private or only drafts | Your app is not audited, or inbox mode is on. | See Posting. Finish drafts in the TikTok app. |
| TikTok `unaudited_client_can_only_post_to_private_accounts` | Unaudited apps can only post privately. | Make the account private, or use drafts. |
| Instagram upload says it needs a public address | Instagram Login cannot take local files. | Set `video_base_url` to a public https folder, or use Facebook Login. |
| YouTube video shows as locked private | Possibly the older rule for unverified apps. | Check YouTube Studio and post it from there. See Posting. |
| YouTube "Sign in to confirm you're not a bot" when recording | YouTube refuses playback to this computer's network. | Use a feed address from the creator, or run the app from a network YouTube accepts. |
| Channel stays "waiting" with YouTube | The app searches YouTube rarely to save quota, so it can notice a stream late. | Wait up to 15 minutes, or use a feed address. |
| Kick chat unavailable | Kick sends chat only to a public https address. | Set one, or leave chat off. |
| Delivery says needs_confirmation | The app cannot tell whether an upload landed. | Check the account, then press "Confirm posted" or "Confirm not posted" on the clip's page. |

## Webhook notifications do not arrive

Check the address in Settings. A failed webhook is logged as a warning and does not stop the app. Browser
notifications only show while the app is open in a tab.

## Still stuck

Run `live-clipper logs`, find the first line with ERROR or warning, and read it. Do not post logs publicly
without reading them first, even though secrets are masked.
