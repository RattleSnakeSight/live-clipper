"""Protected terms and GTA phrase map for translation.

Protection: each protected span is replaced by a placeholder token ``Q<n>Z`` (letters+digit,
which Opus-MT copies verbatim in practice). After translation we verify every placeholder
survived; segments with a dropped placeholder are retried without protection and known terms the MT
left intact get their casing fixed (we never invent text). Surviving placeholders are restored to the original surface form.
"""
from __future__ import annotations

import re

BASE_TERMS = [
    "Los Santos", "Blaine County", "Sandy Shores", "Paleto Bay", "Maze Bank", "Vinewood",
    "Vespucci", "Del Perro", "Mount Chiliad", "Fleeca", "Pacific Standard", "Cayo Perico",
    "Diamond Casino", "LSPD", "LSSD", "FIB", "IAA", "NOOSE", "NoPixel", "GTA", "GTA V", "GTA Online",
    "Zentorno", "Oppressor", "Oppressor Mk II", "Buzzard", "Hydra", "Lazer", "Insurgent", "Adder",
    "T20", "Turismo", "Sultan", "Kuruma", "Deluxo", "Rhino", "Lester", "Franklin", "Trevor",
    "Michael", "Lamar", "Chop", "Los Santos Customs", "Ammu-Nation", "Humane Labs",
]

# Slang MT mangles. Decisions (documented):
#  - "wasted"/"busted" are the in-game on-screen words; keep them untranslated (ES community uses them).
#  - "cops" -> "la poli" (colloquial, matches streamer register); "police" -> "policía".
#  - "wanted level" -> "nivel de búsqueda" (official ES GTA V term); "stars" in that context kept by MT.
#  - "heist" -> "atraco"; "heli" -> "helicóptero"; "ragdoll" kept.
PHRASES_EN_ES = [
    (r"\bwanted level\b", "nivel de búsqueda"),
    (r"\bfive[- ]star wanted level\b", "búsqueda de cinco estrellas"),
    (r"\b(?:the )?cops?\b", "la poli"),
    (r"\bthe police\b", "la policía"),
    (r"\bheist\b", "atraco"),
    (r"\bheli\b", "helicóptero"),
    (r"\bgg\b", "gg"),
]
PHRASES_ES_EN = [
    (r"\bnivel de b[uú]squeda\b", "wanted level"),
    (r"\bla poli\b", "the cops"),
    (r"\bel atraco\b", "the heist"),
    (r"\bme han? (?:atrapado|pillado)\b", "I got busted"),
]
KEEP = ["wasted", "busted", "ragdoll", "chat"]  # never altered by MT (protected as terms)

_MENTION = re.compile(r"@\w+")
_ASCII_NAME = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b|\b[A-Z]{2,}[a-z]*\d*\b")  # CamelCase / ACRONYM

# First word of a sentence is capitalised by grammar; only treat capitalised words as names in
# mid-sentence position AND outside a list of common words.
_COMMON = set("""I I'm I'll I've I'd Oh Okay OK No Yes Yeah Hey The A An And But So Well Chat What Why How Wow
Bueno Banco Bank Calle Street Policía Police Pero Ya Que Y El La Los Las Un Una No Si Sí Oh Dios Mira Vamos Venga Vale""".split())
_CAP_MID = re.compile(r"(?<=[a-záéíóúñ,;] )([A-Z][a-záéíóúñ]{2,}(?: [A-Z][a-záéíóúñ]{2,})*)")


class Glossary:
    def __init__(self, extra_terms=()):
        terms = list(BASE_TERMS) + [t for t in KEEP] + [t.strip() for t in extra_terms if t and t.strip()]
        seen, out = set(), []
        for t in terms:
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        self.terms = sorted(out, key=len, reverse=True)  # longest first
        self._rx = re.compile(r"(?<![\w@])(" + "|".join(re.escape(t) for t in self.terms) + r")(?!\w)",
                              re.IGNORECASE)

    def detect_names(self, text: str) -> list[str]:
        """@mentions, CamelCase/ACRONYM handles and mid-sentence capitalised words."""
        found = _MENTION.findall(text) + _ASCII_NAME.findall(text)
        for m in _CAP_MID.findall(text):
            ws = m.split()
            while ws and ws[0] in _COMMON:
                ws = ws[1:]
            if ws:
                found.append(" ".join(ws))
        return list(dict.fromkeys(found))

    def correct_asr(self, text: str, cutoff: float = 0.78) -> str:
        """Snap near-miss single words to known single-word terms (ASR mishears 'Zentorno')."""
        import difflib
        singles = {t.lower(): t for t in self.terms if " " not in t and len(t) >= 6}

        def fix(m):
            w = m.group(0)
            if len(w) < 5 or w.lower() in singles:
                return singles.get(w.lower(), w)
            hit = difflib.get_close_matches(w.lower(), list(singles), n=1, cutoff=cutoff)
            return singles[hit[0]] if hit else w
        return re.sub(r"[A-Za-z]+", fix, text)

    def protect(self, text: str, src: str = "en", tgt: str = "es"):
        """Return (protected_text, mapping{token: original}). Applies the phrase map first."""
        phrases = PHRASES_EN_ES if (src, tgt) == ("en", "es") else PHRASES_ES_EN
        mapping: dict[str, str] = {}

        def tok(orig: str) -> str:
            k = f"Q{len(mapping)}Z"
            mapping[k] = orig
            return k

        # phrase-map entries are protected with their *target* rendering (bypass MT)
        for pat, repl in phrases:
            text = re.sub(pat, lambda m, r=repl: tok(_match_case(m.group(0), r)), text, flags=re.I)
        extra = self.detect_names(text)
        if extra:
            rx = re.compile(r"(?<![\w@])(" + "|".join(re.escape(t) for t in sorted(extra, key=len, reverse=True)) + r")(?!\w)"
                            if not any(e.startswith("@") for e in extra) else
                            "(" + "|".join(re.escape(t) for t in sorted(extra, key=len, reverse=True)) + ")")
            text = rx.sub(lambda m: tok(m.group(0)), text)
        text = self._rx.sub(lambda m: tok(m.group(0)), text)
        return text, mapping

    @staticmethod
    def restore(text: str, mapping: dict[str, str]):
        text, missing = Glossary._restore(text, mapping)
        # "banco Maze Bank" -> MT "Maze Bank bank": drop the duplicated generic noun
        text = re.sub(r"\b(Maze Bank|Fleeca Bank|Pacific Standard Bank) [Bb]ank\b", r"\1", text)
        text = re.sub(r"\b(?:El|el) (la poli|la policía)\b", lambda m: m.group(1).capitalize() if m.group(0)[0]=="E" else m.group(1), text)
        return text, missing

    @staticmethod
    def _restore(text: str, mapping: dict[str, str]):
        """Restore tokens; return (text, missing_tokens)."""
        missing = []
        for k, v in mapping.items():
            pat = re.compile(r"\b" + k + r"\b|\b" + k[0] + r"\s?" + k[1:-1] + r"\s?Z\b", re.I)
            if pat.search(text):
                text = pat.sub(lambda _m, v=v: v, text)
            else:
                missing.append(k)
        return text, missing


def _match_case(src: str, repl: str) -> str:
    return repl[0].upper() + repl[1:] if src[:1].isupper() else repl
