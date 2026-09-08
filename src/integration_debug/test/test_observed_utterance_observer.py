from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading

from integration_debug.node import InputStats, IntegrationDebugNode


def _observer_harness():
    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    harness._input_stats = {
        "/surgery/audio/observed_utterance": InputStats(),
    }
    harness._last_sentence = ""
    harness._last_voice_parse = {}
    harness.events: list[tuple[str, dict[str, object]]] = []
    harness._record = lambda name, payload: harness.events.append((name, payload))
    return harness


def test_observed_utterance_updates_debug_projection_without_dispatching() -> None:
    harness = _observer_harness()

    IntegrationDebugNode._on_speech_utterance_input(
        harness,
        "/surgery/audio/observed_utterance",
        SimpleNamespace(
            utterance_id="utterance-7",
            text="석션 시작",
            is_final=True,
            source="asr-1.7",
            speaker_role="surgeon",
            has_confidence=True,
            confidence=0.81,
        ),
    )

    stats = harness._input_stats["/surgery/audio/observed_utterance"]
    assert stats.message_count == 1
    assert stats.last_sample.startswith("utterance-7 · asr-1.7 · final")
    assert harness._last_sentence == "석션 시작"
    assert harness._last_voice_parse == {
        "matched": False,
        "ambiguous": False,
        "operation": "",
        "payload": {},
        "reason": "observed_by_command_router",
    }
    assert harness.events == [
        (
            "observed_speech_utterance",
            {
                "topic": "/surgery/audio/observed_utterance",
                "utterance_id": "utterance-7",
                "text": "석션 시작",
                "source": "asr-1.7",
                "speaker_role": "surgeon",
                "is_final": True,
                "has_confidence": True,
                "confidence": 0.81,
            },
        )
    ]
    assert not hasattr(harness, "_dispatch_action")


def test_debug_voice_observer_has_no_legacy_executable_input() -> None:
    root = Path(__file__).resolve().parents[1]
    node_source = (root / "integration_debug" / "node.py").read_text(encoding="utf-8")
    config_source = (root / "config" / "integration_debug.yaml").read_text(
        encoding="utf-8"
    )

    assert "/surgery/audio/request_text" not in node_source
    assert "/surgery/audio/request_text" not in config_source
    assert "parse_voice_command" not in node_source
