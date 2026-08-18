from types import SimpleNamespace

from phase_estimator.estimator import _stamp_to_sec
from phase_estimator.node import PhaseEstimatorNode


class _Stamp:
    sec = 12
    nanosec = 345_000_000


def test_stamp_to_sec_preserves_fractional_seconds():
    assert _stamp_to_sec(_Stamp()) == 12.345


def test_reset_is_repeatable_and_reopens_the_next_lifecycle_edge() -> None:
    node = PhaseEstimatorNode.__new__(PhaseEstimatorNode)
    node._last_lifecycle_control_signature = None
    node._spec_dir = "/test/spec"
    load_calls: list[str] = []
    node._load_spec = load_calls.append

    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="start"))

    assert load_calls == ["/test/spec", "/test/spec"]
    assert node._last_lifecycle_control_signature == ("start", "")
