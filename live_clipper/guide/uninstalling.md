# Uninstalling

Run `live-clipper uninstall`. It stops the app and removes start at login. Add `--purge-data` to also delete
your clips, settings and saved logins. It asks first unless you add `--yes`.

Then remove the program itself with `pipx uninstall live-clipper`
(or `python -m pip uninstall live-clipper` if you installed with pip).

Without `--purge-data` your data folder stays, so a later reinstall picks up where you left off.
