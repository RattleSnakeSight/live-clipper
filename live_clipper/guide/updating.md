# Updating

Run `live-clipper update`. It stops the app if it is running, upgrades the program from the place you
installed it from, and starts it again. Your data stays in your user data folder and is not touched. That
includes your settings, channels, clips, saved logins, formats, presets, and whether you finished the tour.

If you installed with the install script, running the script again does the same thing.
If you run from a checkout (a developer install), use `git pull` instead. `live-clipper update` says so and
changes nothing.

After an update:

- Your edited formats and presets are kept. The built-in ones are never overwritten. Use Reset on a built-in
  one if you want the new shipped version.
- If something looks off, run `live-clipper doctor`, then `live-clipper logs`.
- To check the version, run `live-clipper version`.
