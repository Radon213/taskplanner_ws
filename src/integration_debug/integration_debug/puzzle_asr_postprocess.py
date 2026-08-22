"""Puzzle AI ASR vocabulary and transcript normalization.

This module is derived from the 2026-08-20 Puzzle AI ZIP handoff.  It keeps
the received keyword booster and lexical correction table in the Taskplanner
package, while retaining the Taskplanner-specific voice-command vocabulary
needed by the downstream closed-schema interpreter.

Only server-finalized text is normalized by the runtime.  Partial hypotheses
remain diagnostic data and never enter the Taskplanner voice-input path.
"""

from __future__ import annotations

import re


# The received ZIP's vocabulary is the source for surgical-instrument spelling
# and its correction table.  The direct-teach terms are retained because they
# are part of Taskplanner's existing closed command contract, not an attempt to
# infer a command from free speech.
KEYWORDS: tuple[tuple[str, int], ...] = (
    ("nephrectomy", 7),
    ("직접 교시", 9),
    ("direct teach", 9),
    ("리트랙션", 9),
    ("retraction", 9),
    ("retractor", 8),
    ("툴 체인지", 9),
    ("tool change", 9),
    ("왼쪽", 8),
    ("오른쪽", 8),
    ("5 센티미터", 8),
    ("Bovie", 7),
    ("Army", 8),
    ("Metzenbaum", 8),
    ("Allis", 8),
    ("gauze", 7),
    ("forcep", 7),
    ("Mosquito", 8),
    ("Kelly", 7),
    ("bipolar", 7),
    ("Adson", 8),
    ("suction", 7),
    ("Debakey forcep", 8),
    ("smooth forcep", 8),
    ("Malleable", 8),
    ("메스", 7),
    ("scissor", 7),
    ("thyroid", 6),
    ("thyroidectomy", 7),
    ("Thunderbeat", 8),
    ("Peanut", 6),
)


# {canonical spelling: known ASR variants}.  This is the received ZIP's
# correction table; its output is deliberately limited to lexical spelling
# normalization and has no control-side effects.
CORRECTIONS: dict[str, tuple[str, ...]] = {
    "Bovie": (
        "4 B", "4 view", "fovi", "fovic", "a fovic", "fovida", "gobic",
        "bobi", "bobic", "bowbing", "verbit", "fobits", "bob", "xaphobi",
        "자 boviec", "boviek", "bovi", "bo b", "orbit", "forbit", "forbits",
        "bovy",
    ),
    "Army": (
        "암이", "암 해", "암에", "arm이", "arm 해", "army해", "armyalother",
        "arm year the", "the almi", "almine", "armit", "rma", "ulming",
        "the rb", "amyerdo", "amerado", "bag ami",
    ),
    "Adson": ("adjacent", "adison s", "adison", "idison the", "additon"),
    "Mosquito": (
        "moskitto", "moskito", "moskit", "moskgito", "moskitter", "moskitton",
        "moskipto", "mosketo", "moscite", "boschito", "muscuto", "muscute",
        "muscutum", "musculo", "massking", "more scattered", "scattered",
        "scatter",
    ),
    "Kelly": ("cally", "calli", "callis", "killi"),
    # The handoff explicitly treats this English token as the instrument
    # homophone within the supported surgical vocabulary.
    "메스": ("mass",),
    "Malleable": (
        "malleble", "mallable", "malleoble", "malleolu", "malleuvel",
        "malleuval", "Mallevil s", "malable", "mallebral", "malleobleed",
        "malleubal", "malleuble", "malleolar", "만래불을", "만래에 불을",
        "만래을 물을", "만래요 물을", "만래부를", "만래우불을", "만래여부를",
        "만래우물도", "만래울부를", "만래을부를", "만래울을", "만래음을",
        "말래요 물을", "말래요 불을", "말래우불을", "말래울을", "말레요",
    ),
    "Allis": ("Alice", "Ellis", "the ilis", "illis", "illness"),
    "Metzenbaum": ("metzen maum", "metzan s", "metzan", "metain", "mets and"),
    "suction": ("obsuction", "suction", "eosion", "suctions"),
    "bipolar": ("biform",),
    "scissor": ("seizure",),
    "Peanut": ("P-nut", "P 넣어", "peanut"),
}


def _case_forms(value: str) -> set[str]:
    return {value, value.lower(), value.upper(), value.capitalize(), value.title()}


def _build_canonical_table() -> dict[str, str]:
    known = {keyword for keyword, _sensitivity in KEYWORDS}
    table: dict[str, str] = {}
    for canonical, variants in CORRECTIONS.items():
        if canonical not in known:
            raise ValueError(
                f"ASR correction canonical spelling is not a keyword: {canonical!r}"
            )
        for variant in variants:
            for form in _case_forms(variant):
                if not form or form == canonical:
                    continue
                previous = table.get(form)
                if previous is not None and previous != canonical:
                    raise ValueError(
                        f"ASR correction variant {form!r} maps to both "
                        f"{previous!r} and {canonical!r}"
                    )
                table[form] = canonical
    for canonical, _sensitivity in KEYWORDS:
        for form in _case_forms(canonical):
            if form == canonical:
                continue
            previous = table.get(form)
            if previous is not None and previous != canonical:
                raise ValueError(
                    f"ASR keyword case variant {form!r} conflicts with {previous!r}"
                )
            table[form] = canonical
    return table


_CANONICAL_TABLE = _build_canonical_table()
_VARIANTS = sorted(_CANONICAL_TABLE, key=len, reverse=True)
_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(re.escape(item) for item in _VARIANTS) + r")(?![A-Za-z0-9])"
)


def correct(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return the ZIP-compatible canonical transcript and applied corrections."""

    matches: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        observed = match.group(1)
        canonical = _CANONICAL_TABLE[observed]
        matches.append((observed, canonical))
        return canonical

    return _PATTERN.sub(replace, str(text)), matches
