from __future__ import annotations

from pathlib import Path

from procedure_spec import load_voice_command_catalog, voice_catalog_id_for


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


def test_voice_catalog_contains_only_local_alias_and_retraction_data() -> None:
    thyroid = load_voice_command_catalog(_spec_root() / "thyroidectomy_demo")
    retraction_only = load_voice_command_catalog(
        _spec_root() / "inguinal_hernia_repair_demo"
    )

    assert thyroid.tool_aliases
    assert retraction_only.tool_aliases == {}
    assert "change_tool" in thyroid.retractor_commands
    assert "change_tool" not in retraction_only.retractor_commands
    assert not hasattr(thyroid, "enabled_command_ids")
    assert not hasattr(thyroid, "catalog_schema_version")


def test_voice_catalog_hash_is_stable_for_local_vocabulary() -> None:
    aliases = {"T04": ("보비", "bovie")}

    assert voice_catalog_id_for("case", aliases) == (
        "sha256:1d0c4ee1474722861d549650dcd147be493b75c359503c8d97878a68f42c24e8"
    )
    assert voice_catalog_id_for(
        "case",
        aliases,
        retractor_commands=("stop_retraction", "adjust_retraction"),
        retractor_max_distance_m=0.03,
        retractor_require_explicit_unit=True,
    ) == voice_catalog_id_for(
        "case",
        aliases,
        retractor_commands=("adjust_retraction", "stop_retraction"),
        retractor_max_distance_m=0.03,
        retractor_require_explicit_unit=True,
    )
    assert voice_catalog_id_for(
        "case",
        aliases,
        retractor_commands=("adjust_retraction",),
        retractor_max_distance_m=0.03,
        retractor_require_explicit_unit=True,
    ) != voice_catalog_id_for(
        "case",
        aliases,
        retractor_commands=("adjust_retraction",),
        retractor_max_distance_m=0.02,
        retractor_require_explicit_unit=True,
    )
