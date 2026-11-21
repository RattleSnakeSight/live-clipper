# First run

The first time you open Live Clipper it shows a short wizard. Every question has a default picked for your
computer, so you can accept them all and change things later. Your answers stay on your computer. The wizard
only shows a question when an earlier answer needs it.

## The questions

| Question | What it means | Default |
|---|---|---|
| How will you use Live Clipper? | Your own channel, other streamers with their permission, or just trying it. If you pick other streamers, the next question asks you to confirm their permission. It does not change how the app works. | Your own channel |
| Do you have permission to clip and post their streams? | Only shown if you picked "other streamers". You must answer yes to continue. Only clip and post streamers who said yes. | No |
| Which platforms do you want to watch? | Twitch, Kick, YouTube. Pick at least one. The Sources page shows these first. | Twitch |
| Where should clips and recordings be stored? | A folder on a drive with plenty of space. The app will not use a drive root or your home folder. If the folder you pick already holds other files, it makes a `LiveClipper` subfolder inside it. | The app's own media folder |
| How much disk space may Live Clipper use (GB)? | At this limit the app stops starting new clips and cleans up old posted ones. | A quarter of the free space, at least 5 GB and at most 50 GB |
| How many days should posted clips be kept? | Posted clips are deleted after this long. Clips that are not posted yet are never deleted. | 14 days |
| What should be deleted first when the disk is full? | Original recordings of posted clips first, or whole posted clips, oldest first. Recordings are big and finished clips are small. | Original recordings first |
| Which automatic cleanups do you want? | Delete posted clips after the retention period, and delete the oldest posted clips when the cap is reached. Temporary files are always cleaned, and clips waiting to be posted are always kept. | Both |
| Where should speech recognition run? | The processor or an NVIDIA graphics card. The app tells you what it found. Speech recognition writes the captions. | NVIDIA GPU if one is found, otherwise the processor |
| How accurate should speech recognition be? | Bigger models hear better but need more memory and time. | With an NVIDIA GPU: large with 10 GB of video memory or more, medium with 6 GB or more, small below that. Without one: small with 16 GB of RAM and 8 or more cores, tiny under 6 GB of RAM, base otherwise |
| Which number precision should speech recognition use? | A technical setting. Leave it unless you know why. | float16 on a GPU, int8 on a processor |
| How many streams may be recorded at the same time? | Each recording uses processor, memory and disk. | Half your processor cores or one per 4 GB of RAM, whichever is lower, at least 1 and at most 8 |
| Where do you want to post clips? | YouTube Shorts, TikTok, Instagram Reels, or nowhere (you only want the files). "Nowhere" cannot be combined with the others. The Destinations page shows the ones you pick first. Picking one does not connect it. | Nowhere |
| Should clips be posted automatically? | Only shown if you picked a destination. Automatic posts each finished clip to every destination you have connected, confirmed and turned on. Manual means every clip waits for your approval under "Waiting for your approval" on the Clips page. Without a destination the question is skipped and automatic applies, which posts nothing until you connect one. | Automatic |
| How should Live Clipper tell you about new clips and problems? | No notifications, browser notifications (only while the app is open in a tab), a webhook, or both. | Browser |
| What is the webhook address? | Only shown for the webhook choices. An ntfy topic address, a Discord webhook address, or any address that accepts a JSON message. | Empty |
| Which message format does the webhook expect? | Detect it from the address, plain JSON, ntfy or Discord. | Detect |
| Start Live Clipper automatically when you log in? | Runs in the background so recording is not missed after a restart. It uses Task Scheduler on Windows, a launch agent on macOS and a user service on Linux. | No |

The webhook address is stored in the encrypted vault, not in the settings.

Choosing a destination in the wizard does not connect it. You connect each account later, as described in
Posting. Until an account is connected, enabled and acknowledged, nothing is posted.

## The tour

After the wizard, a short tour points out the main parts of the app. You can click Skip tour (or press Escape) at any time.

- Whether you finished the tour or skipped it is remembered. It does not come back after a restart or an
  update.
- To see it again, open Settings and choose Replay tutorial. That is the only way to replay it.
