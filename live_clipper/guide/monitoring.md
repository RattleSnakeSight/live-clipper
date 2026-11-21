# Monitoring streams

Add a channel, then turn monitoring on. While a channel is live, Live Clipper records it into a short
rolling buffer and watches for moments worth clipping. When one qualifies, it cuts a clip from the buffer.

How many streams you can watch at once is the "recordings at the same time" answer from the wizard.
Each recording uses processor, memory and disk. If free disk space gets critically low, recording pauses
and resumes once space is free again.

The Status page shows each service, how many times the app restarted, free disk space, ffmpeg, and the
latest log lines.

<!-- TODO(integrator): channel states and what each one means, how moments are chosen -->
