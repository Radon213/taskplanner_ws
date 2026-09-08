from pathlib import Path


BRIDGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "surgical_interop_execution"
    / "bridge.py"
)


def _section(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    end_at = source.index(end, start_at)
    return source[start_at:end_at]


def test_voice_tool_request_uses_the_same_busy_lane_as_every_other_request() -> None:
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    ingress = _section(source, "    def _on_skill", "    def _dispatch_tool_transfer")
    admission = _section(
        source, "    def _begin_action_dispatch", "    def _begin_service_dispatch"
    )

    assert "_queue_voice_tool_transfer_preemption" not in ingress
    assert "self._dispatch_tool_transfer(command, transfer_request)" in ingress
    assert 'route == "tool_transfer"' in admission
    assert 'return "tool_transfer_busy"' in admission

