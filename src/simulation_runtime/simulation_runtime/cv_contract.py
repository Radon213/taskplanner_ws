"""Stable, package-independent contract for the future external CV backend.

The Computer Vision team supplied names and QoS profiles, but their custom ROS
message packages, calibration, ontology and timing policy are intentionally not
available yet.  This module records the agreed surface without recreating their
IDLs locally.  It is used by the monitor and the launch layer so that a later
package hand-off is a controlled cut-over rather than an ad-hoc remap.
"""

from __future__ import annotations

from dataclasses import dataclass


CV_CONTRACT_SCHEMA = "taskplanner.cv_external_contract.v1"
CV_CONTRACT_VERSION = "pnu-cv-interface-pending-v1"
PERCEPTION_BACKENDS = frozenset({"local", "external", "disabled"})


@dataclass(frozen=True)
class CvEndpoint:
    """One externally owned CV input or output endpoint.

    ``message_type`` remains a string for custom types.  Importing or locally
    copying an unavailable IDL would create a false DDS compatibility claim.
    """

    key: str
    topic: str
    message_type: str
    qos: str
    owner: str
    direction: str
    required_now: bool = False
    pending_reason: str = ""


def normalize_perception_backend(value: object) -> str:
    """Return a supported backend name or raise a clear configuration error."""

    backend = str(value).strip().casefold()
    if backend not in PERCEPTION_BACKENDS:
        choices = ", ".join(sorted(PERCEPTION_BACKENDS))
        raise ValueError(
            f"PERCEPTION_BACKEND must be one of {choices}; received {value!r}"
        )
    return backend


# CAM4 has one canonical physical RGB source.  The public /surgery alias is
# deliberately listed as an alias, not as a second camera or a second readiness
# sample stream.  The alias is scenario/demand-gated by camera_alias_relay.
STANDARD_INPUT_ENDPOINTS = (
    CvEndpoint(
        key="cam4_rgb",
        topic="/synced/cam_4/color/image_raw/compressed",
        message_type="sensor_msgs/msg/CompressedImage",
        qos="RELIABLE/VOLATILE/KEEP_LAST(20)",
        owner="VIPLab",
        direction="input",
        required_now=True,
    ),
    CvEndpoint(
        key="cam4_rgb_alias",
        topic="/surgery/images/cam4/compressed",
        message_type="sensor_msgs/msg/CompressedImage",
        qos="BEST_EFFORT/VOLATILE/KEEP_LAST(5)",
        owner="Taskplanner camera_alias_relay",
        direction="alias",
        pending_reason="same_physical_source_as_cam4_rgb; never double-counted",
    ),
    CvEndpoint(
        key="cam4_camera_info",
        topic="/synced/cam_4/color/camera_info",
        message_type="sensor_msgs/msg/CameraInfo",
        qos="RELIABLE/VOLATILE/KEEP_LAST(20)",
        owner="VIPLab",
        direction="input",
        pending_reason="provider_and_calibration_pending",
    ),
    CvEndpoint(
        key="cam4_aligned_depth",
        topic="/synced/cam_4/depth/image_rect_raw",
        message_type="sensor_msgs/msg/Image",
        qos="BEST_EFFORT/VOLATILE/KEEP_LAST(5)",
        owner="VIPLab",
        direction="input",
        pending_reason="provider_encoding_units_and_sync_policy_pending",
    ),
    CvEndpoint(
        key="handover_tray_rgb",
        topic="/surgery/images/tray/compressed",
        message_type="sensor_msgs/msg/CompressedImage",
        qos="BEST_EFFORT/VOLATILE/KEEP_LAST(5)",
        owner="VIPLab",
        direction="input",
        pending_reason="optional_handover_tray_camera_not_mayo",
    ),
    CvEndpoint(
        key="handover_tray_camera_info",
        topic="/surgery/cameras/tray/color/camera_info",
        message_type="sensor_msgs/msg/CameraInfo",
        qos="RELIABLE/VOLATILE/KEEP_LAST(5)",
        owner="VIPLab",
        direction="input",
        pending_reason="optional_handover_tray_camera_not_mayo",
    ),
    CvEndpoint(
        key="handover_tray_aligned_depth",
        topic="/surgery/cameras/tray/aligned_depth",
        message_type="sensor_msgs/msg/Image",
        qos="BEST_EFFORT/VOLATILE/KEEP_LAST(5)",
        owner="VIPLab",
        direction="input",
        pending_reason="optional_handover_tray_camera_not_mayo",
    ),
)


# These are CV-owned output contracts from the workbook.  The only purpose of
# recording them before their packages arrive is graph/type/QoS/ownership
# inspection.  This runtime never creates dummy publishers for these topics.
EXTERNAL_OUTPUT_ENDPOINTS = (
    CvEndpoint(
        "cam4_tool_observations",
        "/surgery/perception/cam4/observations",
        "surgical_perception_msgs/msg/ToolObservation2DArray",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(2)",
        "CV team",
        "output",
    ),
    CvEndpoint(
        "cam4_tool_poses",
        "/surgery/perception/cam4/tool_poses",
        "surgical_perception_msgs/msg/ToolPoseArray",
        "RELIABLE/VOLATILE/KEEP_LAST(5)",
        "CV team",
        "output",
        pending_reason="requires_calibration_tf_depth_and_ontology",
    ),
    CvEndpoint(
        "cam4_tool_overlay",
        "/surgery/images/cam4/detection_overlay/compressed",
        "sensor_msgs/msg/CompressedImage",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(2)",
        "CV team",
        "output",
    ),
    CvEndpoint(
        "tray_tool_observations",
        "/surgery/perception/tray/observations",
        "surgical_perception_msgs/msg/ToolObservation2DArray",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(2)",
        "CV team",
        "output",
        pending_reason="handover_tray_not_mayo",
    ),
    CvEndpoint(
        "tray_tool_poses",
        "/surgery/perception/tray/tool_poses",
        "surgical_perception_msgs/msg/ToolPoseArray",
        "RELIABLE/VOLATILE/KEEP_LAST(5)",
        "CV team",
        "output",
        pending_reason="handover_tray_not_mayo",
    ),
    CvEndpoint(
        "tray_tool_overlay",
        "/surgery/images/tray/detection_overlay/compressed",
        "sensor_msgs/msg/CompressedImage",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(2)",
        "CV team",
        "output",
        pending_reason="handover_tray_not_mayo",
    ),
    CvEndpoint(
        "rfdetr_diagnostics",
        "/surgery/perception/rfdetr/diagnostics/json",
        "std_msgs/msg/String",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(10)",
        "CV team",
        "output",
    ),
    CvEndpoint(
        "rfdetr_health",
        "/surgery/perception/rfdetr/health",
        "std_msgs/msg/String",
        "RELIABLE/TRANSIENT_LOCAL/KEEP_LAST(1)",
        "CV team",
        "output",
    ),
    CvEndpoint(
        "cam4_hand_keypoints",
        "/surgery/perception/cam4/hand_keypoints",
        "hand_keypoint_interfaces/msg/HandKeypoints",
        "RELIABLE/VOLATILE/KEEP_LAST(10)",
        "CV team",
        "output",
    ),
    CvEndpoint(
        "cam4_hand_target_pose",
        "/surgery/perception/cam4/hand_target_pose",
        "geometry_msgs/msg/PoseStamped",
        "RELIABLE/VOLATILE/KEEP_LAST(10)",
        "CV team",
        "output",
        pending_reason="monitor_only_until_validity_age_tf_and_robot_contract_are_agreed",
    ),
    CvEndpoint(
        "cam4_hand_overlay",
        "/surgery/images/cam4/hand_overlay/compressed",
        "sensor_msgs/msg/CompressedImage",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(2)",
        "CV team",
        "output",
    ),
    CvEndpoint(
        "hand_diagnostics",
        "/surgery/perception/handkeypoint/diagnostics/json",
        "std_msgs/msg/String",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(10)",
        "CV team",
        "output",
    ),
    CvEndpoint(
        "hand_health",
        "/surgery/perception/handkeypoint/health",
        "std_msgs/msg/String",
        "RELIABLE/TRANSIENT_LOCAL/KEEP_LAST(1)",
        "CV team",
        "output",
    ),
    CvEndpoint(
        "suction_rgb",
        "/surgery/images/suction_camera/color/compressed",
        "sensor_msgs/msg/CompressedImage",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(5)",
        "CV team",
        "output",
        pending_reason="d405_provider_pending",
    ),
    CvEndpoint(
        "suction_aligned_depth",
        "/surgery/images/suction_camera/aligned_depth",
        "sensor_msgs/msg/Image",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(5)",
        "CV team",
        "output",
        pending_reason="d405_provider_encoding_units_and_sync_policy_pending",
    ),
    CvEndpoint(
        "suction_camera_info",
        "/surgery/images/suction_camera/camera_info",
        "sensor_msgs/msg/CameraInfo",
        "RELIABLE/TRANSIENT_LOCAL/KEEP_LAST(1)",
        "CV team",
        "output",
        pending_reason="d405_provider_and_calibration_pending",
    ),
    CvEndpoint(
        "bleeding_mask",
        "/surgery/perception/bleeding/mask",
        "sensor_msgs/msg/Image",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(5)",
        "CV team",
        "output",
        pending_reason="d405_provider_pending",
    ),
    CvEndpoint(
        "bleeding_overlay",
        "/surgery/images/suction_camera/bleeding_overlay/compressed",
        "sensor_msgs/msg/CompressedImage",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(2)",
        "CV team",
        "output",
        pending_reason="d405_provider_pending",
    ),
    CvEndpoint(
        "bleeding_diagnostics",
        "/surgery/perception/bleeding/diagnostics/json",
        "std_msgs/msg/String",
        "BEST_EFFORT/VOLATILE/KEEP_LAST(10)",
        "CV team",
        "output",
    ),
    CvEndpoint(
        "bleeding_health",
        "/surgery/perception/bleeding/health",
        "std_msgs/msg/String",
        "RELIABLE/TRANSIENT_LOCAL/KEEP_LAST(1)",
        "CV team",
        "output",
    ),
)


CUSTOM_IDL_PACKAGES = (
    "surgical_perception_msgs",
    "hand_keypoint_interfaces",
)


def endpoint_by_key(key: str) -> CvEndpoint:
    for endpoint in (*STANDARD_INPUT_ENDPOINTS, *EXTERNAL_OUTPUT_ENDPOINTS):
        if endpoint.key == key:
            return endpoint
    raise KeyError(key)
