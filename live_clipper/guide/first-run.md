# First run

The first time you open Live Clipper it shows "Set up Live Clipper", a short series of questions. Every
question already has an answer picked for your computer, so you can press **Next** through all of them and
change things later. A question only appears when an earlier answer needs it. Your answers stay on your
computer.

The setup cannot be skipped: the other pages open once it is finished. To change your answers later, open
**Settings**, which groups them by topic, or press **Run setup again** there.

## The questions

**How you use it**

- *How will you use Live Clipper?* Your own channel, other streamers with their permission, or just trying
  it. Default: your own channel. It does not change how the app works.
- *Do you have permission to clip and post their streams?* Only shown for "other streamers". You must answer
  yes to continue, and you should only clip streamers who said yes.
- *Which platforms do you want to watch?* Twitch, Kick, YouTube; at least one. Default: Twitch. The Sources
  page lists these first. This is the "Watch a stream" question. Watching needs no account: you paste a
  channel link, and keys can be added later.

**Storage**

- *Where should clips and recordings be stored?* Default: the media folder inside the app's data folder. The
  app refuses a drive root or your home folder, and makes a `LiveClipper` subfolder if the folder you pick
  already holds other files.
- *How much disk space may Live Clipper use (GB)?* Default: a quarter of the free space, at least 5 and at
  most 50. At this cap the app stops starting new clips and cleans up old posted ones.
- *How many days should posted clips be kept?* Default: 14. Clips that are not posted are never deleted.
- *What should be deleted first when the disk is full?* Default: original recordings of posted clips, then
  posted clips. The other choice deletes whole posted clips, oldest first.
- *Which automatic cleanups do you want?* Default: both (delete posted clips after the keep time, and delete
  the oldest posted clips at the cap). Temporary files are always cleaned.

**Speech recognition** (it writes the captions)

- *Where should speech recognition run?* The processor, or an NVIDIA graphics card if one is found.
- *How accurate should it be?* Bigger models hear better but need more memory and time. Default with an
  NVIDIA card: large from 10 GB of video memory, medium from 6 GB, small below. Without one: small with
  16 GB of RAM and 8 cores or more, tiny under 6 GB of RAM, base otherwise.
- *Which number precision?* A technical setting: float16 on a graphics card, int8 on a processor.
- *How many streams may be recorded at the same time?* Default: half your processor cores or one per 4 GB of
  RAM, whichever is lower, between 1 and 8.

**Posting**

- *Where do you want to post clips?* YouTube Shorts, TikTok, Instagram Reels, or nowhere (files only).
  Default: nowhere. Picking a destination here does not connect it. This is the "Post my clips" question,
  and it says who does the posting: YouTube Shorts posts directly through Google, and Instagram and TikTok
  post directly through your own Meta or TikTok developer app. Metricool, the outside scheduling service
  the maintainers' own tested setup used for Instagram and TikTok, is not part of this installation; see
  [Posting](posting.md).
- *Should clips be posted automatically?* Only shown when you picked a destination. Default: **automatic**,
  which posts each finished clip to every destination you have connected, confirmed and switched on.
  **Manual** holds every clip under "Waiting for your approval" on the Clips page until you press Post.
  With no destination there is nothing to post, so the question is skipped.

**Notifications and start at login**

- *How should Live Clipper tell you about new clips and problems?* None, browser notifications (only while
  the app is open in a tab), a webhook (ntfy, Discord or any address that accepts JSON), or both. Default:
  browser. The webhook address is stored in the encrypted vault.
- *Start Live Clipper automatically when you log in?* Default: no. It uses Task Scheduler on Windows, a launch
  agent on macOS and a user service on Linux.

The last page shows "Accounts for what you picked", in two groups: **Watch a stream** and **Post my clips**.
For each platform it says what the connection is for, whether it needs an account, and where you set it up
after setup: on Sources and on Destinations, with the steps next to the fields. Connecting can wait until
setup is finished. The agent that installed Live Clipper can fill in keys from there: see
[Let your agent set up keys](agent-setup.md).

## Connecting accounts afterwards

Sources ("Watch a stream") and Destinations ("Post my clips") each show the other at the top, with how many
connections each has, so you can do one without the other.

- To watch, you need nothing. [Platforms](platforms.md) explains what an API key or a developer application
  adds, and where every value is found.
- To post, each destination card walks through four steps and opens only the next one: developer app keys,
  sign-in, checking the account, switching posting on. [Posting](posting.md) has the same steps, with what
  to do when a sign-in fails.

## A safe first test

Automatic posting is the default, but nothing is posted until you connect an account, tick that you have the
right to post and switch the destination on. For a first test that cannot post anything:

1. Choose **Manual** posting (in setup, or later under Destinations > Posting mode). To get the posting
   question in setup, pick any destination; picking does not connect it.
2. On Destinations, switch on **Use the test destination**. It is a stand-in on your computer and posts
   nowhere.
3. On Channels, add a channel you own or have permission to clip, and switch monitoring on.
4. When the channel goes live and a moment qualifies, the clip appears on the Clips page under "Waiting for
   your approval". Open it to watch the preview, edit it if you like, then press **Post to** and the test
   destination. Its line under "Where it was posted" then says published, on this computer only.

How long the first clip takes depends on the stream: the app needs the channel to be live and a moment that
passes the format's checks. See [Monitoring](monitoring.md) and [Posting](posting.md).

## Appearance

The app opens in a dark theme. **Settings > Appearance** switches to light, and the choice is kept.

## The tour

After the setup, a short tour points out the main parts of the app. Press **Skip tour** (or Escape) at any
time. Finished or skipped, the tour does not come back after a restart or an update. To see it again, open
**Settings** and press **Replay tutorial**.
