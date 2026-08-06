from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_runtime_campaign",
    ROOT / "scripts" / "release_runtime_campaign.py",
)
assert SPEC is not None and SPEC.loader is not None
campaign = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = campaign
SPEC.loader.exec_module(campaign)


def test_parse_memory_bytes_supports_docker_units():
    assert campaign.parse_memory_bytes("512B / 1GiB") == 512
    assert campaign.parse_memory_bytes("1.5MiB / 31.25GiB") == 1572864
    assert campaign.parse_memory_bytes("2GB / 8GB") == 2_000_000_000


def test_memory_growth_uses_stable_windows_and_enforces_limit():
    samples = []
    for index, value in enumerate((100, 100, 101, 119, 120), start=1):
        samples.append(
            {
                "elapsed_sec": float(index * 10),
                "containers": [
                    {"container": "taskplanner-runtime", "memory_used_bytes": value}
                ],
            }
        )

    result = campaign.summarize_memory_growth(
        samples,
        warmup_sec=0.0,
        limit_percent=10.0,
    )

    assert result["status"] == "failed"
    assert result["violating_container_count"] == 1
    assert result["containers"][0]["growth_percent"] == 20.0


def test_memory_growth_reports_short_smoke_as_not_evaluated():
    result = campaign.summarize_memory_growth(
        [
            {
                "elapsed_sec": 1.0,
                "containers": [
                    {"container": "taskplanner-runtime", "memory_used_bytes": 100}
                ],
            }
        ],
        warmup_sec=0.0,
        limit_percent=10.0,
    )

    assert result["status"] == "not_evaluated"
    assert result["containers"][0]["status"] == "insufficient_samples"
