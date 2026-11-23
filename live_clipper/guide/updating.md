# Updating

Run `live-clipper update`. It stops the app if it is running, upgrades the program from the place you
installed it from, and starts it again if it was running. Your data stays in your data folder and is not
touched. That includes your settings, channels, clips, saved logins, formats, presets, and whether you
finished the tour.

Other ways, by how you installed:

- pipx: `pipx upgrade live-clipper`, then `live-clipper start`.
- An install script: run the script again.
- A developer checkout (editable install): `git pull`. `live-clipper update` says so and changes nothing.

After an update:

- Your edited formats and presets are kept. The built-in ones are never overwritten. Use Reset on a built-in
  one if you want the new shipped version.
- If something looks off, run `live-clipper doctor`, then `live-clipper logs`, and see
  [Troubleshooting](troubleshooting.md).
- To check the version, run `live-clipper version`.

## Newer documentation between releases

The guide ships inside the program, so it changes only when the program does. Documentation changes between
releases keep the version number, and `pipx upgrade` can then keep the copy you already have. To pick up guide
changes published after your install, stop the app, reinstall from the same source and start it again:

```text
live-clipper stop
pipx reinstall live-clipper
live-clipper start
```

The newest guide is also readable on GitHub, in the `live_clipper/guide` folder of the repository.

## Update pending

Live Clipper runs only when it is the newest version. Every time it starts, and every time you open it (with
`live-clipper start` or by loading it in the browser), it checks whether a newer version is out.

When one is, the browser shows **Update pending** with the installed and the available version and the
steps for your install method, and `live-clipper start` prints the same. Until you update, Live Clipper
does not watch channels, record, make clips or post, also when it runs in the background or starts at login. Nothing is
deleted: recordings, clips, settings, saved keys and queued posts wait, and everything carries on after the
update. **Check again** asks once more. After the update Live Clipper starts again and opens a new tab.

If an AI agent such as Claude Code or Codex installed Live Clipper, you can ask it to "update Live Clipper to
the newest version". It runs the same command.

`live-clipper status`, `stop`, `doctor`, `logs` and `update` keep working while an update is pending.
`live-clipper update --check` checks from the terminal.

## When the check does not work

Without an answer (no internet, GitHub not reachable, a proxy setting it cannot use, an answer it cannot
read), Live Clipper shows **Can't check for updates**. It never guesses: it does not claim an update exists,
and it does not start its normal work either. It tries again by itself, after 15 seconds and then with growing waits up to every 5 minutes,
and starts as soon as a check works. This means Live Clipper needs an internet connection when it starts.

An update that was already confirmed stays pending through outages and restarts, until you install it.
Once Live Clipper has confirmed at start that it is current, a later failed check does not stop it.

## What the check sends

It reads one small text file, the version line of Live Clipper on the main branch of its GitHub repository,
from `raw.githubusercontent.com`. The request carries no account, key, path, file list or anything else
about you or this computer. Like any web request, GitHub sees your IP address and the time. It repeats at
every start and open, every 6 hours while running, and with growing waits after a failure. Nothing from the
answer is run: Live Clipper only reads the version number.

## Updating from 0.2.0 or older

Versions up to 0.2.0 do not check for updates, so they never show Update pending. Update them once by hand
with `live-clipper update` (or ask the agent that installed it). From 0.3.0 on, Live Clipper tells you
itself.
