# Uninstalling

Run `live-clipper uninstall`. It stops the app and removes start at login. This removes the Task Scheduler
entry on Windows, the launch agent on macOS or the user service on Linux.

Add `--purge-data` to also delete your clips, settings and saved logins. It asks first unless you add `--yes`.
It refuses to delete a folder that is not a dedicated data folder, such as your home folder.

Then remove the program itself with `pipx uninstall live-clipper`
(or `python -m pip uninstall live-clipper` if you installed with pip). The command prints the exact line to
use.

Without `--purge-data` your data folder stays, so a later reinstall picks up where you left off. Where the
data folder is and what it holds is on [Privacy](privacy.md).

To remove only start at login and keep the app, run `live-clipper service uninstall`.

If you installed the agent skill, delete its `live-clipper` folder from your agent's skills folder
(`~/.claude/skills`, `~/.codex/skills` or `~/.qwen/skills`, or `.claude/skills` in a project). If you paired
the key helper, run `live-clipper agent unpair` first; `--purge-data` also deletes the pairing, which is kept
in the data folder.

Two things live outside the data folder:

- Your clips and recordings, if you chose a different storage folder in setup. `--purge-data` deletes the
  data folder only. Delete the storage folder yourself if you want those gone.
- The accounts you connected. Removing the app does not revoke its access. To revoke, remove the app in each
  platform's settings (your Google account permissions, TikTok, Meta's app settings, Twitch connections and
  Kick) and delete the developer applications you made.
