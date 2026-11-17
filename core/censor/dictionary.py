"""Local profanity dictionaries.

Built-in entries are regular expressions over the **normalised** word
(see core.censor.normalize) and are matched with ``re.search``, so a root
also catches its inflections and compounds (``пизд`` → ``пиздец``,
``распиздяй``). ``exceptions`` list ordinary words that happen to contain
a root (``хлеба`` contains ``еба``).

Users add their own words and exclusions; those live in a JSON file in
the user data folder.

Categories:

* ``strong`` – Russian mat and strong English profanity / slurs;
* ``mild`` – rude but often acceptable words (жопа, говно, damn, crap).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.censor.normalize import canonical, collapse_repeats, forms

log = logging.getLogger(__name__)

STRONG = "strong"
MILD = "mild"
CUSTOM = "custom"
CATEGORIES = (STRONG, MILD, CUSTOM)


@dataclass(frozen=True)
class Entry:
    key: str  # short id shown to the user, e.g. "ru:пизд"
    pattern: re.Pattern
    language: str
    category: str
    exceptions: tuple[str, ...] = ()

    def matches(self, form: str) -> bool:
        if not self.pattern.search(form):
            return False
        return not any(e in form for e in self.exceptions)


def _e(key: str, regex: str, language: str, category: str = STRONG, exceptions: tuple[str, ...] = ()) -> Entry:
    return Entry(key, re.compile(regex), language, category, exceptions)


# Shared exceptions for the е-б root ("хлеб", "требую", "колебаться", ...).
_EB_EXCEPTIONS = (
    "хлеб", "треб", "колеб", "греб", "щебет", "ребус", "ребен", "ребят", "себе", "небес", "небо",
    "гребен", "погреб", "отреб", "скребу", "скреб", "дебют", "зебр", "лебед", "вебин", "веб",
    "ребр", "бебе", "фебр", "серебр", "амеб", "стебел", "хлебал", "ребу",
)

BUILTIN: tuple[Entry, ...] = (
    # ---------------- Russian, strong
    _e("ru:хуй", r"х[уy][йеияюё]|х[уy]л[ие]|хуе|хуя", "ru",
       exceptions=("страху", "застрах", "хуление", "хулиг", "хулит")),
    _e("ru:пизд", r"п[иеё]зд", "ru"),
    _e("ru:еб", r"(^|[аеиоуыэюяъьзсдтвн])[её]б(а[нлтшкчв]|у[тчнлшк]?|л[аиоя]|н[уеи]|ыр|ыв|ись|ец|ет|ён|ош|ыч|ук)", "ru",
       exceptions=_EB_EXCEPTIONS),
    _e("ru:бля", r"^(вы|вы)?бл[яе][дт]|^бля$|^бля[хк]?[аиуеоь]?$|^бляд|^блят", "ru", exceptions=("бляха", "блях")),
    _e("ru:муд", r"мудак|мудил|мудоз|мудач|мудо[йж]", "ru"),
    _e("ru:залуп", r"залуп", "ru"),
    _e("ru:гандон", r"г[ао]нд[ао]н", "ru"),
    _e("ru:шлюх", r"шлюх|шлюш", "ru"),
    _e("ru:пидор", r"п[иеё]д[оа]р|п[иеё]дрил|педик", "ru"),
    _e("ru:дроч", r"дроч", "ru"),
    _e("ru:манда", r"^манд(а|ы|у|е|ой|ах)?$|^мандав", "ru"),
    _e("ru:сука", r"^сук(а|и|у|е|ой|ам|ами|ах)$|^сучк|^сучар|^сучь|^сучий|^сукин", "ru"),
    _e("ru:уеб", r"у[её]бищ|у[её]бок|у[её]бан", "ru"),
    # ---------------- Russian, mild
    _e("ru:хер", r"^(на|по|ни|вы|за)?хер|херн|херов", "ru", MILD, exceptions=("херсон", "херес")),
    _e("ru:жопа", r"жоп", "ru", MILD),
    _e("ru:говно", r"говн|дерьм", "ru", MILD),
    _e("ru:срать", r"^(на|по|вы|об|за)?сра(ть|л|ла|ли|ло|нь|ка)|^ссать|^ссыкл|обосс|зассан", "ru", MILD),
    _e("ru:дебил", r"дебил|ублюд|чмо$|^чмошн", "ru", MILD),
    # ---------------- English, strong
    _e("en:fuck", r"f+u+c+k|fuk|fck|phuck|motherf", "en"),
    _e("en:shit", r"shit|sh1t|bullshit", "en", exceptions=("shitake", "shiitake")),
    _e("en:bitch", r"bitch|biatch", "en"),
    _e("en:cunt", r"cunt", "en", exceptions=("scunthorpe",)),
    _e("en:asshole", r"assh[o0]le|^arsehole", "en"),
    _e("en:nigger", r"nigg(a|er|uh)", "en", exceptions=("niggl",)),
    _e("en:faggot", r"^fag(s|got|gots)?$", "en"),
    _e("en:whore", r"wh[o0]re|^slut", "en"),
    _e("en:cock", r"^cock(sucker|s)?$|^dickhead", "en"),
    _e("en:bastard", r"bastard", "en"),
    # ---------------- English, mild
    _e("en:damn", r"^(god)?damn", "en", MILD),
    _e("en:crap", r"^crap", "en", MILD),
    _e("en:piss", r"^piss", "en", MILD),
    _e("en:dick", r"^dicks?$", "en", MILD),
    _e("en:ass", r"^ass(es)?$|^jackass|^dumbass", "en", MILD),
    # ---------------- Already bleeped by the recogniser: "б**дь", "f***"
    _e("masked", r"[а-яa-z]\*|\*[а-яa-z]", "any"),
)


@dataclass(frozen=True)
class Match:
    entry_key: str
    category: str
    language: str
    form: str  # the normalised spelling that matched


def _custom_entry(line: str) -> Entry | None:
    """User entry: ``word`` (exact), ``word*`` (prefix), ``*word*`` (contains)."""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    prefix_wild, suffix_wild = raw.startswith("*"), raw.endswith("*")
    core = raw.strip("*")
    norm = forms(core)
    if not norm:
        return None
    word = collapse_repeats(norm[0])
    body = re.escape(word)
    regex = ("" if prefix_wild else "^") + body + ("" if suffix_wild else "$")
    language = "ru" if re.search(r"[а-я]", word) else "en"
    return Entry(f"custom:{raw}", re.compile(regex), language, CUSTOM)


@dataclass
class Dictionary:
    """Built-in entries plus the user's custom words and exclusions."""

    custom_words: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)

    def entries(self) -> list[Entry]:
        result = list(BUILTIN)
        for line in self.custom_words:
            entry = _custom_entry(line)
            if entry is not None:
                result.append(entry)
        return result

    def excluded(self, word: str) -> bool:
        key = canonical(word)
        return bool(key) and key in {canonical(x) for x in self.exclusions if x.strip()}

    def add_exclusion(self, word: str) -> bool:
        if not word.strip() or self.excluded(word):
            return False
        self.exclusions.append(word.strip())
        return True

    def match(self, word: str, categories: tuple[str, ...] = (STRONG, CUSTOM)) -> Match | None:
        """First entry matching any spelling of ``word``, or None."""
        if self.excluded(word):
            return None
        variants = forms(word)
        if not variants:
            return None
        entries = [e for e in self.entries() if e.category in categories]
        # Custom entries first: they are the user's explicit intent.
        entries.sort(key=lambda e: e.category != CUSTOM)
        for entry in entries:
            for form in variants:
                if entry.matches(form):
                    return Match(entry.key, entry.category, entry.language, form)
        return None

    # ------------------------------------------------------------ storage
    def to_dict(self) -> dict:
        return {"version": 1, "custom_words": self.custom_words, "exclusions": self.exclusions}

    @classmethod
    def load(cls, path: Path) -> "Dictionary":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            log.warning("Could not read censor dictionary %s: %s", path, exc)
            return cls()
        if not isinstance(raw, dict):
            return cls()
        words = [str(w) for w in raw.get("custom_words") or [] if isinstance(w, str)]
        exclusions = [str(w) for w in raw.get("exclusions") or [] if isinstance(w, str)]
        return cls(words, exclusions)

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
