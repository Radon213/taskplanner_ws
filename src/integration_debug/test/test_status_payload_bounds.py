import json
import threading

from std_msgs.msg import String

from integration_debug.node import (
    MAX_EVENT_SUMMARY_ITEMS,
    MAX_EVENT_SUMMARY_STRING_CHARS,
    InputStats,
    IntegrationDebugNode,
    _bounded_event_summary,
)


def test_recent_event_summary_bounds_large_nested_payloads() -> None:
    result = _bounded_event_summary(
        {
            "status_json": "x" * (MAX_EVENT_SUMMARY_STRING_CHARS + 500),
            "rows": list(range(MAX_EVENT_SUMMARY_ITEMS + 4)),
        }
    )

    assert len(result["status_json"]) < MAX_EVENT_SUMMARY_STRING_CHARS + 80
    assert "500 chars omitted" in result["status_json"]
    assert len(result["rows"]) == MAX_EVENT_SUMMARY_ITEMS + 1
    assert "4 items omitted" in result["rows"][-1]
    assert len(json.dumps(result)) < 4096


def test_non_speech_string_input_updates_monitor_only() -> None:
    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    topic = "/integration/cv_contract/status"
    harness._input_stats = {topic: InputStats()}
    harness._asr_topic = "/sensors/surgeon/sentence"
    harness._last_sentence = "surgeon sentence remains authoritative"

    IntegrationDebugNode._on_string_input(
        harness,
        topic,
        String(data='{"schema":"taskplanner.cv_external_contract.v1"}'),
    )

    stats = harness._input_stats[topic]
    assert stats.message_count == 1
    assert stats.last_sample.startswith('{"schema"')
    assert harness._last_sentence == "surgeon sentence remains authoritative"
