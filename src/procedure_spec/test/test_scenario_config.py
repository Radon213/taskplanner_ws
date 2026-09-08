from __future__ import annotations

import pytest

from procedure_spec.scenario_config import (
    SCENARIO_CONFIG_SCHEMA,
    parse_scenario_config,
    scenario_config_payload,
)


def test_scenario_config_round_trip_is_small_and_schema_bound() -> None:
    payload = scenario_config_payload(
        bundle_name="thyroidectomy_demo",
        spec_dir="/specs/thyroidectomy_demo",
        revision="sha256:revision",
    )

    parsed = parse_scenario_config(payload)

    assert payload["schema"] == SCENARIO_CONFIG_SCHEMA
    assert parsed.bundle_name == "thyroidectomy_demo"
    assert parsed.spec_dir == "/specs/thyroidectomy_demo"
    assert parsed.revision == "sha256:revision"


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"schema": "wrong"},
        {
            "schema": SCENARIO_CONFIG_SCHEMA,
            "bundle_name": "bundle\nother",
            "spec_dir": "/specs/bundle",
            "revision": "sha256:x",
        },
    ],
)
def test_scenario_config_rejects_malformed_or_unbounded_input(payload) -> None:
    with pytest.raises(ValueError):
        parse_scenario_config(payload)
