from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from simulation_runtime.fault_scenario import (
    FaultScenario,
    transform_image_bytes,
    transform_speech_text,
)


def _scenario(tmp_path: Path, body: str) -> FaultScenario:
    path = tmp_path / "faults.yaml"
    path.write_text(body, encoding="utf-8")
    return FaultScenario.load(path)


def test_scenario_is_validated_and_active_by_source(tmp_path):
    scenario = _scenario(
        tmp_path,
        """schema: taskplanner.fault_scenario.v1
scenario_id: compound
seed: 7
events:
  - id: cam-drop
    source: cam4
    kind: drop
    start_sec: 2
    duration_sec: 3
""",
    )
    assert scenario.active("cam4", 1.9) == ()
    assert scenario.active("cam4", 2.0)[0].event_id == "cam-drop"
    assert scenario.active("flir", 2.0) == ()


def test_scenario_can_apply_speech_faults_to_sentence_relay(tmp_path):
    scenario = _scenario(
        tmp_path,
        """schema: taskplanner.fault_scenario.v1
scenario_id: asr-alias
seed: 9
events:
  - id: typo
    source: speech
    kind: speech_replace
    start_sec: 0
    duration_sec: 3
""",
    )

    events = scenario.active_any(("sentence", "speech"), 1.0)

    assert [event.event_id for event in events] == ["typo"]


def test_invalid_fault_kind_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unsupported fault kind"):
        _scenario(
            tmp_path,
            """schema: taskplanner.fault_scenario.v1
events:
  - source: cam4
    kind: magic
    start_sec: 0
    duration_sec: 1
""",
        )


def test_image_fault_is_deterministic(tmp_path):
    scenario = _scenario(
        tmp_path,
        """schema: taskplanner.fault_scenario.v1
seed: 19
events:
  - id: cover
    source: flir
    kind: occlusion
    start_sec: 0
    duration_sec: 2
    params: {width_ratio: 0.4, height_ratio: 0.4}
""",
    )
    image = Image.new("RGB", (96, 64), (220, 180, 160))
    encoded = BytesIO()
    image.save(encoded, format="JPEG")
    events = scenario.active("flir", 1.0)
    first = transform_image_bytes(
        encoded.getvalue(),
        events=events,
        scenario=scenario,
        source="flir",
        sequence=4,
    )
    second = transform_image_bytes(
        encoded.getvalue(),
        events=events,
        scenario=scenario,
        source="flir",
        sequence=4,
    )
    assert first == second
    assert first != encoded.getvalue()


def test_speech_noise_preserves_explicit_finality_semantics(tmp_path):
    scenario = _scenario(
        tmp_path,
        """schema: taskplanner.fault_scenario.v1
events:
  - source: speech
    kind: speech_replace
    start_sec: 0
    duration_sec: 2
    params:
      replacements: {Allis: alice}
  - source: speech
    kind: speech_partial
    start_sec: 0
    duration_sec: 2
    params: {keep_words: 2}
""",
    )
    text, is_final = transform_speech_text(
        "Allis forceps please now",
        scenario.active("speech", 1.0),
    )
    assert text == "alice forceps"
    assert is_final is False


def test_release_scenarios_exercise_every_declared_media_and_speech_fault():
    root = Path(__file__).resolve().parents[3]
    scenario_dir = root / "config" / "fault_scenarios"
    observed = {
        event.kind
        for path in scenario_dir.glob("*.yaml")
        if path.read_text(encoding="utf-8").startswith(
            "schema: taskplanner.fault_scenario.v1"
        )
        for event in FaultScenario.load(path).events
    }

    assert {
        "drop",
        "freeze",
        "duplicate",
        "delay",
        "reorder",
        "corrupt",
        "blur",
        "exposure",
        "occlusion",
        "shake",
        "resize",
        "speech_replace",
        "speech_partial",
        "vlm_unhealthy",
        "vlm_invalid_schema",
        "vlm_timeout",
        "vlm_http_500",
        "vlm_restart",
    }.issubset(observed)
