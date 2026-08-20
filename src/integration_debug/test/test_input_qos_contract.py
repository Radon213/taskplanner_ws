from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSReliabilityPolicy,
)

from integration_debug.node import _configured_string_input_qos


def test_cv_status_string_input_uses_transient_local_snapshot_qos() -> None:
    qos = _configured_string_input_qos("reliable_transient_local")

    assert qos.history == QoSHistoryPolicy.KEEP_LAST
    assert qos.depth == 1
    assert qos.reliability == QoSReliabilityPolicy.RELIABLE
    assert qos.durability == QoSDurabilityPolicy.TRANSIENT_LOCAL


def test_live_text_string_inputs_remain_volatile() -> None:
    for configured_name in ("reliable_volatile", None):
        qos = _configured_string_input_qos(configured_name)

        assert qos.history == QoSHistoryPolicy.KEEP_LAST
        assert qos.depth == 20
        assert qos.reliability == QoSReliabilityPolicy.RELIABLE
        assert qos.durability == QoSDurabilityPolicy.VOLATILE
