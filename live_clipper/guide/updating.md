# Updating

Run `live-clipper update`. It stops the app if it is running, upgrades the program from the place you
installed it from, and starts it again. Your data stays in your user data folder and is not touched. That
includes your settings, channels, clips, saved logins, formats, presets, and whether you finished the tour.

If you installed with pipx, `pipx upgrade live-clipper` followed by `live-clipper start` does the same.
If you installed with the install script, running the script again does the same thing.
If you run from a checkout (a developer install), use `git pull` instead. `live-clipper update` says so and
changes nothing.

After an update:

- Your edited formats and presets are kept. The built-in ones are never overwritten. Use Reset on a built-in
  one if you want the new shipped version.
- If something looks off, run `live-clipper doctor`, then `live-clipper logs`.
- To check the version, run `live-clipper version`.

## Update pending

Live Clipper runs only when it is the newest version. Every time it starts, and every time you open it (with
`live-clipper start` or by loading it in the browser), it checks whether a newer version is out.

When one is, the browser shows **Update pending** with the installed and the available version and the
steps above, and `live-clipper start` prints the same. Until you update, Live Clipper does not watch
channels, record, make clips or post, also when it runs in the background or starts at login. Nothing is
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
