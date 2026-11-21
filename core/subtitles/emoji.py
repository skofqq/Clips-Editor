"""Emoji for captions: a word that matches a cue (laughter, fire, money, death…)
puts an emoji above the caption, as many Shorts do.

Cues are word stems in Russian and English, matched at the start of a word,
so «жесть», «жёстко» and «жестко» all match «жест»/«жёст»."""

from __future__ import annotations

import re

# (emoji, stems). The first cue that matches a caption wins.
CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("😂", ("ахах", "хаха", "хех", "ржу", "ржак", "ору", "орнул", "смешн", "угар", "lol", "lmao", "haha", "rofl")),
    ("💀", ("умер", "умир", "смерт", "убил", "убит", "труп", "dead", "died", "kill")),
    ("🔥", ("жест", "жёст", "огон", "огнен", "пожар", "имба", "fire", "insane", "крут")),
    ("😱", ("вау", "офиг", "обалд", "шок", "ужас", "капец", "кошмар", "wow", "omg", "no way")),
    ("🏆", ("клатч", "побед", "выиграл", "выигр", "чемпион", "clutch", "win", "won", "ace", "эйс")),
    ("💰", ("деньг", "денег", "бабк", "бабл", "миллион", "доллар", "рубл", "донат", "money", "cash")),
    ("❤️", ("люблю", "любов", "любим", "обожа", "love")),
    ("😡", ("бесит", "злой", "злюс", "ненавиж", "angry", "hate")),
    ("😭", ("плач", "плак", "грустн", "печаль", "cry", "sad")),
    ("🤔", ("почему", "зачем", "думаю", "хмм", "why", "hmm")),
    ("🚗", ("машин", "тачк", "car")),
    ("🔫", ("стрел", "выстрел", "пистолет", "автомат", "shoot", "gun")),
    ("🎉", ("ура", "поздрав", "праздн", "party")),
    ("👀", ("смотри", "гляди", "look")),
    ("🤡", ("клоун", "clown")),
    ("🐐", ("goat", "легенд")),
)

_WORD = re.compile(r"[\w']+", re.UNICODE)


def _norm(text: str) -> str:
    return text.casefold().replace("ё", "е")


_PATTERNS = tuple(
    (emoji, re.compile(r"(?<![\w])(" + "|".join(re.escape(_norm(s)) for s in stems) + r")", re.UNICODE))
    for emoji, stems in CUES
)


def emoji_for(words) -> str:
    """The emoji for a caption's words (masked words never count), or ""."""
    text = " ".join(_norm(w) for w in words if w and "*" not in w)
    if not _WORD.search(text):
        return ""
    for emoji, pattern in _PATTERNS:
        if pattern.search(text):
            return emoji
    return ""


def emoji_match(words) -> tuple[str, int]:
    """The emoji for a caption and the index of the word that called it, or ("", -1)."""
    normed = [_norm(w) if w and "*" not in w else "" for w in words]
    for emoji, pattern in _PATTERNS:
        for index, word in enumerate(normed):
            if word and pattern.search(word):
                return emoji, index
    # Cues of two words ("no way") only match across the words.
    emoji = emoji_for(words)
    return (emoji, len(normed) - 1) if emoji else ("", -1)


_NUMBER = re.compile(r"\d")


def is_key_word(text: str) -> bool:
    """A word worth its own colour: an emoji cue (laughter, «жесть», money…) or a number."""
    if not text or "*" in text:
        return False
    if _NUMBER.search(text):
        return True
    word = _norm(text)
    return any(pattern.search(word) for _emoji, pattern in _PATTERNS)
