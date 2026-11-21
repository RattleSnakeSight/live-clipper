# Privacy

Live Clipper runs only on your computer.

## What is stored locally

- Settings, channel list, clip records and the activity log, in one database file in your data folder.
- Clips and recordings, in the storage folder you chose.
- Saved logins for the platforms you connected, in an encrypted file. The key lives in your operating
  system keyring when one is available, and in a protected file in the data folder otherwise.
- Log files, with tokens and passwords masked.

## What is sent, and where

Nothing goes to the people who made Live Clipper. There is no telemetry, no usage counting and no account.

The app contacts only services you set up: the streaming platforms you watch, the destinations you post to,
and the webhook address you gave for notifications. A webhook message holds the notification title and
text, such as "Clip posted".

The app listens on your own machine only (127.0.0.1), and every request needs the sign-in link it opens for you.

## Where the data is

`live-clipper doctor` prints the data folder. `live-clipper uninstall --purge-data` deletes it.
