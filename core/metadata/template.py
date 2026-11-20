"""Template text with ``{variables}`` and optional sections.

Rules, in order:

* ``{{`` and ``}}`` are literal braces.
* ``[[ … ]]`` is an optional section: it disappears when any variable in it
  is empty, otherwise only the brackets go. Sections may span lines.
* A line whose variables are all empty is dropped, so ``Game: {game}`` does
  not leave a bare label behind. Lines without variables always stay.
* Unknown variables count as empty; ``unknown_variables`` lists them so the
  editor can warn about typos.
* Runs of blank lines collapse to one; trailing spaces go.
"""

from __future__ import annotations

import re

VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
SECTION = re.compile(r"\[\[(.*?)\]\]", re.DOTALL)
_OPEN, _CLOSE = "\x00", "\x01"

BUILTIN_VARIABLES = (
    "streamer", "streamer_username", "streamer_channel_url", "streamer_twitch_url", "game", "stream_type",
    "participants", "participants_links", "participants_twitch", "stream_url", "clip_title", "date", "hashtags",
)
# Characters that make no sense at the start or end of a title once a value is missing.
_TITLE_EDGES = " \t-—–|·:,/+&"


def _protect(text: str) -> str:
    return text.replace("{{", _OPEN).replace("}}", _CLOSE)


def _restore(text: str) -> str:
    return text.replace(_OPEN, "{").replace(_CLOSE, "}")


def variables_in(text: str) -> list[str]:
    seen: list[str] = []
    for name in VARIABLE.findall(_protect(text)):
        if name not in seen:
            seen.append(name)
    return seen


def unknown_variables(text: str, known) -> list[str]:
    known = set(known)
    return [v for v in variables_in(text) if v not in known]


def _value(values: dict, name: str) -> str:
    value = values.get(name)
    return "" if value is None else str(value)


def _substitute(text: str, values: dict) -> str:
    return VARIABLE.sub(lambda m: _value(values, m.group(1)).replace("{", _OPEN).replace("}", _CLOSE), text)


def render(template: str, values: dict) -> str:
    text = _protect(template.replace("\r\n", "\n"))

    def section(match: re.Match) -> str:
        body = match.group(1)
        names = VARIABLE.findall(body)
        if any(not _value(values, n).strip() for n in names):
            return ""
        return body

    text = SECTION.sub(section, text)
    lines = []
    for line in text.split("\n"):
        names = VARIABLE.findall(line)
        if names and all(not _value(values, n).strip() for n in names):
            continue
        lines.append(_substitute(line, values).rstrip())
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    return _restore(text)


def render_title(template: str, values: dict) -> str:
    """One line: newlines become spaces, doubled spaces and dangling separators go."""
    flat = {k: " ".join(str(v).split()) if v is not None else "" for k, v in values.items()}
    text = render(template.replace("\n", " "), flat)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)  # "()" left by a missing value
    return text.strip(_TITLE_EDGES).strip()
