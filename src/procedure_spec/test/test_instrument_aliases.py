from __future__ import annotations

from pathlib import Path

from procedure_spec import load_bundle


def _thyroid_spec():
    return load_bundle(
        Path(__file__).parents[1]
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )


def test_distinctive_spoken_aliases_resolve_to_the_named_instrument():
    spec = _thyroid_spec()

    expected = {
        "Adson": "T02",
        "Allis": "T03",
        "Bovie": "T04",
        "army": "T05",
        "Senn": "T06",
        "bipolar": "T07",
        "mosquito": "T08",
        "harmonics": "T09",
        "Yankeur": "T10",
        "애드슨": "T02",
        "보비": "T04",
        "바이폴라": "T07",
    }

    assert {
        alias: spec.resolve_instrument_alias(alias)
        for alias in expected
    } == expected


def test_shared_generic_aliases_are_rejected_as_ambiguous():
    spec = _thyroid_spec()

    for alias in (
        "forceps",
        "포셉",
        "retractor",
        "리트랙터",
        "cautery",
        "전기소작기",
        "suction",
        "석션",
    ):
        assert spec.resolve_instrument_alias(alias) is None

    ambiguous = spec.list_ambiguous_instrument_aliases()
    assert "forceps" not in ambiguous
    assert "retractor" not in ambiguous
    assert "cautery" not in ambiguous


def test_full_names_and_canonical_ids_remain_resolvable():
    spec = _thyroid_spec()

    assert spec.resolve_instrument_alias("Bovie surgical cautery") == "T04"
    assert spec.resolve_instrument_alias("Bipolar cautery") == "T07"
    assert spec.resolve_instrument_alias("Yankeur") == "T10"
    assert spec.resolve_instrument_alias("t 04") == "T04"
