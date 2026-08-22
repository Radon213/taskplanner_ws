from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import tools.pnu_live_preflight as preflight
from tools.pnu_live_preflight import (
    EXIT_INVALID,
    EXIT_MODEL_NOT_READY,
    PreflightError,
    check_compose_model_pins,
    validate_model_digest_pins,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _compose_config(
    model_root: Path,
    digests: dict[str, str],
    *,
    read_only: bool = True,
) -> dict[str, object]:
    return {
        "services": {
            "taskplanner-runtime": {
                "environment": {
                    "PNU_EXPECTED_MODEL_DIGESTS_JSON": json.dumps(
                        digests, separators=(",", ":")
                    )
                }
            },
            "pnu-perception": {
                "environment": {
                    "PNU_TOOL_CHECKPOINT": "/models/tool.pth",
                    "PNU_BLOOD_CHECKPOINT": "/models/blood.pth",
                    "PNU_HAND_MODEL": "/models/hand.task",
                },
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(model_root),
                        "target": "/models",
                        "read_only": read_only,
                    }
                ],
            },
        }
    }


def test_digest_pins_require_every_requested_full_lowercase_sha256() -> None:
    digests = {name: character * 64 for name, character in zip(
        ("tool", "blood", "hand"), ("a", "b", "c"), strict=True
    )}
    assert validate_model_digest_pins(json.dumps(digests)) == digests

    with pytest.raises(PreflightError) as missing:
        validate_model_digest_pins(json.dumps({"tool": digests["tool"]}))
    assert missing.value.error_code == "MODEL_DIGEST_PIN_MISSING"
    assert missing.value.exit_code == EXIT_MODEL_NOT_READY

    invalid = dict(digests)
    invalid["tool"] = "A" * 64
    with pytest.raises(PreflightError) as noncanonical:
        validate_model_digest_pins(json.dumps(invalid))
    assert noncanonical.value.error_code == "INVALID_MODEL_DIGEST_PINS"
    assert noncanonical.value.exit_code == EXIT_INVALID

    invalid["tool"] = "a" * 63
    with pytest.raises(PreflightError) as partial:
        validate_model_digest_pins(json.dumps(invalid))
    assert partial.value.error_code == "INVALID_MODEL_DIGEST_PINS"

    duplicate_json = (
        f'{{"tool":"{digests["tool"]}","tool":"{digests["tool"]}",'
        f'"blood":"{digests["blood"]}","hand":"{digests["hand"]}"}}'
    )
    with pytest.raises(PreflightError) as duplicate:
        validate_model_digest_pins(duplicate_json)
    assert duplicate.value.error_code == "INVALID_MODEL_DIGEST_PINS"


def test_reviewed_all_model_map_can_pin_a_debug_subset() -> None:
    digests = {"tool": "a" * 64, "blood": "b" * 64, "hand": "c" * 64}
    assert validate_model_digest_pins(
        json.dumps(digests), algorithms=("tool",)
    ) == digests


def test_compose_pin_preflight_hashes_local_read_only_model_files(
    tmp_path: Path,
) -> None:
    payloads = {
        "tool": b"reviewed tool model",
        "blood": b"reviewed blood model",
        "hand": b"reviewed hand model",
    }
    filenames = {"tool": "tool.pth", "blood": "blood.pth", "hand": "hand.task"}
    for algorithm, payload in payloads.items():
        (tmp_path / filenames[algorithm]).write_bytes(payload)
    digests = {name: _sha256(payload) for name, payload in payloads.items()}

    outcome = check_compose_model_pins(
        _compose_config(tmp_path, digests),
        consumer_service="taskplanner-runtime",
        worker_service="pnu-perception",
        verify_local_files=True,
    )

    assert outcome["accepted"] is True
    assert outcome["local_files_verified"] is True
    assert set(outcome["verified_files"]) == {"tool", "blood", "hand"}
    assert outcome["verified_files"]["tool"]["sha256"] == digests["tool"]


def test_compose_pin_preflight_rejects_mismatch_and_writable_mount(
    tmp_path: Path,
) -> None:
    for filename in ("tool.pth", "blood.pth", "hand.task"):
        (tmp_path / filename).write_bytes(filename.encode())
    wrong = {"tool": "a" * 64, "blood": "b" * 64, "hand": "c" * 64}

    with pytest.raises(PreflightError) as mismatch:
        check_compose_model_pins(
            _compose_config(tmp_path, wrong),
            consumer_service="taskplanner-runtime",
            worker_service="pnu-perception",
            verify_local_files=True,
        )
    assert mismatch.value.error_code == "MODEL_DIGEST_MISMATCH"
    assert mismatch.value.exit_code == EXIT_MODEL_NOT_READY

    with pytest.raises(PreflightError) as writable:
        check_compose_model_pins(
            _compose_config(tmp_path, wrong, read_only=False),
            consumer_service="taskplanner-runtime",
            worker_service="pnu-perception",
            verify_local_files=True,
        )
    assert writable.value.error_code == "INVALID_MODEL_MOUNT"
    assert writable.value.exit_code == EXIT_INVALID


def test_worker_probe_requires_and_compares_reviewed_model_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digests = {"tool": "a" * 64, "blood": "b" * 64, "hand": "c" * 64}
    models = {
        name: {"ready": True, "digest_sha256": digest}
        for name, digest in digests.items()
    }
    health = {"schema": "pnu.health.v1", "ready": True, "models": models}
    capabilities = {
        "schema": "pnu.capabilities.v1",
        "algorithms": ["tool", "blood", "hand"],
        "models": models,
        "auth": {"mode": "none"},
    }

    def fake_get_json(
        url: str, timeout_sec: float, api_token: str | None = None
    ) -> dict[str, object]:
        del timeout_sec, api_token
        return health if url.endswith("/v1/health") else capabilities

    monkeypatch.setattr(preflight, "_get_json", fake_get_json)
    outcome = preflight.check_worker(
        "http://127.0.0.1:8020",
        "local",
        1.0,
        expected_model_digests_json=json.dumps(digests),
    )
    assert outcome["model_digests"] == digests

    mismatched = dict(digests)
    mismatched["tool"] = "d" * 64
    with pytest.raises(PreflightError) as mismatch:
        preflight.check_worker(
            "http://127.0.0.1:8020",
            "local",
            1.0,
            expected_model_digests_json=json.dumps(mismatched),
        )
    assert mismatch.value.error_code == "WORKER_MODEL_DIGEST_MISMATCH"

    with pytest.raises(PreflightError) as unpinned:
        preflight.check_worker("http://127.0.0.1:8020", "local", 1.0)
    assert unpinned.value.error_code == "MODEL_DIGEST_PIN_MISSING"
