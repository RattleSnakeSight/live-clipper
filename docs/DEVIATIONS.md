# Deviations from the original design

Each subsystem appends its own section. Everything not listed here behaves as the original.

## Edit (cutting, rendering, captions)

Evidence: `tests/parity/test_cutting_parity.py` reruns the recorded synthetic cases (`tests/parity/data/`) and
compares cut boundaries, stored reasons, epochs, ffmpeg command lines, filtergraphs, ffprobe properties and caption
files. With the same FFmpeg 9.1 build, every live preview and every final except the titled one is byte-for-byte
the same size as the original's output.

### Kept on purpose (known quirks of the original)

- **Late video start on re-renders.** Finals, re-rendered previews and trims seek on the input side
  (`-ss <trim_in> -i source.ts`). On MPEG-TS the video only starts at the next keyframe after the target: in the
  recorded case the first video frame is at 0.767 s of a 30 s final (video 29.233 s, audio 30.000 s, still in sync).
  This is the default and the parity test checks it. Opt in to the fix with preset value `encode.accurate_seek: true`,
  which moves `-ss` after `-i`. The video then starts at 0 (tested), and decoding takes a little longer.
- Only `buffer_start` / `gap_before` from `resolve()` are stored as `truncated`; the cut loop's `gap_after`,
  `stream_ended` and `segment_evicted` overwrite them, as before.

### Platform and packaging differences

- **Title font.** The original hard-coded the Debian path `/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf`.
  `postprocess.TITLE_FONT` now resolves to the bundled `live_clipper/fonts/Inter-ExtraBold.otf` (Inter 3.19, SIL OFL),
  then that Debian path, then none (drawtext's default font). Hosts without Inter now render the same glyphs as the
  original's Debian host. The original failed outright on Windows/macOS (ffmpeg could not open the font).
  The path is passed with forward slashes. The only change in the command line is the `fontfile=` value.
- **Caption font.** The ASS style still names `Inter`, and the `ass=` filter is unchanged (no `fontsdir`), so the
  command line stays the same. libass uses an installed Inter if there is one. Otherwise it picks the system
  fallback (for example Arial on Windows), and the glyph metrics differ slightly. Install Inter for an identical look.
- **`nice`.** `proc.niced` prefixes `nice -n N` on Linux/macOS as before. On Windows, `nice` is never used, even an
  MSYS/Cygwin one on PATH, because it would rewrite path-like ffmpeg arguments. Instead, every media process (cut,
  render, frame read, loudness probe) starts with `BELOW_NORMAL_PRIORITY_CLASS` (`proc.LOW_PRIO`). No `/proc`
  access exists in these modules.
- **FFmpeg location.** Commands still call `ffmpeg`/`ffprobe`. When the setting `ffmpeg_path` (a folder or the
  binary) is set, the edit service puts that folder first on `PATH` at start.
- **Renamed identifiers.** The transient-ring marker file is now `.live-clipper-ring` (`buffer.RING_MARKER`), the
  face-model override env var is `LIVE_CLIPPER_FACE_MODEL`, and the sweeper thread is `live-clipper-buffer-sweeper`.
- **Lazy cross-subsystem imports.** `postprocess.post_cut` imports `language_policy` (from the selection side) when it
  runs, not at module import. `loudness.LoudnessTrigger.feed` imports `events.Trigger` when it runs. Without this,
  the render and edit paths would need modules this branch does not own.

### Parameters that are now data (defaults = original constants)

`live_clipper/presets/default.json` holds the original values: pre/post roll 18/12 s, max trim 60 s, preview 1280
(crf 23, `-g 60`), final 1920 (crf 21), x264 `veryfast`, fps cap 30, layout `auto`, split 0.32, caption style (Inter
bold, 0.068 W, ...), title 2.8 s / wrap 20, render the original language only. `cut_clip` gained a `crf` keyword
(default 23). `render_video` reads `render_crf`, `accurate_seek`, `title_secs` and `title_wrap` from
`engine.settings`, falling back to the original constants when they are missing. `post_cut` and `apply_edit` pass
the preset's caption style, and `render_languages: "both"` also renders the other language automatically. With the
shipped preset, every output is unchanged.

### Additions (do not change any output)

- `GET /api/clips/{id}/media/thumbnail` makes `thumbnail.jpg` (360 px wide, frame at 1 s of the preview) on first
  request. The original produced no thumbnails.
- ASR model and device come from settings `asr.model` (default `small`), `asr.device` (`auto`: CUDA if ctranslate2
  sees a GPU, else CPU) and `asr.compute_type` (default float16 on CUDA, int8 on CPU). The model downloads into
  `<data dir>/models/whisper`. `GET /api/status` → `services.edit.asr` reports whether faster-whisper is installed
  and whether the model is downloaded.

### Not ported here

- VOD import (`vod.py`) is not part of this branch. The parity adapter reports `vod_cut` as skipped.
- The auto-layout decision in the recorded reference fell back to `fit` because numpy was missing on that host.
  Here numpy and onnxruntime are installed and the real detector decides; for the synthetic source it also picks
  `fit`. The parity comparison ignores the fallback text and detector details.
