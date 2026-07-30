from phase_estimator.estimator import _stamp_to_sec


class _Stamp:
    sec = 12
    nanosec = 345_000_000


def test_stamp_to_sec_preserves_fractional_seconds():
    assert _stamp_to_sec(_Stamp()) == 12.345
