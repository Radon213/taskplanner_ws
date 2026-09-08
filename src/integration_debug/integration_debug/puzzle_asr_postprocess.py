"""Puzzle AI ASR vocabulary and transcript normalization.

This module is derived from the 2026-08-20 Puzzle AI ZIP handoff.  It keeps
the received keyword booster and lexical correction table in the Taskplanner
package, while retaining the Taskplanner-specific voice-command vocabulary
needed by the downstream closed-schema interpreter.

Only server-finalized text is normalized.  Partial hypotheses remain
diagnostic data and never enter the Taskplanner voice-input path.  The
operational runtime publishes the canonical result from :func:`correct` for
every finalized utterance.
"""

from __future__ import annotations

import re
from typing import Callable


# The received ZIP's vocabulary is the source for surgical-instrument spelling
# and its correction table.  The direct-teach terms are retained because they
# are part of Taskplanner's existing closed command contract, not an attempt to
# infer a command from free speech.
KEYWORDS: tuple[tuple[str, int], ...] = (
    ("nephrectomy", 7),
    ("직접", 7),
    ("직접 교시", 7),
    ("교시", 7),
    ("direct teach", 7),
    ("teaching", 7),
    #("리트랙션", 9),
    ("retraction", 9),
    ("retractor", 8),
    ("툴 체인지", 9),
    ("tool change", 9),
    ("왼쪽", 8),
    ("오른쪽", 8),
    ("5 센티미터", 8),
    ("Bovie", 8),
    ("Army", 8),
    # ("Metzenbaum", 8),
    ("Allis", 8),
    ("gauze", 7),
    ("forcep", 7),
    ("Mosquito", 9),
    ("Kelly", 7),
    ("bipolar", 7),
    ("Adson", 8),
    ("suction", 7),
    #"Debakey forcep", 8),
    #"smooth forcep", 8),
    #"Malleable", 8),
    ("메스", 7),
    ("thyroid", 6),
    ("thyroidectomy", 7),
    #"Thunderbeat", 8),
    # ("Peanut", 6),
    ("결론에 적으세요", 4),
    ("결론에 적어 주세요", 4),
    ("줄 바꿔서", 4),
    ("history", 4),
    ("tool", 7),
    ("당겨줘", 7),
    ("종료", 7),
    ("빼", 7),
    ("주세요", 8),
    ("most", 4),
    ("1 cm", 7),
    ("줄 바꿔 주세요", 4),
    ("남겨줘", 4),
)


# {canonical spelling: known ASR variants}.  This is the received ZIP's
# correction table; its output is deliberately limited to lexical spelling
# normalization and has no control-side effects.
CORRECTIONS: dict[str, tuple[str, ...]] = {
    "Bovie": (
        "4 B", "4 view", "fovi", "fovic", "a fovic", "fovida", "gobic",
        "bobi", "bobic", "bowbing", "verbit", "fobits", "bob", "xaphobi",
        "자 boviec", "boviek", "bovi", "bo b", "orbit", "forbit", "forbits",
        "bovy", "O B", "OB", "5 B", "borbit", "bovievi", "boy", "movic", "부위", "forming",
    ),
    "Army": (
        "암이", "암 해", "암에", "arm이", "arm 해", "army해", "armyalother",
        "arm year the", "the almi", "almine", "armit", "rma", "ulming",
        "the rb", "amyerdo", "amerado",
    ),
    "Adson": (
        "adjacent", "adison s", "adison", "idison the", "additon", "Adson's",
        "addition", "redism", "adisson", "additionadis", "Addis", "adisent",
        "addison", "adised", "adsending", "additional", "adition", "at distant",
        "Adjance adise", "adds", "adisome", "adhesion", "ediston", "aditon",
        "adisome", "adsent", "endiston", "additis", "adsent", "allison's",
        "adson's", "addits", "addism",
    ),
    "Mosquito": (
        "moskitto", "moskito", "moskit", "moskgito", "moskitter", "moskitton",
        "moskipto", "mosketo", "moscite", "boschito", "muscuto", "muscute",
        "muscutum", "musculo", "massking", "more scattered", "scattered",
        "scatter", "Osquito","squit","osquito", "Mosquitoto", "squtio",
        "most pitto", "Mostkitto", "Moskitto", "boskip", "muscule", "squitor",
        "moskib", "moskip", "both kidney", "skitto", "skito", "bosquito", "squito",
    ),
    "Kelly": ("cally", "calli", "callis", "killi"),
    # The handoff explicitly treats this English token as the instrument
    # homophone within the supported surgical vocabulary.
    "메스": ("mass",),
    # "Malleable": (
    #     "malleble", "mallable", "malleoble", "malleolu", "malleuvel",
    #     "malleuval", "Mallevil s", "malable", "mallebral", "malleobleed",
    #     "malleubal", "malleuble", "malleolar", "만래불을", "만래에 불을",
    #     "만래을 물을", "만래요 물을", "만래부를", "만래우불을", "만래여부를",
    #     "만래우물도", "만래울부를", "만래을부를", "만래울을", "만래음을",
    #     "말래요 물을", "말래요 불을", "말래우불을", "말래울을", "말레요",
    # ),
    "Allis": ("Alice", "Ellis", "the ilis", "illis", "illness"),
    # "Metzenbaum": ("metzen maum", "metzan s", "metzan", "metain", "mets and"),
    "suction": ("obsuction", "suction", "eosion", "suctions"),
    "bipolar": ("biform",),
    # "Peanut": ("P-nut", "P 넣어", "peanut"),
    "tool": ("stool", "full"),
    "당겨줘": ("남겨줘","남겨 줘"),
    "교시": ("교실", "표시"),
    "종료": ("종류",),
    "빼": ("bag",),
    "teaching": (
        "teating", "dizzine", "titching", "tissing", "disching", "tissue",
        "tissuing", "tethius", "titting",
    ),
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

# A correction such as ``부위 -> Bovie`` is helpful only after the speaker has
# independently made a tool request.  Applying it to every final transcript
# would turn a harmless noun into a bare-tool handover.  Likewise, control
# verbs such as ``종류 -> 종료`` or ``남겨줘 -> 당겨줘`` must never be created
# by lexical post-processing: those words are common enough outside the
# reviewed command grammar to be unsafe as a global rewrite.
_CONTROL_SENSITIVE_CANONICALS = frozenset({"당겨줘", "교시", "종료", "빼"})
_TOOL_REQUEST_SUFFIX_RE = re.compile(
    r"^\s*(?:을|를|이|가)?\s*(?:좀|하나+|한\s*개|한개)?\s*"
    r"(?:줘|주세요|주십시오|주실래|전달(?:해|해줘|해주세요)?|내놔|"
    r"가져와(?:줘|주세요)?|handover|hand\s*over|give|please|"
    r"회수(?:해|해줘|해주세요)?|치워(?:줘|주세요)?|치우(?:줘|세요)?|"
    r"정리(?:해|해줘|해주세요)?|retrieve|remove|clear)",
    re.IGNORECASE,
)
_SUCTION_OPERATION_SUFFIX_RE = re.compile(
    r"^\s*(?:을|를|이|가)?\s*(?:좀|하나+)?\s*"
    r"(?:시작|들어와|켜|start|on|빠져|빼|제거|종료|중지|꺼|out|stop|off)",
    re.IGNORECASE,
)


def _correct_selected(
    text: str,
    *,
    allowed_canonicals: frozenset[str] | None = None,
    allow_match: Callable[[re.Match[str], str], bool] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    matches: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        observed = match.group(1)
        canonical = _CANONICAL_TABLE[observed]
        if allowed_canonicals is not None and canonical not in allowed_canonicals:
            return observed
        if allow_match is not None and not allow_match(match, canonical):
            return observed
        matches.append((observed, canonical))
        return canonical

    return _PATTERN.sub(replace, str(text)), matches


def correct(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return the canonical ZIP-compatible final transcript and corrections."""

    return _correct_selected(
        str(text), allowed_canonicals=frozenset(CORRECTIONS)
    )


def correct_for_command(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return an optional context-restricted correction for non-runtime callers.

    A tool spelling may be repaired only when a distinct request/retrieval
    anchor is present.  ``suction`` variants are repaired only next to a
    reviewed suction operation.  The operational ASR runtime does not use
    this helper; it publishes :func:`correct` directly.
    """

    raw = str(text)

    def allow_match(match: re.Match[str], canonical: str) -> bool:
        if canonical in _CONTROL_SENSITIVE_CANONICALS:
            return False
        suffix = raw[match.end() :]
        if canonical == "suction":
            return bool(_SUCTION_OPERATION_SUFFIX_RE.match(suffix))
        # A tool variant is not enough by itself.  It must be immediately
        # followed by an authored handover/retrieve form, which keeps ordinary
        # prose such as ``부위 설명해줘`` from becoming a bare Bovie command.
        return bool(_TOOL_REQUEST_SUFFIX_RE.match(suffix))

    return _correct_selected(raw, allow_match=allow_match)


__all__ = ["KEYWORDS", "CORRECTIONS", "correct", "correct_for_command"]
