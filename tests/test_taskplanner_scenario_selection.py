from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "taskplanner_scenario_selection.py"
SPEC = importlib.util.spec_from_file_location("taskplanner_scenario_selection_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
selection = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = selection
SPEC.loader.exec_module(selection)


def test_reads_the_small_last_good_selection_only(tmp_path: Path) -> None:
    path = tmp_path / "selected_bundle.json"
    path.write_text(
        json.dumps(
            {
                "schema": selection.SELECTION_SCHEMA,
                "bundle_name": "thyroidectomy_demo",
                "revision": "sha256:ignored-by-launcher",
            }
        ),
        encoding="utf-8",
    )

    assert selection.load_persisted_bundle(path) == "thyroidectomy_demo"


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        json.dumps({"schema": "wrong", "bundle_name": "thyroidectomy"}).encode(),
        json.dumps(
            {
                "schema": selection.SELECTION_SCHEMA,
                "bundle_name": "../outside",
            }
        ).encode(),
        json.dumps(
            {
                "schema": selection.SELECTION_SCHEMA,
                "bundle_name": "a..b",
            }
        ).encode(),
    ],
)
def test_rejects_invalid_or_path_like_selection_state(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "selected_bundle.json"
    path.write_bytes(payload)

    assert selection.load_persisted_bundle(path) is None


def test_rejects_oversized_selection_state(tmp_path: Path) -> None:
    path = tmp_path / "selected_bundle.json"
    path.write_bytes(b"x" * (selection.MAX_SELECTION_BYTES + 1))

    assert selection.load_persisted_bundle(path) is None
