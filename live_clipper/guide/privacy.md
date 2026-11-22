# Privacy

Live Clipper runs only on your computer.

## What is stored locally

- Settings, channel list, clip records and the activity log, in one database file in your data folder.
- Clips and recordings, in the storage folder you chose.
- Saved logins and keys for the platforms you connected, in the vault: an encrypted file. The key lives in your
  operating system keyring when one is available, and in a protected file in the data folder otherwise. This
  includes API keys, client secrets, tokens and the webhook address.
- Log files, with tokens and passwords masked.
- If you pair the AI agent that installed the app: which agent is paired, in the data folder, and your key
  requests (platform, the place you chose, the outcome). Never the key values, which go to the vault.

## What is sent, and where

Nothing goes to the people who made Live Clipper. There is no telemetry, no usage counting and no account.

The app contacts only services you set up:

- The streaming platforms you watch (to ask whether a channel is live, to read chat, and, through Streamlink,
  to record).
- The destinations you post to, with the clip and its title and caption.
- The webhook address you gave for notifications. A webhook message holds the notification title and text,
  such as "Clip posted".
- The download of the speech recognition model the first time a clip is captioned, which goes to the model
  host, not to us. The app does not download translation models: captions in a second language work only
  with models you add yourself.

The app listens on your own machine only (127.0.0.1), and every request needs the sign-in link it opens for
you.

## Redaction

Tokens, secrets, keys and passwords are masked before they reach the logs, the activity list or a
notification. Secrets you paste into the app are never shown again in full. The webhook address appears only
as its host.

## Where the data is

`live-clipper doctor` prints the data folder. `live-clipper uninstall --purge-data` deletes it. See
Uninstalling for what that leaves behind.
