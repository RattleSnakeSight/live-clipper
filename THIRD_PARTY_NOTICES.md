# Third-party notices

Files bundled in this repository that were written by others. Their licence files ship next to them.

| File | What | Licence | Source | SHA-256 |
|---|---|---|---|---|
| `live_clipper/models/face_detection_yunet_2023mar.onnx` | YuNet face detector (OpenCV Zoo), used by the split-layout facecam check | MIT, see `live_clipper/models/LICENSE.face_detection_yunet` | https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| `live_clipper/fonts/Inter-ExtraBold.otf` | Inter 3.19 ExtraBold, the hook-title font | SIL Open Font License 1.1, see `live_clipper/fonts/LICENSE-Inter.txt` | https://github.com/rsms/inter/raw/v3.19/docs/font-files/Inter-ExtraBold.otf | `158144ef2ad48baf0e45907951a00a1d3264f85c742ccfbc087e90d3d9288cfa` |

Downloaded at run time, not bundled:

- faster-whisper models (default `small`, MIT, from Hugging Face `Systran/faster-whisper-*`), into
  `<data dir>/models/whisper` the first time a clip with audio is captioned.
- Opus-MT en<->es converted to CTranslate2 (Apache-2.0), expected in `<data dir>/models/opus-mt-en-es` and
  `opus-mt-es-en`. Without them the original-language captions still work and the translation is marked failed.
- FFmpeg (external binary; LGPL/GPL depending on the build; needs libx264 and libass).
