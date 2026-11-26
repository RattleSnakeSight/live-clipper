# Editing

Open any clip to trim it, change the framing, fix captions and re-render. Edits apply to that clip only.
The original recording stays until storage cleanup removes it (see [Troubleshooting](troubleshooting.md) for the cleanup order),
so you can keep re-editing a clip while its original is still around.

## What you can change

- Trim. Set a new start and end, in seconds from the start of the clip. You can trim only within the clip's
  length limit (60 seconds in the default preset).
- Title. The hook text shown at the start of the clip. It is stored per language (English and Spanish).
- Captions. Correct the text of the burned-in captions, per language.
- Framing. Auto, fit, crop, split or stack.
- Preset. Switch the whole look to another preset for this clip only.
- Look of this clip only. Video quality (CRF), video height, caption size, caption position and whether the
  title shows. These replace the preset for this clip alone. The form starts from the clip's current values and
  only the values you change are sent. Each field explains itself.

Then click Render again. The app makes a new video from the original recording with your changes. A clip
whose original is gone (expired or discarded) can no longer be edited.

## Rules of the editor

- A clip can be edited when it is ready (the preview or final video exists). A clip still being cut cannot.
- One edit or render runs at a time per clip. If you start a second, the app says one is already running.
- If you ask for a render with nothing to change, the app tells you so.
- Captions in a language must be finished before you render that language.

## The manual review queue

In manual posting mode every finished clip waits in the review queue ("Waiting for your approval" on the
Clips page). In automatic mode only clips held by a rule or by the gameplay check wait there. For each clip:

1. Watch the preview.
2. Edit it if you want, and render again.
3. Click "Post to" and the destination, or Reject the clip (or Cancel its delivery) if you do not want it.
   Reject also deletes the clip's video files from this computer and frees that space; it cannot be undone.
   The files stay only while an upload of the clip may still need them, another clip shares them, or the clip is
   being cut or rendered, and the clip page says so. The record of what was sent is always kept.
   If Reject kept the files because a render was running, wait for that render to finish, then use
   Settings > Clean up now: the preview lists the rejected clip, and confirming it removes the files.

Nothing is posted until you click Post. A clip that is held for review by a rule shows up in the same queue.
Clips in the queue are never removed by storage cleanup. Clicking "Clean up now" in Settings deletes, at once,
fully posted clips and rejected clips, and only those its confirmation listed; approved clips, clips in the queue and clips still being posted stay. A clip that is being
cut or rendered keeps its files, its original and its temporary files, by Clean up now and by automatic cleanup,
even when the render starts after the preview; it is listed again once the render finishes. How posting and its states work is on
[Posting](posting.md).
