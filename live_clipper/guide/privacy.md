# Privacy

Live Clipper runs only on your computer. The people who made it run no server, receive no data and have no
account for you. There is no telemetry and no usage counting.

## What is stored, and where

Everything lives in two places on your computer: the data folder and the storage folder you chose in setup.

| System | Default data folder |
|---|---|
| Windows | `%LOCALAPPDATA%\live-clipper` |
| macOS | `~/Library/Application Support/live-clipper` |
| Linux | `~/.local/share/live-clipper` |

`live-clipper doctor` prints the one in use. `--home <folder>` or the `LIVE_CLIPPER_HOME` environment
variable picks another.

- Settings, channels, clip records and the activity log: one database file in the data folder.
- Clips and recordings: the storage folder (by default the `media` folder inside the data folder). Posted
  clips are deleted after the keep time you chose (14 days by default); clips that are not posted are never
  deleted automatically. See [Troubleshooting](troubleshooting.md) for the cleanup order.
- Saved logins and keys for the platforms you connected, and the webhook address: the vault, an encrypted
  file in the data folder. Its key is kept in your system's credential manager when the optional `keyring`
  Python package is installed next to Live Clipper, and otherwise in a protected file in the data folder.
- Log files, with tokens and passwords masked.
- The speech recognition model, once downloaded: the `models` folder in the data folder.
- If you pair the agent that installed the app: which agent is paired, and your key requests (platform, the
  place you chose, the outcome). Never the key values, which go to the vault.

## What goes over the network, and when

| When | To | What is sent |
|---|---|---|
| Every start, every open, every 6 hours while running | GitHub (`raw.githubusercontent.com`) | A plain request for the version line of Live Clipper's newest release. No account, key, path, file list or device details. |
| While watching channels | The streaming platforms you watch | Live status questions, chat reading, and the stream recording (through Streamlink). |
| When a clip posts | The destinations you connected | The clip, its title and its caption. |
| When a notification fires, if you set a webhook | Your webhook address | The notification title and text, such as "Clip posted". |
| The first time a clip is captioned | Hugging Face, where the speech models are published | A download of the model you picked in setup. |
| Install and update | GitHub and the Python package index | The download of Live Clipper and its dependencies. |

Like any web request, each one shows your IP address and the time to the service you contact. Nothing from
the update answer is run: the app only reads the version number (see [Updating](updating.md)).

The app does not download translation models. Captions in a second language need converted Opus-MT models
that you place in the data folder's `models` folder yourself; without them, a second-language render reports
that the model is missing, and original-language captions work as usual.

The app listens on your own computer only (127.0.0.1), and every request needs the sign-in link it opens for
you. If you give Kick a public address for chat, only the separate Kick webhook listener is reached through it.

## Redaction

Tokens, secrets, keys and passwords are masked before they reach the logs, the activity list or a
notification. Secrets you paste into the app are never shown again in full, and the webhook address appears
only as its host. Read a log before you share it anyway.

## Removing your data

`live-clipper uninstall --purge-data` deletes the data folder; see [Uninstalling](uninstalling.md) for what
that leaves behind, such as a separate storage folder and the access you granted on each platform.
