"""Bilingual captions (owned by the captions workstream). Contract:

asr.Transcriber(model: str, device: str, threads: int)
    .transcribe(wav_path) -> Transcript   # detects language (en/es), word timestamps
translate.Translator(models_dir)
    .translate_segments(segments, src: "en"|"es", tgt: "en"|"es") -> list[Segment]
subs.build_cues(segments, max_chars=32, max_lines=2) -> list[Cue]
subs.to_srt(cues) / subs.to_vtt(cues) / subs.to_ass(cues, style: dict, play_res=(w, h)) -> str
glossary: protects names / GTA terms through translation (placeholders), see glossary.py

Transcript = {"language": "en", "language_probability": 0.97, "duration": 30.0,
              "segments": [{"start": 0.0, "end": 2.1, "text": "...", "words": [{"start","end","word"}]}]}
Times are seconds relative to the clip start (the clip's source.ts start after trim).
"""
