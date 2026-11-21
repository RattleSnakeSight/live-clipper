# Troubleshooting

## First checks

- `live-clipper doctor` checks Python, ffmpeg, free disk space and the port.
- `live-clipper status` says whether the app is running.
- `live-clipper logs` prints recent log lines. Secrets are masked in the logs.
- The Status page in the app shows the same, plus service states and the restart count.

## The disk is full or recording paused

Live Clipper stops starting new clips when free space is low or when it has used the disk cap you set.
It then cleans up in this order:

1. Temporary render files older than an hour.
2. Trash past its keep time.
3. Posted clips older than your retention period move to trash.
4. If space is still short, trash is emptied early, then original recordings of posted clips (or whole
   posted clips first, depending on your answer), oldest first.

Clips that are not posted yet are never deleted. If the cap is full of unposted clips, post or discard some,
or raise the cap. The Status page lists what would be cleaned next.

## The browser says unauthorized

Open the app with `live-clipper start`. It opens the page with a fresh sign-in link.

## The app keeps restarting

Check `live-clipper logs` and the restart list on the Status page, then run `live-clipper doctor`.

<!-- TODO(integrator): platform-specific problems such as login failures, offline detection and upload errors -->
