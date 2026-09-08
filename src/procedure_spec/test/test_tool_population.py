from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from procedure_spec import (
    InstrumentPopulationSpec,
    compact_procedure_prompt,
    load_bundle,
)
from procedure_spec.prompt_bundle import build_raw_bundle_from_prompt
from procedure_spec.validator import SpecValidationError, validate_raw_bundle


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


def _population_prompt(
    tmp_path: Path,
    population: object,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source_bundle = _spec_root() / "inguinal_hernia_repair_demo"
    prompt = yaml.safe_load(
        (source_bundle / "vlm_procedure_prompt.yaml").read_text(
            encoding="utf-8"
        )
    )
    prompt["tool_population"] = population

    # load_bundle resolves the shared display catalog from the candidate's
    # parent, just like an installed specs directory.
    (tmp_path / "display_catalog.yaml").write_text(
        (_spec_root() / "display_catalog.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    candidate = tmp_path / "population_prompt"
    candidate.mkdir()
    (candidate / "vlm_procedure_prompt.yaml").write_text(
        yaml.safe_dump(prompt, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return candidate


def _raw_population_bundle(tmp_path: Path) -> dict:
    candidate = _population_prompt(
        tmp_path,
        {"T01": {"initial_count": 0, "capacity": 3}},
    )
    display_catalog = yaml.safe_load(
        (tmp_path / "display_catalog.yaml").read_text(encoding="utf-8")
    )
    return build_raw_bundle_from_prompt(candidate, display_catalog)


def _instrument(raw_bundle: dict, instrument_id: str) -> dict:
    return next(
        instrument
        for instrument in raw_bundle["instruments"]["instruments"]
        if instrument["id"] == instrument_id
    )


def test_legacy_inventory_remains_fixed_and_query_compatible() -> None:
    spec = load_bundle(_spec_root() / "inguinal_hernia_repair_demo")

    assert spec.get_inventory_count("T03") == 2
    assert spec.get_tool_inventory()["T03"] == 2
    assert spec.get_inventory_capacity("T03") == 2
    assert spec.get_tool_inventory_capacity()["T03"] == 2
    assert spec.is_exchangeable_population("T03") is False
    assert spec.get_tool_population("T03") == InstrumentPopulationSpec(
        initial_count=2,
        capacity=2,
        exchangeable=False,
    )


def test_exchangeable_population_loader_model_and_query_contract(
    tmp_path: Path,
) -> None:
    candidate = _population_prompt(
        tmp_path,
        {
            "T01": {"initial_count": 0, "capacity": 3},
            "T06": {"initial_count": 2, "capacity": 4},
        },
    )

    spec = load_bundle(candidate)

    # Existing DT/EIR consumers still receive only materialized initial tools.
    assert spec.get_inventory_count("T01") == 0
    assert spec.get_tool_inventory()["T01"] == 0
    assert spec.get_inventory_count("T06") == 2
    # Observation-oriented consumers can separately allocate the larger pool.
    assert spec.get_inventory_capacity("T01") == 3
    assert spec.get_tool_inventory_capacity()["T06"] == 4
    assert spec.is_exchangeable_population("T01") is True
    assert spec.is_exchangeable_population("T02") is False
    assert spec.get_tool_population("T01") == InstrumentPopulationSpec(
        initial_count=0,
        capacity=3,
        exchangeable=True,
    )
    assert spec.get_tool_populations()["T02"] == InstrumentPopulationSpec(
        initial_count=1,
        capacity=1,
        exchangeable=False,
    )
    instrument = next(
        item for item in spec.bundle.instruments if item.id == "T01"
    )
    assert instrument.population == spec.get_tool_population("T01")

    # A zero-initial population reserves observation slots without inventing a
    # home-rack observation or exposing a controller-addressable instance.
    bootstrap = spec.get_mock_perception_stages()[0]
    assert "T01" not in {
        observation.instrument_id for observation in bootstrap.observations
    }
    assert compact_procedure_prompt(candidate)["tools"]["T01"]["q"] == 0


@pytest.mark.parametrize(
    ("population", "message"),
    [
        ({"T99": {"initial_count": 0, "capacity": 1}}, "unknown tools"),
        ({"T01": 2}, "T01 must be a mapping"),
        ({"T01": {"capacity": 2}}, "requires initial_count"),
        (
            {"T01": {"initial_count": -1, "capacity": 2}},
            "initial_count must be a non-negative integer",
        ),
        (
            {"T01": {"initial_count": 3, "capacity": 2}},
            "cannot exceed capacity",
        ),
        (
            {"T01": {"initial_count": 1, "capacity": 0}},
            "capacity must be a positive integer",
        ),
        (
            {"T01": {"initial_count": 1, "capacity": 64}},
            "total tool population capacity must be at most 64",
        ),
    ],
)
def test_population_prompt_rejects_invalid_authoring(
    tmp_path: Path,
    population: object,
    message: str,
) -> None:
    candidate = _population_prompt(tmp_path, population)

    with pytest.raises(ValueError, match=message):
        load_bundle(candidate)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            {"inventory_count": -1},
            "inventory_count must be a non-negative integer",
        ),
        (
            {"inventory_count": 4, "inventory_capacity": 3},
            "inventory_count cannot exceed inventory_capacity 3",
        ),
        (
            {"inventory_capacity": 0},
            "inventory_capacity must be a positive integer",
        ),
        (
            {"exchangeable_population": "yes"},
            "exchangeable_population must be boolean",
        ),
    ],
)
def test_raw_bundle_validator_rejects_invalid_population_metadata(
    tmp_path: Path,
    mutation: dict,
    message: str,
) -> None:
    raw_bundle = _raw_population_bundle(tmp_path)
    _instrument(raw_bundle, "T01").update(mutation)

    with pytest.raises(SpecValidationError, match=message):
        validate_raw_bundle(raw_bundle)


def test_raw_bundle_validator_requires_capacity_and_enforces_global_bound(
    tmp_path: Path,
) -> None:
    raw_bundle = _raw_population_bundle(tmp_path)
    population = _instrument(raw_bundle, "T01")
    population.pop("inventory_capacity")
    with pytest.raises(SpecValidationError, match="requires inventory_capacity"):
        validate_raw_bundle(raw_bundle)

    raw_bundle = _raw_population_bundle(tmp_path / "second")
    _instrument(raw_bundle, "T01")["inventory_capacity"] = 64
    with pytest.raises(
        SpecValidationError,
        match="total tool population capacity must be at most 64",
    ):
        validate_raw_bundle(raw_bundle)


def test_fixed_raw_inventory_cannot_smuggle_extra_capacity(
    tmp_path: Path,
) -> None:
    raw_bundle = _raw_population_bundle(tmp_path)
    fixed = _instrument(raw_bundle, "T02")
    fixed["inventory_capacity"] = fixed["inventory_count"] + 1

    with pytest.raises(
        SpecValidationError,
        match="fixed inventory_capacity must equal inventory_count",
    ):
        validate_raw_bundle(raw_bundle)
