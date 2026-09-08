from __future__ import annotations

import sys
from pathlib import Path

from procedure_spec import load_bundle, load_frozen_handover_ngram_prior


TASK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK_DIR))

import thyroidectomy_location_policy_census as census  # noqa: E402


def test_default_dt_policy_matches_requested_location_census_mix() -> None:
    spec = load_bundle(census.DEFAULT_SPEC_DIR)
    prior = load_frozen_handover_ngram_prior(spec, census.DEFAULT_SPEC_DIR)
    assert prior is not None
    tool_ids = tuple(spec.list_requestable_instrument_ids())
    context_rows = census.contexts(prior, tool_ids)

    actions, by_tool, conflicts = census.summarize(
        context_rows,
        tool_ids,
        prepare_threshold=0.125,
        recovery_threshold=0.391,
        recovery_enabled_tools=frozenset({"T02", "T08"}),
        mayo_hand_present=False,
    )

    assert len(context_rows) == 37
    assert actions == {"prepare": 1807, "recover": 813, "wait": 377}
    assert conflicts == 1127
    assert [by_tool[("prepare", tool_id)] for tool_id in tool_ids] == [
        297,
        980,
        332,
        198,
    ]
    assert [by_tool[("recover", tool_id)] for tool_id in tool_ids] == [
        405,
        0,
        0,
        408,
    ]
