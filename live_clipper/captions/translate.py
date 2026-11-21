"""Offline en<->es translation with Opus-MT (CTranslate2 int8) + sentencepiece."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from .glossary import Glossary

PAIRS = {("en", "es"): "opus-mt-en-es", ("es", "en"): "opus-mt-es-en"}


class Translator:
    def __init__(self, models_dir="data/models", threads: int = 4, glossary_terms=(), beam_size: int = 2):
        self.models_dir = Path(models_dir)
        self.threads = threads
        self.beam_size = beam_size
        self.glossary = Glossary(glossary_terms)
        self._loaded: dict[tuple[str, str], tuple] = {}
        self._lock = threading.Lock()
        self.load_s = 0.0

    def _get(self, src, tgt):
        key = (src, tgt)
        if key not in PAIRS:
            raise ValueError(f"unsupported pair {src}->{tgt}")
        with self._lock:
            if key not in self._loaded:
                import ctranslate2
                import sentencepiece as spm
                t0 = time.perf_counter()
                d = self.models_dir / PAIRS[key]
                tr = ctranslate2.Translator(str(d), device="cpu", compute_type="int8",
                                            inter_threads=1, intra_threads=self.threads)
                sp_s = spm.SentencePieceProcessor(model_file=str(d / "source.spm"))
                sp_t = spm.SentencePieceProcessor(model_file=str(d / "target.spm"))
                self._loaded[key] = (tr, sp_s, sp_t)
                self.load_s += time.perf_counter() - t0
            return self._loaded[key]

    def _mt(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []
        tr, sp_s, sp_t = self._get(src, tgt)
        toks = [sp_s.encode(t, out_type=str) + ["</s>"] for t in texts]
        with self._lock:
            res = tr.translate_batch(toks, beam_size=self.beam_size, max_decoding_length=256,
                                     repetition_penalty=1.1)
        return [sp_t.decode(r.hypotheses[0]) for r in res]

    def translate_texts(self, texts: list[str], src: str, tgt: str) -> list[str]:
        """Translate with glossary protection; falls back per-text if a placeholder is dropped."""
        if src == tgt:
            return list(texts)
        prot = [self.glossary.protect(t, src, tgt) for t in texts]
        outs = self._mt([p[0] for p in prot], src, tgt)
        final: list[str] = []
        retry: list[int] = []
        scores: dict[int, int] = {}
        for i, (o, (_p, mapping)) in enumerate(zip(outs, prot)):
            restored, missing = Glossary.restore(o, mapping)
            if missing:
                retry.append(i)
                scores[i] = len(mapping) - len(missing)
            final.append(restored)
        if retry:
            # A placeholder was dropped: retry unprotected and keep whichever candidate preserves
            # more protected terms (ties keep the protected version). Never fabricates text.
            raw = self._mt([texts[i] for i in retry], src, tgt)
            for i, r in zip(retry, raw):
                cand = _keep_terms(texts[i], r, self.glossary)
                mapping = prot[i][1]
                cand_score = sum(1 for v in mapping.values()
                                 if re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", cand, re.I))
                if cand_score > scores[i]:
                    final[i] = cand
        return [re.sub(r"\s+([,.!?;:])", r"\1", f).strip() for f in final]

    def translate_segments(self, segments, src, tgt):
        """Translate segment texts, keeping timings; word timing distributed proportionally."""
        texts = [s["text"] for s in segments]
        outs = self.translate_texts(texts, src, tgt)
        res = []
        for s, t in zip(segments, outs):
            res.append({"start": s["start"], "end": s["end"], "text": t,
                        "words": distribute_words(t, s["start"], s["end"])})
        return res


def _keep_terms(src_text: str, mt_text: str, g: Glossary) -> str:
    """Fix casing of protected terms that MT kept in some form."""
    for m in g._rx.finditer(src_text):
        term = m.group(0)
        mt_text = re.sub(r"(?<!\w)" + re.escape(term) + r"(?!\w)", term, mt_text, flags=re.I)
    return mt_text


def distribute_words(text: str, start: float, end: float) -> list[dict]:
    """Spread target words over [start, end] proportionally to character length."""
    words = text.split()
    if not words:
        return []
    weights = [len(w) + 1 for w in words]
    total = float(sum(weights))
    dur = max(0.0, end - start)
    out, t = [], start
    for w, wt in zip(words, weights):
        d = dur * wt / total
        out.append({"start": round(t, 3), "end": round(t + d, 3), "word": w})
        t += d
    return out
