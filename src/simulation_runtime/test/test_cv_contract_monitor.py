import json
from types import SimpleNamespace

import pytest

from simulation_runtime.cv_contract import (
    endpoint_by_key,
    normalize_perception_backend,
    normalize_perception_location,
    normalize_perception_provider,
    resolve_perception_selection,
    validate_perception_endpoint,
)
from simulation_runtime.cv_contract_monitor import (
    CvContractMonitor,
    qos_contract_matches,
    qos_contract_state,
    validate_aligned_compressed_depth,
    validate_camera_info,
    validate_compressed_depth,
    validate_compressed_image,
    validate_depth_image,
    validate_viplab_stream_status,
)


def _header(*, sec: int = 10, nanosec: int = 0, frame_id: str = "cam4"):
    return SimpleNamespace(
        stamp=SimpleNamespace(sec=sec, nanosec=nanosec), frame_id=frame_id
    )


def _stream_status(*, depth: bool = False) -> SimpleNamespace:
    stream_id = "cam_4_depth" if depth else "cam_4"
    source_topic = (
        "/synced/cam_4/depth/image_rect_raw/compressedDepth"
        if depth
        else "/synced/cam_4/color/image_raw/compressed"
    )
    return SimpleNamespace(
        data=json.dumps(
            {
                "schema": "arpa_multicam.stream_status.v1",
                "stream_id": stream_id,
                "source_topic": source_topic,
                "source_stamp": {"sec": 10, "nanosec": 20},
                "frame_id": (
                    "cam_4_depth_optical_frame"
                    if depth
                    else "cam_4_color_optical_frame"
                ),
                "format": (
                    "16UC1; compressedDepth png"
                    if depth
                    else "rgb8; jpeg compressed bgr8"
                ),
                "measured_hz": 14.9,
                "payload_bytes": 330_000 if depth else 166_000,
                "published_count": 100,
                "dropped_count": 0,
                "qos": {
                    "reliability": "best_effort",
                    "durability": "volatile",
                    "depth": 1,
                },
            },
            separators=(",", ":"),
        )
    )


def test_viplab_status_validates_rgb_and_depth_without_receiving_payloads() -> None:
    for depth in (False, True):
        source_topic = (
            "/synced/cam_4/depth/image_rect_raw/compressedDepth"
            if depth
            else "/synced/cam_4/color/image_raw/compressed"
        )
        valid, errors, details = validate_viplab_stream_status(
            _stream_status(depth=depth),
            expected_stream_id="cam_4_depth" if depth else "cam_4",
            expected_source_topic=source_topic,
            compressed_depth=depth,
        )
        assert valid
        assert errors == []
        assert details["source_stamp_sec"] == pytest.approx(10.00000002)
        assert details["evidence_transport"] == "retained_stream_status"


def test_viplab_status_rejects_source_miswire_and_extra_keys() -> None:
    message = _stream_status()
    payload = json.loads(message.data)
    payload["unexpected"] = True
    message.data = json.dumps(payload)

    valid, errors, _details = validate_viplab_stream_status(
        message,
        expected_stream_id="cam_4",
        expected_source_topic="/synced/cam_4/color/image_raw/compressed",
        compressed_depth=False,
    )

    assert not valid
    assert "stream_status_keys_mismatch" in errors


def test_normalize_perception_backend_is_explicit() -> None:
    assert normalize_perception_backend(" External ") == "external"
    assert normalize_perception_backend("LOCAL") == "local"
    with pytest.raises(ValueError, match="PERCEPTION_BACKEND"):
        normalize_perception_backend("automatic")


def test_perception_provider_and_location_are_independent_axes() -> None:
    assert normalize_perception_provider(" PNU_HAND_BLOOD ") == "pnu_hand_blood"
    assert normalize_perception_location("REMOTE") == "remote"
    selection = resolve_perception_selection(
        provider="pnu_hand_blood",
        location="local",
        legacy_backend="external",
    )
    assert selection.provider == "pnu_hand_blood"
    assert selection.location == "local"
    assert selection.legacy_backend == "external"
    assert selection.source == "explicit_axes"


@pytest.mark.parametrize(
    ("backend", "provider", "location"),
    [
        ("local", "builtin_rfdetr", "local"),
        ("external", "pnu_hand_blood", "remote"),
        ("disabled", "disabled", "local"),
    ],
)
def test_legacy_backend_maps_to_explicit_selection(
    backend: str, provider: str, location: str
) -> None:
    selection = resolve_perception_selection(legacy_backend=backend)
    assert selection.provider == provider
    assert selection.location == location
    assert selection.legacy_backend == backend
    assert selection.source == "legacy_backend"


def test_location_without_provider_is_rejected_fail_closed() -> None:
    with pytest.raises(ValueError, match="requires PERCEPTION_PROVIDER"):
        resolve_perception_selection(location="remote", legacy_backend="local")


def test_perception_endpoint_must_match_selected_location() -> None:
    local = resolve_perception_selection(
        provider="builtin_rfdetr", location="local"
    )
    assert validate_perception_endpoint("http://127.0.0.1:8010/", local) == (
        "http://127.0.0.1:8010"
    )
    with pytest.raises(ValueError, match="requires a loopback"):
        validate_perception_endpoint("http://192.168.1.20:8010", local)

    remote = resolve_perception_selection(
        provider="builtin_rfdetr", location="remote"
    )
    assert validate_perception_endpoint("https://192.168.1.20:8443", remote) == (
        "https://192.168.1.20:8443"
    )
    with pytest.raises(ValueError, match="without an API path"):
        validate_perception_endpoint("https://192.168.1.20:8443/api", remote)
    with pytest.raises(ValueError, match="non-loopback"):
        validate_perception_endpoint("http://localhost:8010", remote)
    with pytest.raises(ValueError, match="non-loopback"):
        validate_perception_endpoint("http://localhost.:8010", remote)
    with pytest.raises(ValueError, match="non-loopback"):
        validate_perception_endpoint("http://0.0.0.0:8010", remote)


def test_remote_endpoint_rejects_a_dns_loopback_alias(monkeypatch) -> None:
    remote = resolve_perception_selection(
        provider="pnu_hand_blood", location="remote"
    )
    monkeypatch.setattr(
        "simulation_runtime.cv_contract.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ],
    )
    with pytest.raises(ValueError, match="non-loopback"):
        validate_perception_endpoint("http://loopback-alias.example:8020", remote)


def test_disabled_perception_rejects_a_worker_endpoint() -> None:
    disabled = resolve_perception_selection(provider="disabled")
    assert validate_perception_endpoint("", disabled) == ""
    with pytest.raises(ValueError, match="must be empty"):
        validate_perception_endpoint("http://127.0.0.1:8010", disabled)


def test_monitor_exposes_only_worker_origin() -> None:
    node = CvContractMonitor.__new__(CvContractMonitor)
    node._perception_endpoint = "https://perception.lan:8443/api/v2"
    assert node._perception_worker_origin() == "https://perception.lan:8443"


def test_qos_contract_checker_requires_exact_external_profile() -> None:
    expected = "RELIABLE/TRANSIENT_LOCAL/KEEP_LAST(1)"
    assert qos_contract_matches(
        expected,
        {"reliability": 1, "durability": 1, "depth": 1},
    )
    assert not qos_contract_matches(
        expected,
        {"reliability": 1, "durability": 2, "depth": 10},
    )
    assert qos_contract_state(
        expected,
        {"reliability": 1, "durability": 1, "depth": 0},
    ) == "UNVERIFIABLE_DEPTH"


def test_workbook_qos_profiles_remain_exact() -> None:
    assert endpoint_by_key("cam4_rgb").topic == (
        "/synced/cam_4/color/image_raw/compressed"
    )
    assert endpoint_by_key("cam4_rgb").qos == (
        "BEST_EFFORT/VOLATILE/KEEP_LAST(1)"
    )
    assert endpoint_by_key("cam4_camera_info").topic == (
        "/synced/cam_4/color/camera_info"
    )
    assert endpoint_by_key("cam4_camera_info").qos == (
        "RELIABLE/VOLATILE/KEEP_LAST(20)"
    )
    assert endpoint_by_key("cam4_depth_camera_info").topic == (
        "/synced/cam_4/depth/camera_info"
    )
    assert endpoint_by_key("cam4_native_depth_compressed").topic == (
        "/synced/cam_4/depth/image_rect_raw/compressedDepth"
    )
    assert endpoint_by_key("cam4_native_depth_compressed").qos == (
        "BEST_EFFORT/VOLATILE/KEEP_LAST(1)"
    )
    assert endpoint_by_key("cam4_depth_to_color_extrinsics").topic == (
        "/synced/cam_4/extrinsics/depth_to_color"
    )
    assert endpoint_by_key("cam4_depth_to_color_extrinsics").qos == (
        "RELIABLE/TRANSIENT_LOCAL/KEEP_LAST(1)"
    )
    assert endpoint_by_key("cam4_aligned_depth_compressed").topic == (
        "/synced/cam_4/aligned_depth_to_color/"
        "image_raw/compressedDepth"
    )
    assert endpoint_by_key("cam4_aligned_depth_compressed").qos == (
        "BEST_EFFORT/VOLATILE/KEEP_LAST(1)"
    )
    assert endpoint_by_key("cam4_aligned_depth_camera_info").topic == (
        "/synced/cam_4/aligned_depth_to_color/camera_info"
    )
    assert endpoint_by_key("cam4_rgb_alias").qos == (
        "BEST_EFFORT/VOLATILE/KEEP_LAST(5)"
    )
    assert endpoint_by_key("handover_tray_camera_info").qos == (
        "RELIABLE/VOLATILE/KEEP_LAST(5)"
    )
    assert endpoint_by_key("suction_camera_info").qos == (
        "RELIABLE/TRANSIENT_LOCAL/KEEP_LAST(1)"
    )
    assert endpoint_by_key("bleeding_mask").qos == (
        "BEST_EFFORT/VOLATILE/KEEP_LAST(5)"
    )


def test_compressed_image_requires_payload_stamp_and_frame() -> None:
    valid, errors, details = validate_compressed_image(
        SimpleNamespace(header=_header(), format="jpeg", data=b"123")
    )
    assert valid
    assert errors == []
    assert details["payload_bytes"] == 3

    valid, errors, _ = validate_compressed_image(
        SimpleNamespace(header=_header(sec=0, frame_id=""), format="", data=b"")
    )
    assert not valid
    assert set(errors) == {
        "empty_payload",
        "missing_source_stamp",
        "missing_frame_id",
    }


def test_native_compressed_depth_is_not_claimed_as_color_aligned() -> None:
    valid, errors, details = validate_compressed_depth(
        SimpleNamespace(
            header=_header(frame_id="cam4_depth_optical_frame"),
            format="16UC1; compressedDepth png",
            data=b"png",
        )
    )
    assert valid
    assert errors == []
    assert details["alignment_state"] == "NATIVE_DEPTH_FRAME_NOT_COLOR_ALIGNED"
    assert details["depth_units_policy"] == (
        "UNVERIFIED_NO_LIVE_SCALE_CONTRACT"
    )

    valid, errors, _ = validate_compressed_depth(
        SimpleNamespace(
            header=_header(frame_id="cam4_depth_optical_frame"),
            format="jpeg",
            data=b"jpeg",
        )
    )
    assert not valid
    assert "format_is_not_compressed_depth" in errors


def test_aligned_compressed_depth_requires_cam4_color_frame_and_16uc1() -> None:
    valid, errors, details = validate_aligned_compressed_depth(
        SimpleNamespace(
            header=_header(frame_id="cam_4_color_optical_frame"),
            format="16UC1; compressedDepth png",
            data=b"png",
        )
    )
    assert valid
    assert errors == []
    assert details["alignment_state"] == (
        "RGB_ALIGNED_CAM4_COLOR_OPTICAL_FRAME"
    )

    valid, errors, _ = validate_aligned_compressed_depth(
        SimpleNamespace(
            header=_header(frame_id="cam_4_depth_optical_frame"),
            format="32FC1; compressedDepth png",
            data=b"png",
        )
    )
    assert not valid
    assert set(errors) == {
        "format_is_not_16uc1",
        "frame_is_not_cam4_color_optical_frame",
    }


def test_camera_info_checks_matrix_shape_without_claiming_calibration_validity() -> None:
    message = SimpleNamespace(
        header=_header(),
        width=1280,
        height=720,
        k=[1.0] * 9,
        r=[1.0] * 9,
        p=[1.0] * 12,
    )
    valid, errors, details = validate_camera_info(message)
    assert valid
    assert errors == []
    assert details["calibration_policy"] == "PENDING_EXTERNAL_CALIBRATION_VERSION"

    message.p = [float("nan")] * 12
    valid, errors, _ = validate_camera_info(message)
    assert not valid
    assert "invalid_p_matrix" in errors


def test_depth_image_is_structural_only_until_unit_and_sync_contract_arrives() -> None:
    message = SimpleNamespace(
        header=_header(),
        width=4,
        height=2,
        step=8,
        encoding="16UC1",
        data=b"x" * 16,
    )
    valid, errors, details = validate_depth_image(message)
    assert valid
    assert errors == []
    assert details["depth_units_policy"] == "PENDING_EXTERNAL_CONTRACT"

    message.data = b"x" * 15
    valid, errors, _ = validate_depth_image(message)
    assert not valid
    assert "payload_shorter_than_step_times_height" in errors
