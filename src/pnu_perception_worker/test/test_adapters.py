from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from pnu_perception_worker.adapters import BloodAdapter, HandAdapter, ToolAdapter
from pnu_perception_worker.depth import DepthContext
from pnu_perception_worker.support_plane import RuntimePlaneValidation


def _metric_depth(value: float = 0.8) -> DepthContext:
    depth = np.full((24, 32), value, dtype=np.float32)
    ready = value > 0.0
    return DepthContext(
        received=True,
        decoded=True,
        input_ready=ready,
        reasons=() if ready else ("depth_has_no_valid_samples",),
        raw_shape=(24, 32),
        depth_m=depth,
        depth_scale_m_per_unit=0.001,
        valid_pixels=24 * 32 if value > 0.0 else 0,
        valid_ratio=1.0 if value > 0.0 else 0.0,
        alignment_id="cam4-align-v1",
    )


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        source={
            "rgb": {
                "stamp_ns": 1_000_000,
                "frame_id": "cam_4_color_optical_frame",
                "format": "jpeg",
            }
        },
        metadata={
            "color_camera_info": {
                "width": 32,
                "height": 24,
                "frame_id": "cam_4_color_optical_frame",
                "k": [100.0, 0.0, 16.0, 0.0, 100.0, 12.0, 0.0, 0.0, 1.0],
                "d": [0.0] * 5,
            }
        },
    )


def _palm_frame_v2(j0, j2, j9, j17):
    origin = 0.5 * (np.asarray(j0) + np.asarray(j9))
    x_axis = np.asarray(j9) - np.asarray(j0)
    x_axis /= np.linalg.norm(x_axis) + 1.0e-9
    y_axis = 0.5 * (np.asarray(j0) + np.asarray(j17)) - np.asarray(j2)
    y_axis -= np.dot(y_axis, x_axis) * x_axis
    y_axis /= np.linalg.norm(y_axis) + 1.0e-9
    z_axis = np.cross(x_axis, y_axis)
    z_axis /= np.linalg.norm(z_axis) + 1.0e-9
    return origin, np.column_stack((x_axis, y_axis, z_axis))


def _rot_to_quat_wxyz(rotation):
    matrix = np.asarray(rotation, dtype=np.float64)
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = np.sqrt(trace + 1.0) * 2.0
        quat = np.array(
            [
                0.25 * scale,
                (matrix[2, 1] - matrix[1, 2]) / scale,
                (matrix[0, 2] - matrix[2, 0]) / scale,
                (matrix[1, 0] - matrix[0, 1]) / scale,
            ]
        )
    else:
        diagonal = int(np.argmax(np.diag(matrix)))
        if diagonal == 0:
            scale = np.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
            quat = np.array(
                [
                    (matrix[2, 1] - matrix[1, 2]) / scale,
                    0.25 * scale,
                    (matrix[0, 1] + matrix[1, 0]) / scale,
                    (matrix[0, 2] + matrix[2, 0]) / scale,
                ]
            )
        elif diagonal == 1:
            scale = np.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
            quat = np.array(
                [
                    (matrix[0, 2] - matrix[2, 0]) / scale,
                    (matrix[0, 1] + matrix[1, 0]) / scale,
                    0.25 * scale,
                    (matrix[1, 2] + matrix[2, 1]) / scale,
                ]
            )
        else:
            scale = np.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
            quat = np.array(
                [
                    (matrix[1, 0] - matrix[0, 1]) / scale,
                    (matrix[0, 2] + matrix[2, 0]) / scale,
                    (matrix[1, 2] + matrix[2, 1]) / scale,
                    0.25 * scale,
                ]
            )
    return quat / np.linalg.norm(quat)


class _FakeHandCore:
    @staticmethod
    def process_frame(*args, **kwargs):
        hand = {
            "hand_index": 0,
            "handedness": {"label": "Right", "score": 0.8754321},
            "joints_2d": [[10.0, 20.0]] * 21,
            "kp_scores": [1.0] * 21,
        }
        return [hand], args[0], 0


def test_hand_public_shape_normalizes_handedness_to_label_and_score() -> None:
    adapter = HandAdapter.__new__(HandAdapter)
    adapter._core = _FakeHandCore()
    adapter._detector = object()
    adapter._mp = object()
    adapter._last_media_timestamp_ms = -1
    request = SimpleNamespace(
        source={"rgb": {"stamp_ns": 1_000_000, "frame_id": "cam4", "format": "jpeg"}},
        metadata={},
    )
    result = adapter.infer(np.zeros((24, 32, 3), dtype=np.uint8), request)
    assert result["hands"][0]["handedness"] == {
        "label": "Right",
        "score": 0.875432,
    }
    assert len(result["hands"][0]["joints_2d"]) == 21
    assert len(result["hands"][0]["kp_scores"]) == 21


def test_hand_public_shape_uses_explicit_unknown_instead_of_null() -> None:
    class CoreWithoutHandedness(_FakeHandCore):
        @staticmethod
        def process_frame(*args, **kwargs):
            hands, frame, valid = _FakeHandCore.process_frame(*args, **kwargs)
            hands[0]["handedness"] = None
            return hands, frame, valid

    adapter = HandAdapter.__new__(HandAdapter)
    adapter._core = CoreWithoutHandedness()
    adapter._detector = object()
    adapter._mp = object()
    adapter._last_media_timestamp_ms = -1
    request = SimpleNamespace(
        source={"rgb": {"stamp_ns": 1_000_000, "frame_id": "cam4", "format": "jpeg"}},
        metadata={},
    )
    result = adapter.infer(np.zeros((24, 32, 3), dtype=np.uint8), request)
    assert result["hands"][0]["handedness"] == {
        "label": "Unknown",
        "score": 0.0,
    }


def test_hand_rgbd_returns_typed_metric_joints_and_xyzw_palm_pose() -> None:
    class MetricHandCore:
        palm_frame_v2 = staticmethod(lambda *_args: ([0.1, 0.2, 0.8], np.eye(3)))
        rot_to_quat_wxyz = staticmethod(lambda _rotation: [1.0, 0.0, 0.0, 0.0])

        @staticmethod
        def process_frame(*args, **kwargs):
            joints = [[0.01 * index, 0.02, 0.8] for index in range(21)]
            hand = {
                "hand_index": 0,
                "handedness": {"label": "Left", "score": 0.9},
                "joints_2d": [[10.0, 12.0]] * 21,
                "joints_3d": joints,
                "kp_scores": [1.0] * 21,
                "kp_valid_depth": [True] * 21,
                "palm_6d": {
                    "translation": [0.1, 0.2, 0.8],
                    "rotation_matrix": [
                        [1.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0],
                        [0.0, 0.0, 1.0],
                    ],
                    "rotation_quat_wxyz": [1.0, 0.0, 0.0, 0.0],
                },
            }
            return [hand], args[0], 21

    adapter = HandAdapter.__new__(HandAdapter)
    adapter._core = MetricHandCore()
    adapter._detector = object()
    adapter._mp = object()
    adapter._last_media_timestamp_ms = -1
    result = adapter.infer(
        np.zeros((24, 32, 3), dtype=np.uint8), _request(), _metric_depth()
    )
    assert result["schema"] == "pnu.hand.rgbd.v1"
    assert result["metric_3d"] == {
        "ready": True,
        "status": "ready",
        "reasons": [],
    }
    hand = result["hands"][0]
    assert len(hand["joints_3d"]) == 21
    assert hand["kp_valid_depth"] == [True] * 21
    assert hand["palm_6d"] == {
        "translation": [0.1, 0.2, 0.8],
        "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
        "rotation_matrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
    }


def test_hand_rgbd_undistorts_joints_and_recomputes_palm_from_corrected_points() -> (
    None
):
    uv = [[10.0 + (index % 7) * 2.0, 7.0 + (index // 7) * 5.0] for index in range(21)]
    uv[0] = [8.0, 18.0]
    uv[2] = [11.0, 7.0]
    uv[9] = [22.0, 18.0]
    uv[17] = [25.0, 9.0]
    z_m = 0.8

    class DistortedCore:
        palm_frame_v2 = staticmethod(_palm_frame_v2)
        rot_to_quat_wxyz = staticmethod(_rot_to_quat_wxyz)

        @staticmethod
        def process_frame(*args, **kwargs):
            hand = {
                "hand_index": 0,
                "handedness": {"label": "Left", "score": 0.9},
                "joints_2d": uv,
                # Upstream pinhole X/Y is deliberately stale and must be replaced.
                "joints_3d": [[99.0, 99.0, z_m] for _ in range(21)],
                "kp_scores": [1.0] * 21,
                "kp_valid_depth": [True] * 21,
                "palm_6d": {
                    "translation": [9.0, 9.0, 9.0],
                    "rotation_matrix": np.eye(3).tolist(),
                    "rotation_quat_wxyz": [1.0, 0.0, 0.0, 0.0],
                },
            }
            return [hand], args[0], 21

    request = _request()
    request.metadata["color_camera_info"]["d"] = [
        -0.05712,
        0.07287,
        -0.0000908,
        0.000346,
        -0.02320,
    ]
    adapter = HandAdapter.__new__(HandAdapter)
    adapter._core = DistortedCore()
    adapter._detector = object()
    adapter._mp = object()
    adapter._last_media_timestamp_ms = -1
    result = adapter.infer(
        np.zeros((24, 32, 3), dtype=np.uint8), request, _metric_depth()
    )
    hand = result["hands"][0]
    matrix = np.asarray(request.metadata["color_camera_info"]["k"]).reshape(3, 3)
    distortion = np.asarray(request.metadata["color_camera_info"]["d"])
    rays = cv2.undistortPoints(
        np.asarray(uv, dtype=np.float64).reshape(-1, 1, 2), matrix, distortion
    ).reshape(-1, 2)
    expected = np.column_stack((rays[:, 0] * z_m, rays[:, 1] * z_m, [z_m] * 21))
    assert np.asarray(hand["joints_3d"]) == pytest.approx(expected, abs=1.0e-6)
    expected_origin, _ = _palm_frame_v2(
        expected[0], expected[2], expected[9], expected[17]
    )
    assert hand["palm_6d"] is not None
    assert hand["palm_6d"]["translation"] == pytest.approx(expected_origin, abs=1.0e-6)
    assert hand["palm_6d"]["translation"] != [9.0, 9.0, 9.0]


def test_hand_rgbd_rejects_border_clipped_depth_for_out_of_frame_keypoints() -> None:
    class BorderClippingCore:
        @staticmethod
        def process_frame(*args, **kwargs):
            joints_2d = [[10.0, 12.0] for _ in range(21)]
            joints_2d[2] = [-50.0, 10.0]
            joints_2d[5] = [100.0, 10.0]
            hand = {
                "hand_index": 0,
                "handedness": {"label": "Left", "score": 0.9},
                "joints_2d": joints_2d,
                "joints_3d": [[0.1, 0.1, 0.8] for _ in range(21)],
                "kp_scores": [1.0] * 21,
                # This reproduces the upstream border-clipping false positive.
                "kp_valid_depth": [True] * 21,
                "palm_6d": {
                    "translation": [0.1, 0.2, 0.8],
                    "rotation_matrix": [
                        [1.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0],
                        [0.0, 0.0, 1.0],
                    ],
                    "rotation_quat_wxyz": [1.0, 0.0, 0.0, 0.0],
                },
            }
            return [hand], args[0], 21

    adapter = HandAdapter.__new__(HandAdapter)
    adapter._core = BorderClippingCore()
    adapter._detector = object()
    adapter._mp = object()
    adapter._last_media_timestamp_ms = -1
    result = adapter.infer(
        np.zeros((24, 32, 3), dtype=np.uint8), _request(), _metric_depth()
    )
    hand = result["hands"][0]
    assert hand["kp_valid_depth"][2] is False
    assert hand["kp_valid_depth"][5] is False
    assert hand["joints_3d"][2] == [0.0, 0.0, 0.0]
    assert hand["joints_3d"][5] == [0.0, 0.0, 0.0]
    assert hand["kp_valid_depth"][0] is True
    assert hand["palm_6d"] is None


def test_hand_zero_depth_falls_back_to_2d_without_fabricating_3d() -> None:
    class ZeroDepthCore:
        @staticmethod
        def process_frame(*args, **kwargs):
            hand = {
                "hand_index": 0,
                "handedness": None,
                "joints_2d": [[10.0, 12.0]] * 21,
                "joints_3d": [[0.0, 0.0, 0.0]] * 21,
                "kp_scores": [1.0] * 21,
                "kp_valid_depth": [False] * 21,
                "palm_6d": None,
            }
            return [hand], args[0], 0

    adapter = HandAdapter.__new__(HandAdapter)
    adapter._core = ZeroDepthCore()
    adapter._detector = object()
    adapter._mp = object()
    adapter._last_media_timestamp_ms = -1
    result = adapter.infer(
        np.zeros((24, 32, 3), dtype=np.uint8), _request(), _metric_depth(0.0)
    )
    assert result["schema"] == "pnu.hand.2d.v1"
    assert "joints_3d" not in result["hands"][0]
    assert "kp_valid_depth" not in result["hands"][0]
    assert "palm_6d" not in result["hands"][0]
    assert "metric_3d" not in result


def test_blood_rgbd_samples_instance_and_combined_centroid_depth() -> None:
    mask = np.zeros((24, 32), dtype=bool)
    mask[8:12, 14:18] = True
    detections = SimpleNamespace(
        xyxy=np.asarray([[14.0, 8.0, 18.0, 12.0]]),
        class_id=np.asarray([0]),
        confidence=np.asarray([0.88]),
        mask=np.asarray([mask]),
    )

    class Model:
        @staticmethod
        def predict(*args, **kwargs):
            return detections

    class Cuda:
        @staticmethod
        def is_available():
            return False

    adapter = BloodAdapter.__new__(BloodAdapter)
    adapter._torch = SimpleNamespace(cuda=Cuda())
    adapter._model = Model()
    adapter._threshold = 0.5
    adapter._max_detections = 10
    adapter._max_total_rle_counts = 10_000
    adapter._upstream = SimpleNamespace(
        centroid=lambda value: (
            [
                float(np.where(value)[1].mean()),
                float(np.where(value)[0].mean()),
            ]
            if np.any(value)
            else None
        )
    )
    result = adapter.infer(
        np.zeros((24, 32, 3), dtype=np.uint8), _request(), _metric_depth(0.75)
    )
    assert result["schema"] == "pnu.blood.rgbd.v1"
    assert result["detections"][0]["centroid_depth_m"] == pytest.approx(0.75)
    assert result["combined_blood_centroid_depth_m"] == pytest.approx(0.75)
    assert result["metric_3d"]["ready"] is True


def test_blood_rgbd_combined_centroid_uses_union_mask_median_depth() -> None:
    masks = np.zeros((2, 24, 32), dtype=bool)
    masks[0, 8:12, 4:8] = True
    masks[1, 8:12, 24:28] = True
    detections = SimpleNamespace(
        xyxy=np.asarray([[4.0, 8.0, 8.0, 12.0], [24.0, 8.0, 28.0, 12.0]]),
        class_id=np.asarray([0, 0]),
        confidence=np.asarray([0.9, 0.85]),
        mask=masks,
    )

    class Cuda:
        @staticmethod
        def is_available():
            return False

    adapter = BloodAdapter.__new__(BloodAdapter)
    adapter._torch = SimpleNamespace(cuda=Cuda())
    adapter._model = SimpleNamespace(predict=lambda *_args, **_kwargs: detections)
    adapter._threshold = 0.5
    adapter._max_detections = 10
    adapter._max_total_rle_counts = 10_000
    adapter._upstream = SimpleNamespace(
        centroid=lambda value: (
            [float(np.where(value)[1].mean()), float(np.where(value)[0].mean())]
            if np.any(value)
            else None
        )
    )
    depth_m = np.full((24, 32), 2.0, dtype=np.float32)
    union = np.any(masks, axis=0)
    depth_m[union] = 0.8
    depth = DepthContext(
        received=True,
        decoded=True,
        input_ready=True,
        reasons=(),
        raw_shape=(24, 32),
        depth_m=depth_m,
        depth_scale_m_per_unit=0.001,
        valid_pixels=24 * 32,
        valid_ratio=1.0,
        alignment_id="cam4-align-v1",
    )

    result = adapter.infer(np.zeros((24, 32, 3), dtype=np.uint8), _request(), depth)
    combined = result["combined_blood_centroid_xy_px"]
    assert combined == pytest.approx([15.5, 9.5])
    assert not union[round(combined[1]), round(combined[0])]
    assert result["combined_blood_centroid_depth_m"] == pytest.approx(0.8)
    for index, row in enumerate(result["detections"]):
        point = row["centroid_xy_px"]
        assert masks[index, int(point[1]), int(point[0])]
        assert row["centroid_depth_m"] == pytest.approx(0.8)


def test_blood_rgbd_instance_centroid_uses_concave_mask_median_depth() -> None:
    mask = np.zeros((24, 32), dtype=bool)
    mask[6:18, 10:22] = True
    mask[9:15, 13:19] = False
    detections = SimpleNamespace(
        xyxy=np.asarray([[10.0, 6.0, 22.0, 18.0]]),
        class_id=np.asarray([0]),
        confidence=np.asarray([0.9]),
        mask=np.asarray([mask]),
    )

    class Cuda:
        @staticmethod
        def is_available():
            return False

    adapter = BloodAdapter.__new__(BloodAdapter)
    adapter._torch = SimpleNamespace(cuda=Cuda())
    adapter._model = SimpleNamespace(predict=lambda *_args, **_kwargs: detections)
    adapter._threshold = 0.5
    adapter._max_detections = 10
    adapter._max_total_rle_counts = 10_000
    adapter._upstream = SimpleNamespace(
        centroid=lambda value: (
            [float(np.where(value)[1].mean()), float(np.where(value)[0].mean())]
            if np.any(value)
            else None
        )
    )
    depth_m = np.full((24, 32), 2.0, dtype=np.float32)
    depth_m[mask] = 0.65
    depth = DepthContext(
        received=True,
        decoded=True,
        input_ready=True,
        reasons=(),
        raw_shape=(24, 32),
        depth_m=depth_m,
        depth_scale_m_per_unit=0.001,
        valid_pixels=24 * 32,
        valid_ratio=1.0,
        alignment_id="cam4-align-v1",
    )

    result = adapter.infer(np.zeros((24, 32, 3), dtype=np.uint8), _request(), depth)
    point = result["detections"][0]["centroid_xy_px"]
    assert point == pytest.approx([15.5, 11.5])
    assert not mask[round(point[1]), round(point[0])]
    assert result["detections"][0]["centroid_depth_m"] == pytest.approx(0.65)


def test_tool_rgbd_serializes_all_pose_evidence_and_degrades_provisional_plane() -> (
    None
):
    mask = np.zeros((24, 32), dtype=bool)
    mask[8:16, 6:26] = True
    instance = SimpleNamespace(
        frame_local_instance_id=2,
        canonical_class_id=4,
        model_class_index=3,
        class_name="Adson Forceps",
        class_confidence=0.92,
        bbox_xyxy_px=(6.0, 8.0, 26.0, 16.0),
        mask=mask,
        observation_point_uv_px=(16.0, 12.0),
        observation_point_selection_mode="central_longitudinal_band_max_clearance",
        observation_point_boundary_clearance_px=4.0,
        observation_point_depth_m=0.8,
        position_m=(0.0, 0.0, 0.8),
        orientation_xyzw=(0.0, 0.0, 0.0, 1.0),
        pose_mode="PLANAR_4DOF_WITH_NORMAL_PRIOR",
        position_valid=True,
        orientation_valid=True,
        validity="VALID",
        symmetry_type="NONE",
        endpoint_sign_confidence=0.9,
        valid_depth_ratio=1.0,
        pose_point_count=160,
        axis_anisotropy=8.0,
        status_flags=("POSITION_IS_MASK_INTERNAL_OBSERVED_SURFACE_POINT",),
        invalid_reason="",
    )
    frame_result = SimpleNamespace(
        instances=[instance],
        ontology_version="pnu-tool-ontology-v1",
        calibration_version="cam4-align-v1",
        pose_convention_version="pnu.cam4.planar_tool_pose_convention.v2",
    )

    class Algorithm:
        @staticmethod
        def detect_and_estimate(**kwargs):
            return frame_result

    class Camera:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    adapter = ToolAdapter.__new__(ToolAdapter)
    adapter._algorithm = Algorithm()
    adapter._camera_type = Camera
    adapter._support_plane = SimpleNamespace(
        inlier_ratio=0.74,
        residual_p95_m=0.005,
        config_version="reference-cam4-plane-provisional",
    )
    adapter._support_plane_validated = False
    adapter._max_detections = 10
    adapter._max_total_rle_counts = 10_000
    result = adapter.infer(
        np.zeros((24, 32, 3), dtype=np.uint8), _request(), _metric_depth()
    )
    assert result["schema"] == "pnu.tool.rgbd.v1"
    assert result["ontology_version"] == "pnu-tool-ontology-v1"
    assert result["calibration_version"] == "cam4-align-v1"
    assert result["pose_convention_version"].endswith(".v2")
    assert result["support_plane_config_version"] == (
        "reference-cam4-plane-provisional"
    )
    assert result["support_plane_validated"] is False
    detection = result["detections"][0]
    assert set(detection) == {
        "instance_id",
        "canonical_class_id",
        "model_class_index",
        "class_name",
        "confidence",
        "bbox_xyxy_px",
        "mask_rle",
        "observation",
        "pose",
    }
    assert set(detection["observation"]) == {
        "mask_bbox_xyxy_px",
        "mask_area_px",
        "observation_point_uv_px",
        "observation_point_valid",
        "observation_point_inside_mask",
        "observation_point_depth_valid",
        "observation_point_depth_m",
        "observation_point_selection_mode",
        "observation_point_boundary_clearance_px",
    }
    assert set(detection["pose"]) == {
        "position_m",
        "orientation_xyzw",
        "pose_mode",
        "position_valid",
        "orientation_valid",
        "dof_observed",
        "observation_point_definition",
        "axis_definition",
        "symmetry_type",
        "endpoint_sign_confidence",
        "valid_depth_ratio",
        "pose_point_count",
        "axis_anisotropy",
        "support_plane_inlier_ratio",
        "support_plane_residual_p95_m",
        "pose_confidence",
        "pose_confidence_calibrated",
        "validity",
        "status_flags",
        "invalid_reason",
    }
    assert detection["observation"]["observation_point_depth_valid"] is True
    assert detection["pose"]["position_valid"] is True
    assert detection["pose"]["orientation_valid"] is False
    assert detection["pose"]["orientation_xyzw"] is None
    assert detection["pose"]["dof_observed"] == [
        True,
        True,
        True,
        False,
        False,
        False,
    ]
    assert detection["pose"]["validity"] == "DEGRADED"
    assert "SUPPORT_PLANE_UNVALIDATED" in detection["pose"]["status_flags"]
    assert result["metric_3d"] == {
        "ready": True,
        "status": "ready",
        "reasons": [],
    }


@pytest.mark.parametrize(
    ("point_depth", "position", "orientation", "position_valid", "orientation_valid"),
    [
        (None, (0.0, 0.0, 0.8), (0.0, 0.0, 0.0, 1.0), False, False),
        (0.8, (np.nan, 0.0, 0.8), (0.0, 0.0, 0.0, 1.0), False, False),
        (0.8, (0.0, 0.0, 0.8), (0.0, 0.0, 0.0, 0.0), True, False),
    ],
)
def test_tool_rgbd_derives_validity_from_finite_coherent_evidence(
    point_depth,
    position,
    orientation,
    position_valid,
    orientation_valid,
) -> None:
    mask = np.zeros((24, 32), dtype=bool)
    mask[8:16, 6:26] = True
    instance = SimpleNamespace(
        frame_local_instance_id=2,
        canonical_class_id=4,
        model_class_index=3,
        class_name="Adson Forceps",
        class_confidence=0.92,
        bbox_xyxy_px=(6.0, 8.0, 26.0, 16.0),
        mask=mask,
        observation_point_uv_px=(16.0, 12.0),
        observation_point_selection_mode="mask_internal",
        observation_point_boundary_clearance_px=4.0,
        observation_point_depth_m=point_depth,
        position_m=position,
        orientation_xyzw=orientation,
        pose_mode="PLANAR_4DOF_WITH_NORMAL_PRIOR",
        position_valid=True,
        orientation_valid=True,
        validity="VALID",
        symmetry_type="NONE",
        endpoint_sign_confidence=0.9,
        valid_depth_ratio=1.0,
        pose_point_count=160,
        axis_anisotropy=8.0,
        status_flags=("POSITION_IS_MASK_INTERNAL_OBSERVED_SURFACE_POINT",),
        invalid_reason="",
    )
    adapter = ToolAdapter.__new__(ToolAdapter)
    adapter._support_plane = SimpleNamespace(
        inlier_ratio=0.74,
        residual_p95_m=0.005,
        config_version="validated-cam4-plane-v1",
    )
    adapter._support_plane_validated = True
    adapter._max_total_rle_counts = 10_000
    detection, _ = adapter._encode_rgbd_instance(instance, 0)
    observation = detection["observation"]
    pose = detection["pose"]
    assert observation["observation_point_depth_valid"] is position_valid
    assert (observation["observation_point_depth_m"] is not None) is position_valid
    assert pose["position_valid"] is position_valid
    assert (pose["position_m"] is not None) is position_valid
    assert pose["orientation_valid"] is orientation_valid
    assert (pose["orientation_xyzw"] is not None) is orientation_valid
    if not position_valid:
        assert pose["pose_mode"] == "INVALID"
        assert pose["validity"] == "INVALID"
        assert pose["invalid_reason"]
    else:
        assert pose["pose_mode"] == "POSITION_3D_ONLY"
        assert pose["validity"] == "DEGRADED"


def test_tool_2d_contract_does_not_claim_rgbd_fields() -> None:
    mask = np.zeros((24, 32), dtype=bool)
    mask[8:16, 6:26] = True
    instance = SimpleNamespace(
        frame_local_instance_id=0,
        canonical_class_id=4,
        model_class_index=3,
        class_name="Adson Forceps",
        class_confidence=0.8,
        bbox_xyxy_px=(6.0, 8.0, 26.0, 16.0),
        mask=mask,
    )
    adapter = ToolAdapter.__new__(ToolAdapter)
    adapter._detector = SimpleNamespace(
        predict=lambda *_args, **_kwargs: SimpleNamespace(
            instances=[instance], image_width=32, image_height=24
        )
    )
    adapter._max_detections = 10
    adapter._max_total_rle_counts = 10_000
    result = adapter.infer(np.zeros((24, 32, 3), dtype=np.uint8), _request())
    assert result["schema"] == "pnu.tool.2d.v1"
    assert "model_class_index" not in result["detections"][0]
    assert "observation" not in result["detections"][0]
    assert "pose" not in result["detections"][0]
    assert "metric_3d" not in result


def test_rgbd_zero_detections_is_executed_success_for_all_algorithms() -> None:
    empty_tool_result = SimpleNamespace(
        instances=[],
        ontology_version="pnu-tool-ontology-v1",
        calibration_version="cam4-align-v1",
        pose_convention_version="pnu.cam4.planar_tool_pose_convention.v2",
    )
    tool = ToolAdapter.__new__(ToolAdapter)
    tool._algorithm = SimpleNamespace(
        detect_and_estimate=lambda **_kwargs: empty_tool_result
    )
    tool._camera_type = type(
        "Camera",
        (),
        {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
    )
    tool._support_plane = SimpleNamespace(
        inlier_ratio=0.74,
        residual_p95_m=0.005,
        config_version="validated-cam4-plane-v1",
    )
    tool._support_plane_validated = True
    tool._max_detections = 10
    tool._max_total_rle_counts = 10_000

    empty_detections = SimpleNamespace(
        xyxy=np.empty((0, 4)),
        class_id=np.empty((0,), dtype=np.int64),
        confidence=np.empty((0,)),
        mask=np.empty((0, 24, 32), dtype=bool),
    )

    class Cuda:
        @staticmethod
        def is_available():
            return False

    blood = BloodAdapter.__new__(BloodAdapter)
    blood._torch = SimpleNamespace(cuda=Cuda())
    blood._model = SimpleNamespace(predict=lambda *_args, **_kwargs: empty_detections)
    blood._threshold = 0.5
    blood._max_detections = 10
    blood._max_total_rle_counts = 10_000
    blood._upstream = SimpleNamespace(centroid=lambda _mask: None)

    hand = HandAdapter.__new__(HandAdapter)
    hand._core = SimpleNamespace(
        process_frame=lambda *_args, **_kwargs: ([], _args[0], 0)
    )
    hand._detector = object()
    hand._mp = object()
    hand._last_media_timestamp_ms = -1

    frame = np.zeros((24, 32, 3), dtype=np.uint8)
    for result in (
        tool.infer(frame, _request(), _metric_depth()),
        blood.infer(frame, _request(), _metric_depth()),
        hand.infer(frame, _request(), _metric_depth()),
    ):
        assert result["schema"].endswith(".rgbd.v1")
        assert result["metric_3d"] == {
            "ready": True,
            "status": "ready",
            "reasons": [],
        }


def test_tool_zero_detection_serializes_latest_support_plane_drift_metrics() -> None:
    empty_tool_result = SimpleNamespace(
        instances=[],
        ontology_version="pnu-tool-ontology-v1",
        calibration_version="cam4-align-v1",
        pose_convention_version="pnu.cam4.planar_tool_pose_convention.v2",
    )
    adapter = ToolAdapter.__new__(ToolAdapter)
    adapter._algorithm = SimpleNamespace(
        detect_and_estimate=lambda **_kwargs: empty_tool_result
    )
    adapter._camera_type = type(
        "Camera",
        (),
        {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
    )
    adapter._support_plane = SimpleNamespace(
        inlier_ratio=0.95,
        residual_p95_m=0.004,
        config_version="validated-cam4-plane-v1",
    )
    adapter._support_plane_validation_requested = True
    adapter._support_plane_validated = True
    adapter._support_plane_static_reasons = ()
    adapter._support_plane_calibration = SimpleNamespace(
        inlier_ratio=0.95,
        residual_p95_m=0.004,
        validate_frame=lambda **_kwargs: RuntimePlaneValidation(
            valid=False,
            reasons=("support_plane_runtime_inlier_ratio_low",),
            evaluated=True,
            metrics_available=True,
            sample_count=12_345,
            inlier_ratio=0.71,
            residual_median_m=0.0032,
            residual_p95_m=0.021,
            camera_info_sha256="a" * 64,
        ),
    )
    adapter._max_detections = 10
    adapter._max_total_rle_counts = 10_000

    result = adapter.infer(
        np.zeros((24, 32, 3), dtype=np.uint8), _request(), _metric_depth()
    )

    assert result["detections"] == []
    assert result["support_plane_validated"] is False
    assert result["support_plane_diagnostics"] == {
        "schema": "pnu.tool.support_plane_diagnostics.v1",
        "validation_requested": True,
        "artifact_loaded": True,
        "static_reasons": [],
        "calibration_fit": {
            "available": True,
            "inlier_ratio": 0.95,
            "residual_p95_m": 0.004,
        },
        "runtime_validation": {
            "evaluated": True,
            "metrics_available": True,
            "valid": False,
            "reasons": ["support_plane_runtime_inlier_ratio_low"],
            "sample_count": 12_345,
            "inlier_ratio": 0.71,
            "residual_median_m": 0.0032,
            "residual_p95_m": 0.021,
            "camera_info_sha256": "a" * 64,
        },
    }


def test_tool_support_plane_static_failure_reasons_are_bounded() -> None:
    adapter = ToolAdapter.__new__(ToolAdapter)
    adapter._support_plane_validation_requested = True
    adapter._support_plane_calibration = None
    adapter._support_plane_static_reasons = (
        "support_plane_artifact_invalid",
        " malformed\n" + ("x" * 300),
    )
    adapter._last_support_plane_validation = RuntimePlaneValidation(
        valid=False,
        reasons=("support_plane_artifact_unavailable",),
    )

    diagnostics = adapter._support_plane_diagnostics()

    assert diagnostics["artifact_loaded"] is False
    assert diagnostics["calibration_fit"] == {
        "available": False,
        "inlier_ratio": None,
        "residual_p95_m": None,
    }
    assert diagnostics["static_reasons"][0] == "support_plane_artifact_invalid"
    assert "\n" not in diagnostics["static_reasons"][1]
    assert len(diagnostics["static_reasons"][1]) == 160
    runtime = diagnostics["runtime_validation"]
    assert runtime["evaluated"] is False
    assert runtime["metrics_available"] is False
    assert runtime["valid"] is False
    assert runtime["reasons"] == ["support_plane_artifact_unavailable"]
    assert runtime["inlier_ratio"] is None
