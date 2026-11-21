from live_clipper.titles import make_title


def T(*t, lang="en"):
    return {"language": lang, "segments": [{"start": i, "end": i + 2, "text": x} for i, x in enumerate(t)]}


GENERIC = ("everything just changed", "wild moment", "insane clip")


def check(t):
    assert 0 < len(t) <= 70, t
    assert not any(g in t.lower() for g in GENERIC)


def test_crash_chase_laugh():
    t = make_title(T("Okay chat, I'm taking the Zentorno straight through the LSPD roadblock.",
                     "Oh no, I just crashed into the gas station!", "The cops are chasing me"),
                   ["LUL", "KEKW LUL", "OMEGALUL", "lmao", "LUL LUL"], "chat_burst")
    check(t)
    assert "Zentorno" in t and "gas station" in t and "chat" in t and "chase" in t


def test_heist_hype():
    t = make_title(T("We're robbing the Fleeca bank, Lester is on the radio", "Hold the door!"),
                   ["POGGERS", "Pog", "W", "clip it", "hype"], "emote_burst")
    check(t)
    assert "Fleeca" in t and "chat goes wild" in t


def test_wanted_stars_plane():
    t = make_title(T("Five star wanted level and they sent the military, I'm in the Hydra",
                     "Dodging missiles over the airport!"),
                   [], "audio_spike")
    check(t)
    assert "Hydra" in t or "airport" in t or "star" in t


def test_spanish_title_from_spanish_speech():
    t = make_title(T("Bueno chat, voy a robar el banco Maze Bank con Lester",
                     "¡No puede ser, me atrapó la policía!", lang="es"),
                   ["F", "F", "RIP", "F"], "chat_burst", lang="es")
    check(t)
    assert "Maze Bank" in t and "Pillado" in t and "chat" in t


def test_spanish_title_from_english_speech():
    t = make_title(T("I just crashed the Zentorno into the gas station!"), ["LUL", "KEKW", "LUL"], "x",
                   lang="es")
    check(t)
    assert "Zentorno" in t and "gasolinera" in t and "estrella" in t


def test_thin_evidence_falls_back_to_quote():
    t = make_title(T("I think we should head to the beach and hang out with Michael for a while"), [],
                   "audio_spike")
    check(t)
    assert "beach" in t


def test_empty_never_crashes():
    check(make_title({"segments": []}, [], ""))
    check(make_title({"segments": []}, [], "chat_burst", lang="es"))


def test_length_cap_long_everything():
    t = make_title(T("The Oppressor explodes at the Maze Bank " * 5,
                     "five star chase heist helicopter police"),
                   ["LUL"] * 10, "x")
    check(t)


def test_english_action_without_place_does_not_crash():
    """Reference defect: this raised TypeError. It now takes the path the other languages already take."""
    t = make_title(T("I crashed so hard"), [], "chat_burst")
    check(t)
    assert t == "Brutal crash out of nowhere"
    veh = make_title(T("I crashed the Zentorno so hard"), [], "chat_burst")
    assert "Zentorno" in veh
