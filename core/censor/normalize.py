"""Text normalisation for profanity matching.

The same normalisation is applied to dictionary entries and to recognised
words, so spelling variants meet in the middle:

* case, ``ё`` → ``е``, punctuation stripped;
* Latin look-alike letters inside Cyrillic words (``cyкa`` → ``сука``);
* digits used as letters (``п1зд3ц`` → ``пиздец``);
* stretched letters collapsed (``бляяяять`` → ``блять``, ``fuuuck`` → ``fuck``);
* Latin transliteration of Russian (``blyat`` → ``блят``) as an extra form.
"""

from __future__ import annotations

import re
import unicodedata

_CYRILLIC = re.compile(r"[а-яё]")
_LATIN = re.compile(r"[a-z]")
_KEEP = re.compile(r"[^0-9a-zа-яё*]+")

# Latin letters that look like Cyrillic ones.
_LOOKALIKE = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у", "u": "и", "n": "п", "r": "г",
})
_DIGITS_CYR = str.maketrans({"0": "о", "1": "и", "3": "з", "4": "ч", "6": "б", "8": "в", "@": "а"})
_DIGITS_LAT = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})

# Latin transliteration of Russian, longest sequences first.
_TRANSLIT = [
    ("shch", "щ"), ("sch", "щ"), ("yo", "е"), ("jo", "е"), ("zh", "ж"), ("kh", "х"), ("ch", "ч"),
    ("sh", "ш"), ("ts", "ц"), ("yu", "ю"), ("ju", "ю"), ("ya", "я"), ("ja", "я"), ("ye", "е"),
    ("a", "а"), ("b", "б"), ("v", "в"), ("w", "в"), ("g", "г"), ("d", "д"), ("e", "е"), ("z", "з"),
    ("i", "и"), ("j", "й"), ("y", "ы"), ("k", "к"), ("q", "к"), ("l", "л"), ("m", "м"), ("n", "н"),
    ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("f", "ф"), ("h", "х"),
    ("c", "ц"), ("x", "кс"),
]


def collapse_repeats(text: str) -> str:
    """Collapse runs of the same letter: ``сууука`` → ``сука``."""
    return re.sub(r"(.)\1+", r"\1", text)


def _base(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return _KEEP.sub("", text)


def transliterate(latin: str) -> str:
    out, i = [], 0
    while i < len(latin):
        for src, dst in _TRANSLIT:
            if latin.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(latin[i])
            i += 1
    result = "".join(out)
    # "y" before a vowel or at the end of a word is usually "й" ("huy", "nahuy").
    return re.sub(r"ы(?=[аеиоуэюя]|$)", "й", result)


def forms(word: str) -> list[str]:
    """Normalised spellings of ``word`` to test against the dictionaries.

    The first form is the canonical one; extra forms cover look-alike
    letters, digits and transliteration. Each is also given with repeated
    letters collapsed.
    """
    base = _base(word)
    if not base:
        return []
    candidates = [base]
    has_cyr = bool(_CYRILLIC.search(base))
    has_lat = bool(_LATIN.search(base))
    if has_cyr:
        candidates.append(base.translate(_LOOKALIKE).translate(_DIGITS_CYR))
    elif has_lat:
        latin = base.translate(_DIGITS_LAT)
        candidates.append(latin)
        candidates.append(transliterate(latin))
    else:  # digits / symbols only
        candidates.append(base.translate(_DIGITS_CYR))
    result: list[str] = []
    for item in candidates:
        for variant in (item, collapse_repeats(item)):
            if variant and variant not in result:
                result.append(variant)
    return result


def canonical(word: str) -> str:
    """Single key used for exclusion lists and de-duplication."""
    found = forms(word)
    return collapse_repeats(found[1] if len(found) > 1 and _CYRILLIC.search(found[0] or "") else found[0]) if found else ""


def language_of(word: str) -> str:
    base = _base(word)
    if _CYRILLIC.search(base):
        return "ru"
    if _LATIN.search(base):
        return "en"
    return ""
