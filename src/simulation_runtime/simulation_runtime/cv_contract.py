"""Stable ROS contract for local or LAN-hosted PNU perception.

The typed Tool and Hand interfaces are pinned copies of the upstream
``hand-blood-tools`` IDLs.  Geometry remains fail-closed unless live depth
registration, metric units, calibration and per-frame timing evidence pass;
publishing a topic alone never authorizes robot control.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import urlsplit


CV_CONTRACT_SCHEMA = "taskplanner.cv_external_contract.v1"
CV_CONTRACT_VERSION = "pnu-cv-interface-aligned-depth-3d-v2"
PERCEPTION_BACKENDS = frozenset({"local", "external", "disabled"})
PERCEPTION_PROVIDERS = frozenset(
    {"builtin_rfdetr", "pnu_hand_blood", "disabled"}
)
PERCEPTION_LOCATIONS = frozenset({"local", "remote"})


@dataclass(frozen=True)
class PerceptionSelection:
    """Resolved provider/location axes with the legacy backend projection."""

    provider: str
    location: str
    legacy_backend: str
    source: str


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


def normalize_perception_provider(value: object) -> str:
    """Return a supported perception implementation identifier."""

    provider = str(value).strip().casefold()
    if provider not in PERCEPTION_PROVIDERS:
        choices = ", ".join(sorted(PERCEPTION_PROVIDERS))
        raise ValueError(
            f"PERCEPTION_PROVIDER must be one of {choices}; received {value!r}"
        )
    return provider


def normalize_perception_location(value: object) -> str:
    """Return the host-placement axis without silently enabling failover."""

    location = str(value).strip().casefold()
    if location not in PERCEPTION_LOCATIONS:
        choices = ", ".join(sorted(PERCEPTION_LOCATIONS))
        raise ValueError(
            f"PERCEPTION_LOCATION must be one of {choices}; received {value!r}"
        )
    return location


def resolve_perception_selection(
    *,
    provider: object = "",
    location: object = "",
    legacy_backend: object = "local",
) -> PerceptionSelection:
    """Resolve explicit axes, falling back to the old backend contract.

    An explicitly configured provider never inherits placement from the legacy
    backend.  This prevents a stale ``PERCEPTION_BACKEND=external`` value from
    unexpectedly moving a newly selected provider to a remote host.
    """

    provider_text = str(provider).strip()
    location_text = str(location).strip()
    if provider_text:
        resolved_provider = normalize_perception_provider(provider_text)
        resolved_location = normalize_perception_location(
            location_text if location_text else "local"
        )
        source = "explicit_axes"
    else:
        if location_text:
            raise ValueError(
                "PERCEPTION_LOCATION requires PERCEPTION_PROVIDER when the "
                "legacy PERCEPTION_BACKEND fallback is in use"
            )
        backend = normalize_perception_backend(legacy_backend)
        resolved_provider, resolved_location = {
            "local": ("builtin_rfdetr", "local"),
            "external": ("pnu_hand_blood", "remote"),
            "disabled": ("disabled", "local"),
        }[backend]
        source = "legacy_backend"

    legacy_projection = {
        "builtin_rfdetr": "local",
        "pnu_hand_blood": "external",
        "disabled": "disabled",
    }[resolved_provider]
    return PerceptionSelection(
        provider=resolved_provider,
        location=resolved_location,
        legacy_backend=legacy_projection,
        source=source,
    )


def validate_perception_endpoint(
    value: object, selection: PerceptionSelection
) -> str:
    """Validate placement against an explicit HTTP(S) worker endpoint.

    Location switching is deliberate: local workers may only use loopback,
    while remote workers may never silently fall back to loopback or a bind-all
    address.  Authentication material belongs in a mounted secret, not URL
    user-info.
    """

    endpoint = str(value).strip()
    if selection.provider == "disabled":
        if endpoint:
            raise ValueError(
                "PERCEPTION_ENDPOINT must be empty when perception is disabled"
            )
        return ""
    if not endpoint:
        raise ValueError(
            "PERCEPTION_ENDPOINT is required for an enabled perception provider"
        )

    parsed = urlsplit(endpoint)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(
            "PERCEPTION_ENDPOINT must be an absolute http(s) URL with a host"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "PERCEPTION_ENDPOINT must not contain credentials; use a mounted secret"
        )
    if parsed.query or parsed.fragment:
        raise ValueError(
            "PERCEPTION_ENDPOINT must not contain a query string or fragment"
        )
    if parsed.path not in {"", "/"}:
        raise ValueError(
            "PERCEPTION_ENDPOINT must be an origin URL without an API path"
        )
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"PERCEPTION_ENDPOINT has an invalid port: {exc}") from exc

    hostname = parsed.hostname.casefold().rstrip(".")
    local_names = {"localhost", "localhost.localdomain"}
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname in local_names:
            addresses = (ipaddress.ip_address("127.0.0.1"),)
        else:
            try:
                resolved = socket.getaddrinfo(
                    hostname,
                    None,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                )
            except socket.gaierror as exc:
                raise ValueError(
                    "PERCEPTION_ENDPOINT hostname must resolve during validation"
                ) from exc
            addresses = tuple(
                {
                    ipaddress.ip_address(item[4][0].split("%", 1)[0])
                    for item in resolved
                }
            )
            if not addresses:
                raise ValueError(
                    "PERCEPTION_ENDPOINT hostname resolved to no IP addresses"
                )
    else:
        addresses = (address,)

    all_loopback = all(item.is_loopback for item in addresses)
    any_forbidden_remote = any(
        item.is_loopback or item.is_unspecified for item in addresses
    )

    if selection.location == "local" and not all_loopback:
        raise ValueError(
            "PERCEPTION_LOCATION=local requires a loopback PERCEPTION_ENDPOINT"
        )
    if selection.location == "remote" and any_forbidden_remote:
        raise ValueError(
            "PERCEPTION_LOCATION=remote requires a non-loopback worker endpoint"
        )
    return endpoint.rstrip("/")


# CAM4 has one canonical physical RGB source.  The public /surgery alias is
# deliberately listed as an alias, not as a second camera or a second readiness
# sample stream.  The alias is scenario/demand-gated by camera_alias_relay.
STANDARD_INPUT_ENDPOINTS = (
    CvEndpoint(
        key="cam4_rgb",
        topic="/synced/cam_4/color/image_raw/compressed",
        message_type="sensor_msgs/msg/CompressedImage",
        qos="BEST_EFFORT/VOLATILE/KEEP_LAST(1)",
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
        key="cam4_depth_camera_info",
        topic="/synced/cam_4/depth/camera_info",
        message_type="sensor_msgs/msg/CameraInfo",
        qos="RELIABLE/VOLATILE/KEEP_LAST(20)",
        owner="VIPLab",
        direction="input",
        pending_reason="calibration_version_and_depth_to_color_extrinsics_pending",
    ),
    CvEndpoint(
        key="cam4_native_depth_compressed",
        topic="/synced/cam_4/depth/image_rect_raw/compressedDepth",
        message_type="sensor_msgs/msg/CompressedImage",
        qos="BEST_EFFORT/VOLATILE/KEEP_LAST(1)",
        owner="VIPLab",
        direction="input",
        pending_reason=(
            "native_depth_optical_frame; align_depth_disabled; requires "
            "validated_depth_to_color_registration_before_metric_pose"
        ),
    ),
    CvEndpoint(
        key="cam4_depth_to_color_extrinsics",
        topic="/synced/cam_4/extrinsics/depth_to_color",
        message_type="realsense2_camera_msgs/msg/Extrinsics",
        qos="RELIABLE/TRANSIENT_LOCAL/KEEP_LAST(1)",
        owner="VIPLab",
        direction="input",
        pending_reason=(
            "message_idl_documents_column_major_but_viplab_fallback_is_named_"
            "row_major; transpose_semantics_must_be_validated_before_registration"
        ),
    ),
    CvEndpoint(
        key="cam4_aligned_depth_compressed",
        topic=(
            "/synced/cam_4/aligned_depth_to_color/"
            "image_raw/compressedDepth"
        ),
        message_type="sensor_msgs/msg/CompressedImage",
        qos="BEST_EFFORT/VOLATILE/KEEP_LAST(1)",
        owner="VIPLab CAM4 librealsense align filter",
        direction="input",
        pending_reason=(
            "cam4_only_rgb_aligned_16uc1_compressedDepth; metric use requires "
            "matching color optical frame, dimensions, source stamps and "
            "validated live sensor scale"
        ),
    ),
    CvEndpoint(
        key="cam4_aligned_depth_camera_info",
        topic="/synced/cam_4/aligned_depth_to_color/camera_info",
        message_type="sensor_msgs/msg/CameraInfo",
        qos="RELIABLE/VOLATILE/KEEP_LAST(20)",
        owner="VIPLab CAM4 librealsense align filter",
        direction="input",
        pending_reason=(
            "must match CAM4 color CameraInfo dimensions, optical frame and "
            "calibration before metric output"
        ),
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


# These are the upstream-compatible output contracts.  The PNU ROS bridge owns
# CAM4 publications when that provider is selected; the monitor itself never
# creates dummy publishers or treats topic presence as geometry authorization.
EXTERNAL_OUTPUT_ENDPOINTS = (
    CvEndpoint(
        "cam4_tool_observations",
        "/surgery/perception/cam4/observations",
        "surgical_perception_msgs/msg/ToolObservation2DArray",
        "RELIABLE/VOLATILE/KEEP_LAST(10)",
        "Taskplanner PNU bridge",
        "output",
    ),
    CvEndpoint(
        "cam4_tool_poses",
        "/surgery/perception/cam4/tool_poses",
        "surgical_perception_msgs/msg/ToolPoseArray",
        "RELIABLE/VOLATILE/KEEP_LAST(10)",
        "Taskplanner PNU bridge",
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
        "cam4_blood_semantics",
        "/surgery/perception/cam4/blood_semantics/json",
        "std_msgs/msg/String",
        "RELIABLE/VOLATILE/KEEP_LAST(10)",
        "Taskplanner PNU bridge",
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
    "realsense2_camera_msgs",
)


def endpoint_by_key(key: str) -> CvEndpoint:
    for endpoint in (*STANDARD_INPUT_ENDPOINTS, *EXTERNAL_OUTPUT_ENDPOINTS):
        if endpoint.key == key:
            return endpoint
    raise KeyError(key)
