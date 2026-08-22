"""Bring up Taskplanner for public-evidence-only surgical-video shadow replay."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen
import uuid

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    LogInfo,
    OpaqueFunction,
    SetLaunchConfiguration,
)
from launch.conditions import IfCondition
from launch.substitutions import (
    EnvironmentVariable,
    LaunchConfiguration,
    PathJoinSubstitution,
    PythonExpression,
)
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare
import yaml

from bringup.perception_config import resolve_launch_perception


SHADOW_CONTRACT_VERSION = "shadow-rfdetr-preflight-v1"
IMAGE_MESSAGE_TYPE = "sensor_msgs/msg/CompressedImage"
BUILD_MARKER_PATH = Path("/etc/taskplanner-build.json")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _bed_robot_contract_configuration(context: Any) -> list[Any]:
    bundle_id = LaunchConfiguration("default_bundle").perform(context).strip()
    if bundle_id in {"thyroidectomy", "thyroidectomy_demo"}:
        procedure_type = "thyroidectomy"
    elif bundle_id == "nephrectomy":
        procedure_type = "nephrectomy"
    else:
        procedure_type = ""
    return [
        SetLaunchConfiguration(
            "bed_robot_contract_enabled",
            "true" if procedure_type else "false",
        ),
        SetLaunchConfiguration(
            "bed_robot_contract_procedure_type",
            procedure_type,
        ),
    ]


def _as_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected boolean value, got {value!r}")


def _validate_topic(name: str, value: str) -> None:
    topic = str(value).strip()
    if not topic:
        raise ValueError(f"{name} must be declared")
    if not topic.startswith("/") or "//" in topic or topic.endswith("/"):
        raise ValueError(f"{name} is not a valid absolute ROS topic: {topic!r}")


def validate_shadow_routes(routes: dict[str, str]) -> None:
    """Reject ambiguous source and normalized topic routing."""
    required = {
        "source_field_image_topic",
        "source_cam1_topic",
        "source_cam2_topic",
        "source_cam3_topic",
        "source_cam4_topic",
        "source_flir_topic",
        "source_bbox_topic",
        "source_segmentation_topic",
        "source_transcript_topic",
        "field_image_topic",
        "flir_image_topic",
        "cam4_image_topic",
        "segmented_flir_image_topic",
        "cam4_overlay_image_topic",
        "cam4_semantics_topic",
    }
    missing = sorted(required.difference(routes))
    if missing:
        raise ValueError(f"route values are missing: {', '.join(missing)}")
    for name in sorted(required):
        _validate_topic(name, routes[name])

    source_cameras = {
        name: routes[name]
        for name in (
            "source_cam1_topic",
            "source_cam2_topic",
            "source_cam3_topic",
            "source_cam4_topic",
            "source_flir_topic",
        )
    }
    duplicates: dict[str, list[str]] = {}
    for name, topic in source_cameras.items():
        duplicates.setdefault(topic, []).append(name)
    duplicate_routes = {
        topic: names for topic, names in duplicates.items() if len(names) > 1
    }
    if duplicate_routes:
        details = "; ".join(
            f"{topic}: {', '.join(names)}"
            for topic, names in sorted(duplicate_routes.items())
        )
        raise ValueError(f"source camera routes must be unique ({details})")

    if routes["source_field_image_topic"] != routes["source_cam4_topic"]:
        raise ValueError(
            "source_field_image_topic is a compatibility alias and must match "
            "source_cam4_topic"
        )
    source_perception = {
        "source_bbox_topic": routes["source_bbox_topic"],
        "source_segmentation_topic": routes["source_segmentation_topic"],
    }
    if routes["source_bbox_topic"] == routes["source_segmentation_topic"]:
        raise ValueError(
            "source_bbox_topic and source_segmentation_topic must be distinct"
        )
    for name, topic in source_perception.items():
        if topic in source_cameras.values():
            raise ValueError(
                f"{name} must not overlap a source camera route: {topic!r}"
            )

    source_topics = set(source_cameras.values())
    source_topics.update(source_perception.values())
    if routes["source_transcript_topic"] in source_topics:
        raise ValueError(
            "source_transcript_topic must not overlap an image or perception route"
        )
    source_topics.add(routes["source_transcript_topic"])

    normalized = {
        name: routes[name]
        for name in (
            "field_image_topic",
            "flir_image_topic",
            "cam4_image_topic",
            "segmented_flir_image_topic",
            "cam4_overlay_image_topic",
            "cam4_semantics_topic",
        )
    }
    duplicates.clear()
    for name, topic in normalized.items():
        duplicates.setdefault(topic, []).append(name)
    duplicate_outputs = {
        topic: names for topic, names in duplicates.items() if len(names) > 1
    }
    if duplicate_outputs:
        details = "; ".join(
            f"{topic}: {', '.join(names)}"
            for topic, names in sorted(duplicate_outputs.items())
        )
        raise ValueError(f"normalized output routes must be unique ({details})")
    source_output_overlap = sorted(set(normalized.values()).intersection(source_topics))
    if source_output_overlap:
        raise ValueError(
            "normalized output routes must not overlap source inputs "
            f"({', '.join(source_output_overlap)})"
        )


def _metadata_path(bag_path: str | Path) -> Path:
    path = Path(bag_path).expanduser()
    if path.is_dir():
        return path / "metadata.yaml"
    if path.name == "metadata.yaml":
        return path
    return path.parent / "metadata.yaml"


def read_bag_topics(bag_path: str | Path) -> dict[str, str]:
    """Read topic names and types without opening the MCAP payload."""
    metadata_path = _metadata_path(bag_path)
    if not metadata_path.is_file():
        raise FileNotFoundError(
            f"rosbag metadata was not found at {metadata_path}"
        )
    payload = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    info = payload.get("rosbag2_bagfile_information", payload)
    rows = info.get("topics_with_message_count", [])
    topics: dict[str, str] = {}
    for row in rows:
        metadata = row.get("topic_metadata", {}) if isinstance(row, dict) else {}
        name = str(metadata.get("name", "")).strip()
        message_type = str(metadata.get("type", "")).strip()
        if name:
            topics[name] = message_type
    if not topics:
        raise ValueError(f"rosbag metadata contains no topics: {metadata_path}")
    return topics


def inspect_bag_routes(
    bag_path: str | Path,
    routes: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Return blocking VLM route errors and non-blocking public-input warnings."""
    topics = read_bag_topics(bag_path)
    errors: list[str] = []
    warnings: list[str] = []
    for label in ("source_cam4_topic", "source_flir_topic"):
        topic = routes[label]
        if topic not in topics:
            errors.append(f"{label} {topic!r} is absent from the rosbag")
        elif topics[topic] != IMAGE_MESSAGE_TYPE:
            errors.append(
                f"{label} {topic!r} has type {topics[topic]!r}; "
                f"expected {IMAGE_MESSAGE_TYPE!r}"
            )
    for label in (
        "source_cam1_topic",
        "source_cam2_topic",
        "source_cam3_topic",
        "source_bbox_topic",
        "source_segmentation_topic",
        "source_transcript_topic",
    ):
        topic = routes[label]
        if topic not in topics:
            warnings.append(f"{label} {topic!r} is absent from the rosbag")
    return errors, warnings


def fetch_rfdetr_health(
    service_url: str,
    timeout_sec: float,
    *,
    opener: Any = urlopen,
) -> dict[str, Any]:
    """Validate the host RF-DETR service without loading or invoking a model."""
    base_url = str(service_url).strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid RF-DETR service URL: {service_url!r}")
    response = opener(f"{base_url}/health", timeout=max(0.1, timeout_sec))
    try:
        status = int(getattr(response, "status", 200))
        body = response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if status != 200:
        raise RuntimeError(f"RF-DETR health returned HTTP {status}")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        raise RuntimeError(f"RF-DETR service is not ready: {payload!r}")
    models = payload.get("models", {})
    if not isinstance(models, dict) or not models.get("flir") or not models.get(
        "cam4"
    ):
        raise RuntimeError(
            "RF-DETR health is missing the FLIR or CAM4 model readiness marker"
        )
    return payload


def find_ros_executable(
    package: str,
    executable: str,
    *,
    prefixes: list[str] | None = None,
) -> Path | None:
    """Find a ROS console script without invoking ros2 during launch."""
    search_prefixes = list(prefixes or [])
    if not search_prefixes:
        for variable in ("AMENT_PREFIX_PATH", "COLCON_PREFIX_PATH"):
            search_prefixes.extend(
                part
                for part in os.environ.get(variable, "").split(os.pathsep)
                if part
            )
        workspace = os.environ.get("TASKPLANNER_WS", "").strip()
        if workspace:
            search_prefixes.append(str(Path(workspace) / "install" / package))
    for prefix in search_prefixes:
        candidate = Path(prefix) / "lib" / package / executable
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    direct = shutil.which(executable)
    return Path(direct) if direct else None


def read_build_marker(
    *,
    marker_path: Path = BUILD_MARKER_PATH,
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    marker: dict[str, str] = {}
    if marker_path.is_file():
        try:
            value = json.loads(marker_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                marker.update({str(key): str(item) for key, item in value.items()})
        except (OSError, ValueError):
            marker["marker_error"] = f"could not read {marker_path}"
    env = environment if environment is not None else os.environ
    overrides = {
        "image_version": env.get("TASKPLANNER_IMAGE_VERSION", ""),
        "git_sha": env.get("TASKPLANNER_IMAGE_GIT_SHA", ""),
        "shadow_contract": env.get(
            "TASKPLANNER_SHADOW_CONTRACT_VERSION",
            "",
        ),
    }
    marker.update({key: value for key, value in overrides.items() if value})
    return marker


def select_trace_path(
    trace_root: str | Path,
    run_id: str,
    requested_path: str = "",
) -> Path:
    """Choose a new trace path while preserving explicitly requested paths."""
    def reserve(candidate: Path) -> bool:
        reservation = candidate.with_name(f"{candidate.name}.reserved")
        try:
            descriptor = os.open(
                reservation,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "run_id": run_id,
                    "trace_path": str(candidate),
                    "pid": os.getpid(),
                    "reserved_at": datetime.now(timezone.utc).isoformat(),
                },
                stream,
                sort_keys=True,
            )
            stream.write("\n")
        return True

    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError(
            "run_id must start with an alphanumeric character and contain only "
            "letters, digits, '.', '_' or '-'"
        )
    if requested_path.strip():
        candidate = Path(requested_path).expanduser().resolve()
        if candidate.exists():
            raise FileExistsError(
                f"explicit trace_path already exists: {candidate}"
            )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if not reserve(candidate):
            raise FileExistsError(
                f"explicit trace_path is already reserved: {candidate}"
            )
        return candidate

    run_dir = Path(trace_root).expanduser().resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    candidate = run_dir / "shadow_trace.v1.jsonl"
    suffix = 1
    while candidate.exists() or not reserve(candidate):
        candidate = run_dir / f"shadow_trace.v1.{suffix:03d}.jsonl"
        suffix += 1
    return candidate


def _configuration(context: Any, name: str) -> str:
    return str(LaunchConfiguration(name).perform(context)).strip()


def _shadow_preflight(context: Any) -> list[Any]:
    """Resolve run identity and stop unsafe shadow launches before nodes start."""
    require_vlm = _as_bool(_configuration(context, "require_vlm"))
    interactive = _as_bool(_configuration(context, "interactive_replay"))
    run_id = _configuration(context, "run_id")
    if not run_id:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{timestamp}-{uuid.uuid4().hex[:8]}"
    trace_path = select_trace_path(
        _configuration(context, "trace_root"),
        run_id,
        _configuration(context, "trace_path"),
    )

    routes = {
        name: _configuration(context, name)
        for name in (
            "source_field_image_topic",
            "source_cam1_topic",
            "source_cam2_topic",
            "source_cam3_topic",
            "source_cam4_topic",
            "source_flir_topic",
            "source_bbox_topic",
            "source_segmentation_topic",
            "source_transcript_topic",
            "field_image_topic",
            "flir_image_topic",
            "cam4_image_topic",
            "segmented_flir_image_topic",
            "cam4_overlay_image_topic",
            "cam4_semantics_topic",
        )
    }
    try:
        validate_shadow_routes(routes)
    except ValueError as exc:
        raise RuntimeError(f"shadow route preflight failed: {exc}") from exc

    readiness_errors: list[str] = []
    warnings: list[str] = []
    if interactive:
        bag_path = _configuration(context, "source_bag_path")
        if not bag_path:
            raise RuntimeError(
                "shadow route preflight failed: source_bag_path is required "
                "when interactive_replay=true"
            )
        try:
            bag_errors, bag_warnings = inspect_bag_routes(bag_path, routes)
            readiness_errors.extend(bag_errors)
            warnings.extend(bag_warnings)
        except (OSError, ValueError) as exc:
            readiness_errors.append(str(exc))

    provider = _configuration(context, "perception_provider")
    adapter_enabled = provider == "builtin_rfdetr" and _as_bool(
        _configuration(context, "enable_rfdetr_perception")
    )
    bridge = None
    health: dict[str, Any] = {"status": "disabled"}
    if adapter_enabled:
        bridge = find_ros_executable("vlm_node", "rfdetr_perception_bridge")
        if bridge is None:
            readiness_errors.append(
                "rfdetr_perception_bridge is absent from the sourced ROS install; "
                "rebuild and source the latest workspace"
            )

        service_url = _configuration(context, "perception_endpoint")
        try:
            health = fetch_rfdetr_health(
                service_url,
                float(_configuration(context, "rfdetr_preflight_timeout_sec")),
            )
        except Exception as exc:
            readiness_errors.append(
                f"RF-DETR service health failed at {service_url}: {exc}"
            )
            health = {"status": "unavailable"}

    marker = read_build_marker()
    image_contract = marker.get("shadow_contract", "")
    if Path("/.dockerenv").exists() and not image_contract:
        readiness_errors.append(
            "container image has no shadow build marker; rebuild taskplanner-ws:dev"
        )
    elif image_contract and image_contract != SHADOW_CONTRACT_VERSION:
        readiness_errors.append(
            "container shadow contract is stale: "
            f"image={image_contract!r}, launch={SHADOW_CONTRACT_VERSION!r}"
        )

    if readiness_errors and require_vlm:
        details = "\n - ".join(readiness_errors)
        raise RuntimeError(
            "shadow VLM preflight failed (require_vlm=true):\n"
            f" - {details}\n"
            "Set require_vlm=false to keep replay and voice-driven planning "
            "available without visual inference."
        )
    if readiness_errors:
        warnings.extend(readiness_errors)

    marker_summary = (
        f"image={marker.get('image_version', 'unmarked')} "
        f"sha={marker.get('git_sha', 'unknown')} "
        f"contract={image_contract or 'unmarked'} "
        f"launch={SHADOW_CONTRACT_VERSION}"
    )
    actions: list[Any] = [
        SetLaunchConfiguration("run_id", run_id),
        SetLaunchConfiguration("trace_path", str(trace_path)),
        LogInfo(msg=f"[shadow preflight] build marker: {marker_summary}"),
        LogInfo(
            msg=(
                "[shadow preflight] ready: "
                f"run_id={run_id} trace={trace_path} "
                f"provider={provider} "
                f"bridge={bridge or ('missing' if adapter_enabled else 'disabled')} "
                f"rfdetr={health.get('status', 'unavailable')}"
            )
        ),
    ]
    actions.extend(
        LogInfo(msg=f"[shadow preflight][DEGRADED] {warning}")
        for warning in warnings
    )
    return actions


def generate_launch_description() -> LaunchDescription:
    spec_dir = LaunchConfiguration("spec_dir")
    mode = LaunchConfiguration("mode")
    run_id = LaunchConfiguration("run_id")
    case_id = LaunchConfiguration("case_id")
    trace_path = LaunchConfiguration("trace_path")
    reference_path = LaunchConfiguration("reference_path")
    tool_catalog_path = LaunchConfiguration("tool_catalog_path")
    field_image_topic = LaunchConfiguration("field_image_topic")
    flir_image_topic = LaunchConfiguration("flir_image_topic")
    cam4_image_topic = LaunchConfiguration("cam4_image_topic")
    segmented_flir_image_topic = LaunchConfiguration(
        "segmented_flir_image_topic"
    )
    cam4_overlay_image_topic = LaunchConfiguration(
        "cam4_overlay_image_topic"
    )
    composite_image_topic = LaunchConfiguration("composite_image_topic")
    cam4_semantics_topic = LaunchConfiguration("cam4_semantics_topic")
    perception_backend = LaunchConfiguration("perception_backend")
    perception_provider = LaunchConfiguration("perception_provider")
    perception_location = LaunchConfiguration("perception_location")
    perception_endpoint = LaunchConfiguration("perception_endpoint")
    pnu_api_token_file = LaunchConfiguration("pnu_api_token_file")
    pnu_allow_insecure_remote_http = LaunchConfiguration(
        "pnu_allow_insecure_remote_http"
    )
    pnu_allow_unauthenticated_remote = LaunchConfiguration(
        "pnu_allow_unauthenticated_remote"
    )
    pnu_expected_model_digests_json = LaunchConfiguration(
        "pnu_expected_model_digests_json"
    )
    pnu_expected_tool_support_plane_config_version = LaunchConfiguration(
        "pnu_expected_tool_support_plane_config_version"
    )
    pnu_depth_scale_m_per_unit = LaunchConfiguration(
        "pnu_depth_scale_m_per_unit"
    )
    pnu_depth_scale_validated = LaunchConfiguration(
        "pnu_depth_scale_validated"
    )
    pnu_depth_alignment_validated = LaunchConfiguration(
        "pnu_depth_alignment_validated"
    )
    pnu_depth_alignment_id = LaunchConfiguration("pnu_depth_alignment_id")
    cv_contract_status_topic = LaunchConfiguration("cv_contract_status_topic")
    cv_cam4_rgb_topic = LaunchConfiguration("cv_cam4_rgb_topic")
    cv_cam4_camera_info_topic = LaunchConfiguration(
        "cv_cam4_camera_info_topic"
    )
    cv_cam4_native_depth_compressed_topic = LaunchConfiguration(
        "cv_cam4_native_depth_compressed_topic"
    )
    cv_cam4_depth_camera_info_topic = LaunchConfiguration(
        "cv_cam4_depth_camera_info_topic"
    )
    cv_cam4_depth_to_color_extrinsics_topic = LaunchConfiguration(
        "cv_cam4_depth_to_color_extrinsics_topic"
    )
    cv_cam4_aligned_depth_compressed_topic = LaunchConfiguration(
        "cv_cam4_aligned_depth_compressed_topic"
    )
    cv_cam4_aligned_depth_camera_info_topic = LaunchConfiguration(
        "cv_cam4_aligned_depth_camera_info_topic"
    )
    require_vlm = LaunchConfiguration("require_vlm")
    perception_bboxes_topic = LaunchConfiguration("perception_bboxes_topic")
    perception_segmentation_topic = LaunchConfiguration(
        "perception_segmentation_topic"
    )
    tray_image_topic = LaunchConfiguration("tray_image_topic")
    source_transcript_topic = LaunchConfiguration("source_transcript_topic")
    source_bag_path = LaunchConfiguration("source_bag_path")
    annotation_cases_root = LaunchConfiguration("annotation_cases_root")
    source_field_image_topic = LaunchConfiguration("source_field_image_topic")
    source_cam1_topic = LaunchConfiguration("source_cam1_topic")
    source_cam2_topic = LaunchConfiguration("source_cam2_topic")
    source_cam3_topic = LaunchConfiguration("source_cam3_topic")
    source_cam4_topic = LaunchConfiguration("source_cam4_topic")
    source_flir_topic = LaunchConfiguration("source_flir_topic")
    source_bbox_topic = LaunchConfiguration("source_bbox_topic")
    source_segmentation_topic = LaunchConfiguration(
        "source_segmentation_topic"
    )
    interactive_replay = LaunchConfiguration("interactive_replay")
    replay_mode = LaunchConfiguration("replay_mode")
    replay_rate = LaunchConfiguration("replay_rate")
    image_duration_sec = LaunchConfiguration("image_duration_sec")
    replay_vlm_health_timeout_sec = LaunchConfiguration(
        "replay_vlm_health_timeout_sec"
    )
    replay_vlm_wait_timeout_sec = LaunchConfiguration(
        "replay_vlm_wait_timeout_sec"
    )
    replay_vlm_soft_lag_sec = LaunchConfiguration(
        "replay_vlm_soft_lag_sec"
    )
    replay_vlm_hard_lag_sec = LaunchConfiguration(
        "replay_vlm_hard_lag_sec"
    )
    replay_vlm_hard_release_lag_sec = LaunchConfiguration(
        "replay_vlm_hard_release_lag_sec"
    )
    replay_vlm_max_visual_lead_sec = LaunchConfiguration(
        "replay_vlm_max_visual_lead_sec"
    )
    vlm_model_input_max_source_lag_sec = LaunchConfiguration(
        "vlm_model_input_max_source_lag_sec"
    )
    replay_drain_timeout_sec = LaunchConfiguration(
        "replay_drain_timeout_sec"
    )
    replay_drain_settle_sec = LaunchConfiguration(
        "replay_drain_settle_sec"
    )
    fault_scenario_path = LaunchConfiguration("fault_scenario_path")
    vlm_base_url = LaunchConfiguration("vlm_base_url")
    vlm_provider_id = LaunchConfiguration("vlm_provider_id")
    vlm_model_id = LaunchConfiguration("vlm_model_id")
    vlm_api_mode = LaunchConfiguration("vlm_api_mode")
    vlm_publish_period_sec = LaunchConfiguration("vlm_publish_period_sec")
    vlm_request_timeout_sec = LaunchConfiguration("vlm_request_timeout_sec")
    vlm_retry_count = LaunchConfiguration("vlm_retry_count")
    vlm_image_max_side_px = LaunchConfiguration("vlm_image_max_side_px")
    vlm_multiview_max_skew_sec = LaunchConfiguration(
        "vlm_multiview_max_skew_sec"
    )
    vlm_perception_image_max_skew_sec = LaunchConfiguration(
        "vlm_perception_image_max_skew_sec"
    )
    cam4_crop_x_norm = LaunchConfiguration("cam4_crop_x_norm")
    cam4_crop_y_norm = LaunchConfiguration("cam4_crop_y_norm")
    cam4_crop_width_norm = LaunchConfiguration("cam4_crop_width_norm")
    cam4_crop_height_norm = LaunchConfiguration("cam4_crop_height_norm")
    vlm_open_set_phase_bootstrap_observations = LaunchConfiguration(
        "vlm_open_set_phase_bootstrap_observations"
    )
    vlm_response_format = LaunchConfiguration("vlm_response_format")
    vlm_reasoning_effort = LaunchConfiguration("vlm_reasoning_effort")
    vlm_task_profile = LaunchConfiguration("vlm_task_profile")
    vlm_max_output_tokens = LaunchConfiguration("vlm_max_output_tokens")
    vlm_generation_seed = LaunchConfiguration("vlm_generation_seed")
    vlm_response_mode = LaunchConfiguration("vlm_response_mode")
    vlm_replay_response_path = LaunchConfiguration("vlm_replay_response_path")
    counterfactual_success_feedback = LaunchConfiguration(
        "counterfactual_success_feedback"
    )
    allow_type_instance_assumption = LaunchConfiguration(
        "allow_type_instance_assumption"
    )
    enable_rosbridge = LaunchConfiguration("enable_rosbridge")
    rosbridge_port = LaunchConfiguration("rosbridge_port")
    groot2_port = LaunchConfiguration("groot2_port")
    default_bundle = LaunchConfiguration("default_bundle")
    publish_shared_state = LaunchConfiguration("publish_shared_state")
    publish_shared_free_text = LaunchConfiguration(
        "publish_shared_free_text"
    )
    bed_robot_contract_enabled = LaunchConfiguration(
        "bed_robot_contract_enabled"
    )
    bed_robot_contract_procedure_type = LaunchConfiguration(
        "bed_robot_contract_procedure_type"
    )
    use_sim_time = {"use_sim_time": True}
    builtin_rfdetr_adapter_enabled = PythonExpression(
        [
            "'",
            perception_provider,
            "' == 'builtin_rfdetr' and '",
            LaunchConfiguration("enable_rfdetr_perception"),
            "'.lower() in ('true', '1', 'yes')",
        ]
    )
    pnu_adapter_enabled = PythonExpression(
        ["'", perception_provider, "' == 'pnu_hand_blood'"]
    )
    non_strict = PythonExpression(["'", mode, "' != 'strict'"])
    fault_enabled = PythonExpression(
        [
            "'",
            fault_scenario_path,
            "' != '' and '",
            interactive_replay,
            "'.lower() == 'true'",
        ]
    )
    replay_flir_output_topic = PythonExpression(
        [
            "'/test/fault/raw/flir/compressed' if '",
            fault_scenario_path,
            "' != '' and '",
            interactive_replay,
            "'.lower() == 'true' else '",
            flir_image_topic,
            "'",
        ]
    )
    replay_cam4_output_topic = PythonExpression(
        [
            "'/test/fault/raw/cam4/compressed' if '",
            fault_scenario_path,
            "' != '' and '",
            interactive_replay,
            "'.lower() == 'true' else '",
            cam4_image_topic,
            "'",
        ]
    )
    replay_transcript_output_topic = PythonExpression(
        [
            "'/test/fault/raw/speech/sentence' if '",
            fault_scenario_path,
            "' != '' and '",
            interactive_replay,
            "'.lower() == 'true' else '",
            source_transcript_topic,
            "'",
        ]
    )
    replay_vlm_result_output_topic = PythonExpression(
        [
            "'/test/fault/raw/vlm/result' if '",
            fault_scenario_path,
            "' != '' and '",
            interactive_replay,
            "'.lower() == 'true' else '/vlm/result'",
        ]
    )
    replay_vlm_health_output_topic = PythonExpression(
        [
            "'/test/fault/raw/vlm/health' if '",
            fault_scenario_path,
            "' != '' and '",
            interactive_replay,
            "'.lower() == 'true' else '/vlm/health'",
        ]
    )
    evaluation_observation_topic = PythonExpression(
        [
            "'/shadow/evaluation_observation' if '",
            mode,
            "' != 'strict' else ''",
        ]
    )
    spec_default = PathJoinSubstitution(
        [FindPackageShare("procedure_spec"), "specs", default_bundle]
    )
    rosbridge_process = ExecuteProcess(
        condition=IfCondition(enable_rosbridge),
        cmd=[
            "ros2",
            "run",
            "rosbridge_server",
            "rosbridge_websocket",
            "--ros-args",
            "-p",
            ["port:=", rosbridge_port],
            "-p",
            "address:=127.0.0.1",
        ],
        output="screen",
    )
    rosapi_node = Node(
        package="rosapi",
        executable="rosapi_node",
        name="rosapi",
        condition=IfCondition(enable_rosbridge),
        parameters=[{"use_sim_time": False}],
        output="screen",
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("default_bundle", default_value="thyroidectomy"),
            DeclareLaunchArgument("spec_dir", default_value=spec_default),
            DeclareLaunchArgument("mode", default_value="strict"),
            DeclareLaunchArgument("run_id", default_value=""),
            DeclareLaunchArgument("case_id"),
            DeclareLaunchArgument("trace_path", default_value=""),
            DeclareLaunchArgument(
                "trace_root",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_SHADOW_TRACE_ROOT",
                    default_value="output/shadow_runs",
                ),
            ),
            DeclareLaunchArgument("reference_path", default_value=""),
            DeclareLaunchArgument("tool_catalog_path", default_value=""),
            DeclareLaunchArgument(
                "field_image_topic",
                default_value="/surgery/images/field/compressed",
            ),
            DeclareLaunchArgument(
                "flir_image_topic",
                default_value="/surgery/images/flir/compressed",
            ),
            DeclareLaunchArgument(
                "cam4_image_topic",
                default_value="/surgery/images/cam4/compressed",
            ),
            DeclareLaunchArgument(
                "segmented_flir_image_topic",
                default_value="/surgery/images/flir/segmented/compressed",
            ),
            DeclareLaunchArgument(
                "cam4_overlay_image_topic",
                default_value="/surgery/images/cam4/detection_overlay/compressed",
            ),
            DeclareLaunchArgument(
                "composite_image_topic",
                default_value="/surgery/images/vlm/composite/compressed",
            ),
            DeclareLaunchArgument(
                "cam4_semantics_topic",
                default_value="/surgery/perception/cam4/semantics/json",
            ),
            DeclareLaunchArgument(
                "perception_backend",
                default_value=EnvironmentVariable(
                    "PERCEPTION_BACKEND",
                    default_value="local",
                ),
                description=(
                    "Legacy alias: local maps to builtin_rfdetr/local, external "
                    "to pnu_hand_blood/remote, and disabled to disabled/local."
                ),
            ),
            DeclareLaunchArgument(
                "perception_provider",
                default_value=EnvironmentVariable(
                    "PERCEPTION_PROVIDER",
                    default_value="",
                ),
                description=(
                    "Explicit provider axis. Empty maps PERCEPTION_BACKEND."
                ),
            ),
            DeclareLaunchArgument(
                "perception_location",
                default_value=EnvironmentVariable(
                    "PERCEPTION_LOCATION",
                    default_value="",
                ),
                description=(
                    "Worker placement: local or remote. No automatic failover."
                ),
            ),
            DeclareLaunchArgument(
                "perception_endpoint",
                default_value=EnvironmentVariable(
                    "PERCEPTION_ENDPOINT",
                    default_value="",
                ),
                description=(
                    "Explicit worker endpoint. Empty maps RFDETR_SERVICE_URL."
                ),
            ),
            DeclareLaunchArgument(
                "rfdetr_service_url",
                default_value=EnvironmentVariable(
                    "RFDETR_SERVICE_URL",
                    default_value="http://127.0.0.1:8010",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_service_url",
                default_value=EnvironmentVariable(
                    "PNU_SERVICE_URL",
                    default_value="",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_api_token_file",
                default_value=EnvironmentVariable(
                    "PNU_CLIENT_API_TOKEN_FILE",
                    default_value="",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_allow_insecure_remote_http",
                default_value=EnvironmentVariable(
                    "PNU_ALLOW_INSECURE_REMOTE_HTTP",
                    default_value="false",
                ),
                description=(
                    "Development-only opt-in for HTTP to a non-loopback PNU "
                    "worker. Remote endpoints require HTTPS by default."
                ),
            ),
            DeclareLaunchArgument(
                "pnu_allow_unauthenticated_remote",
                default_value=EnvironmentVariable(
                    "PNU_ALLOW_UNAUTHENTICATED_REMOTE",
                    default_value="false",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_expected_model_digests_json",
                default_value=EnvironmentVariable(
                    "PNU_EXPECTED_MODEL_DIGESTS_JSON",
                    default_value="{}",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_expected_tool_support_plane_config_version",
                default_value=EnvironmentVariable(
                    "PNU_EXPECTED_TOOL_SUPPORT_PLANE_CONFIG_VERSION",
                    default_value="",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_scale_m_per_unit",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_SCALE_M_PER_UNIT",
                    default_value="0.0",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_scale_validated",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_SCALE_VALIDATED",
                    default_value="false",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_alignment_validated",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_ALIGNMENT_VALIDATED",
                    default_value="false",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_alignment_id",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_ALIGNMENT_ID",
                    default_value="",
                ),
            ),
            DeclareLaunchArgument(
                "rfdetr_preflight_timeout_sec",
                default_value=EnvironmentVariable(
                    "RFDETR_PREFLIGHT_TIMEOUT_SEC",
                    default_value="2.0",
                ),
            ),
            DeclareLaunchArgument(
                "enable_rfdetr_perception",
                default_value="true",
            ),
            DeclareLaunchArgument(
                "cv_contract_status_topic",
                default_value="/integration/cv_contract/status",
            ),
            DeclareLaunchArgument(
                "cv_cam4_rgb_topic",
                default_value="/synced/cam_4/color/image_raw/compressed",
            ),
            DeclareLaunchArgument(
                "cv_cam4_camera_info_topic",
                default_value="/synced/cam_4/color/camera_info",
            ),
            DeclareLaunchArgument(
                "cv_cam4_native_depth_compressed_topic",
                default_value=(
                    "/synced/cam_4/depth/image_rect_raw/compressedDepth"
                ),
            ),
            DeclareLaunchArgument(
                "cv_cam4_depth_camera_info_topic",
                default_value="/synced/cam_4/depth/camera_info",
            ),
            DeclareLaunchArgument(
                "cv_cam4_depth_to_color_extrinsics_topic",
                default_value=(
                    "/synced/cam_4/extrinsics/depth_to_color"
                ),
            ),
            DeclareLaunchArgument(
                "cv_cam4_aligned_depth_compressed_topic",
                default_value=(
                    "/synced/cam_4/aligned_depth_to_color/"
                    "image_raw/compressedDepth"
                ),
            ),
            DeclareLaunchArgument(
                "cv_cam4_aligned_depth_camera_info_topic",
                default_value=(
                    "/synced/cam_4/aligned_depth_to_color/camera_info"
                ),
            ),
            DeclareLaunchArgument(
                "perception_bboxes_topic",
                default_value="/surgery/perception/cam4/tools/bboxes/json",
            ),
            DeclareLaunchArgument(
                "perception_segmentation_topic",
                default_value="/surgery/perception/cam4/tools/segmentation/json",
            ),
            DeclareLaunchArgument(
                "source_field_image_topic",
                default_value="/surgery/cam4/color/image/compressed",
            ),
            DeclareLaunchArgument(
                "source_cam1_topic",
                default_value="/surgery/cam1/color/image/compressed",
            ),
            DeclareLaunchArgument(
                "source_cam2_topic",
                default_value="/surgery/cam2/color/image/compressed",
            ),
            DeclareLaunchArgument(
                "source_cam3_topic",
                default_value="/surgery/cam3/color/image/compressed",
            ),
            DeclareLaunchArgument(
                "source_cam4_topic",
                default_value="/surgery/cam4/color/image/compressed",
            ),
            DeclareLaunchArgument(
                "source_flir_topic",
                default_value="/surgery/flir/image/compressed",
            ),
            DeclareLaunchArgument(
                "source_bbox_topic",
                default_value="/surgery/cam4/tools/bboxes/json",
            ),
            DeclareLaunchArgument(
                "source_segmentation_topic",
                default_value="/surgery/cam4/tools/segmentation/json",
            ),
            DeclareLaunchArgument(
                "tray_image_topic",
                default_value="/shadow/no_tray_image",
            ),
            DeclareLaunchArgument(
                "source_transcript_topic",
                default_value="/surgery/transcript",
            ),
            DeclareLaunchArgument("source_bag_path", default_value=""),
            DeclareLaunchArgument(
                "annotation_cases_root",
                default_value=PathJoinSubstitution(
                    [
                        EnvironmentVariable(
                            "TASKPLANNER_WS",
                            default_value="/workspaces/taskplanner_ws",
                        ),
                        "annotations",
                        "observable_tool_events",
                        "cases",
                    ]
                ),
            ),
            DeclareLaunchArgument("interactive_replay", default_value="false"),
            DeclareLaunchArgument("require_vlm", default_value="false"),
            DeclareLaunchArgument("replay_mode", default_value="elastic_demo"),
            DeclareLaunchArgument("replay_rate", default_value="1.0"),
            DeclareLaunchArgument("image_duration_sec", default_value="138.4284"),
            DeclareLaunchArgument(
                "replay_vlm_health_timeout_sec",
                default_value="15.0",
            ),
            DeclareLaunchArgument(
                "replay_vlm_wait_timeout_sec",
                default_value="20.0",
            ),
            DeclareLaunchArgument(
                "replay_vlm_soft_lag_sec",
                default_value="0.5",
            ),
            DeclareLaunchArgument(
                "replay_vlm_hard_lag_sec",
                default_value="2.5",
            ),
            DeclareLaunchArgument(
                "replay_vlm_hard_release_lag_sec",
                default_value="1.0",
            ),
            DeclareLaunchArgument(
                "replay_vlm_max_visual_lead_sec",
                default_value="0.35",
            ),
            DeclareLaunchArgument(
                "vlm_model_input_max_source_lag_sec",
                default_value="1.5",
            ),
            DeclareLaunchArgument(
                "replay_drain_timeout_sec",
                default_value="45.0",
            ),
            DeclareLaunchArgument(
                "replay_drain_settle_sec",
                default_value="1.25",
            ),
            DeclareLaunchArgument(
                "fault_scenario_path",
                default_value="",
                description=(
                    "Optional deterministic fault timeline. When set, only "
                    "the replay controller's public FLIR, CAM4, and transcript "
                    "outputs are relayed through the fault injector."
                ),
            ),
            DeclareLaunchArgument(
                "vlm_base_url",
                default_value="http://127.0.0.1:8001",
            ),
            DeclareLaunchArgument("vlm_provider_id", default_value="vllm"),
            DeclareLaunchArgument(
                "vlm_model_id",
                default_value="AxionML/Qwen3.5-4B-NVFP4",
            ),
            DeclareLaunchArgument("vlm_api_mode", default_value="openai_compat"),
            DeclareLaunchArgument("vlm_publish_period_sec", default_value="1.0"),
            DeclareLaunchArgument(
                "vlm_request_timeout_sec",
                default_value="6.0",
            ),
            DeclareLaunchArgument("vlm_retry_count", default_value="1"),
            DeclareLaunchArgument("vlm_image_max_side_px", default_value="1024"),
            DeclareLaunchArgument(
                "vlm_multiview_max_skew_sec",
                default_value="0.1",
            ),
            DeclareLaunchArgument(
                "vlm_perception_image_max_skew_sec",
                default_value="0.2",
            ),
            DeclareLaunchArgument("cam4_crop_x_norm", default_value="0.32"),
            DeclareLaunchArgument("cam4_crop_y_norm", default_value="0.18"),
            DeclareLaunchArgument("cam4_crop_width_norm", default_value="0.62"),
            DeclareLaunchArgument("cam4_crop_height_norm", default_value="0.78"),
            DeclareLaunchArgument(
                "vlm_open_set_phase_bootstrap_observations",
                default_value="24",
            ),
            DeclareLaunchArgument("vlm_response_format", default_value="json_schema"),
            DeclareLaunchArgument("vlm_reasoning_effort", default_value="none"),
            DeclareLaunchArgument(
                "vlm_task_profile",
                default_value="full",
                choices=("full", "tool_forecast_only"),
                description=(
                    "VLM task profile. tool_forecast_only is an isolated raw "
                    "next-tool benchmark and does not replace the normal profile."
                ),
            ),
            DeclareLaunchArgument("vlm_max_output_tokens", default_value="320"),
            DeclareLaunchArgument("vlm_generation_seed", default_value="0"),
            DeclareLaunchArgument("vlm_response_mode", default_value="live"),
            DeclareLaunchArgument("vlm_replay_response_path", default_value=""),
            DeclareLaunchArgument(
                "counterfactual_success_feedback",
                default_value="true",
            ),
            DeclareLaunchArgument(
                "allow_type_instance_assumption",
                default_value="true",
            ),
            DeclareLaunchArgument("enable_rosbridge", default_value="false"),
            DeclareLaunchArgument("rosbridge_port", default_value="9091"),
            DeclareLaunchArgument("groot2_port", default_value="0"),
            DeclareLaunchArgument(
                "publish_shared_state",
                default_value="true",
                description=(
                    "Publish the reviewed read-only /surgery/* state contract "
                    "during shadow replay."
                ),
            ),
            DeclareLaunchArgument(
                "publish_shared_free_text",
                default_value="false",
                description=(
                    "Allow reviewed free-form speech/VLM text in public state. "
                    "Keep false unless the integration network is approved for it."
                ),
            ),
            OpaqueFunction(function=_bed_robot_contract_configuration),
            OpaqueFunction(function=resolve_launch_perception),
            OpaqueFunction(function=_shadow_preflight),
            Node(
                package="surgical_interop_gateway",
                executable="surgical_interop_gateway",
                name="surgical_interop_gateway",
                condition=IfCondition(publish_shared_state),
                parameters=[
                    {
                        "default_bundle": default_bundle,
                        "publish_free_text": ParameterValue(
                            publish_shared_free_text,
                            value_type=bool,
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="shadow_evaluation",
                executable="interactive_replay_controller",
                name="interactive_shadow_replay_controller",
                condition=IfCondition(interactive_replay),
                parameters=[
                    {
                        "use_sim_time": False,
                        "case_id": ParameterValue(case_id, value_type=str),
                        "run_id": ParameterValue(run_id, value_type=str),
                        "procedure_id": ParameterValue(
                            default_bundle,
                            value_type=str,
                        ),
                        "bag_path": ParameterValue(
                            source_bag_path,
                            value_type=str,
                        ),
                        "annotation_cases_root": ParameterValue(
                            annotation_cases_root,
                            value_type=str,
                        ),
                        "source_image_topic": source_field_image_topic,
                        "source_cam1_topic": source_cam1_topic,
                        "source_cam2_topic": source_cam2_topic,
                        "source_cam3_topic": source_cam3_topic,
                        "source_cam4_topic": source_cam4_topic,
                        "source_flir_topic": source_flir_topic,
                        "source_bbox_topic": source_bbox_topic,
                        "source_segmentation_topic": (
                            source_segmentation_topic
                        ),
                        "output_image_topic": field_image_topic,
                        "transcript_topic": source_transcript_topic,
                        "mode": replay_mode,
                        "playback_rate": ParameterValue(
                            replay_rate,
                            value_type=float,
                        ),
                        "image_duration_sec": ParameterValue(
                            image_duration_sec,
                            value_type=float,
                        ),
                        "vlm_period_sec": ParameterValue(
                            vlm_publish_period_sec,
                            value_type=float,
                        ),
                        "require_vlm": ParameterValue(
                            require_vlm,
                            value_type=bool,
                        ),
                        "vlm_health_timeout_sec": ParameterValue(
                            replay_vlm_health_timeout_sec,
                            value_type=float,
                        ),
                        "vlm_wait_timeout_sec": ParameterValue(
                            replay_vlm_wait_timeout_sec,
                            value_type=float,
                        ),
                        "vlm_soft_lag_sec": ParameterValue(
                            replay_vlm_soft_lag_sec,
                            value_type=float,
                        ),
                        "vlm_hard_lag_sec": ParameterValue(
                            replay_vlm_hard_lag_sec,
                            value_type=float,
                        ),
                        "vlm_hard_release_lag_sec": ParameterValue(
                            replay_vlm_hard_release_lag_sec,
                            value_type=float,
                        ),
                        "vlm_max_visual_lead_sec": ParameterValue(
                            replay_vlm_max_visual_lead_sec,
                            value_type=float,
                        ),
                        "vlm_input_image_topic": (
                            composite_image_topic
                        ),
                        "drain_timeout_sec": ParameterValue(
                            replay_drain_timeout_sec,
                            value_type=float,
                        ),
                        "drain_settle_sec": ParameterValue(
                            replay_drain_settle_sec,
                            value_type=float,
                        ),
                        "auto_start": False,
                    }
                ],
                remappings=[
                    (flir_image_topic, replay_flir_output_topic),
                    (cam4_image_topic, replay_cam4_output_topic),
                    (
                        source_transcript_topic,
                        replay_transcript_output_topic,
                    ),
                ],
                output="screen",
            ),
            Node(
                package="simulation_runtime",
                executable="fault_injector",
                name="shadow_fault_injector",
                condition=IfCondition(fault_enabled),
                parameters=[
                    {
                        "use_sim_time": False,
                        "enabled": True,
                        "start_on_first_image": True,
                        "scenario_path": ParameterValue(
                            fault_scenario_path,
                            value_type=str,
                        ),
                        "raw_flir_topic": "/test/fault/raw/flir/compressed",
                        "raw_cam4_topic": "/test/fault/raw/cam4/compressed",
                        "raw_sentence_topic": (
                            "/test/fault/raw/speech/sentence"
                        ),
                        "flir_topic": flir_image_topic,
                        "cam4_topic": cam4_image_topic,
                        "sentence_topic": source_transcript_topic,
                        "raw_vlm_result_topic": (
                            "/test/fault/raw/vlm/result"
                        ),
                        "raw_vlm_health_topic": (
                            "/test/fault/raw/vlm/health"
                        ),
                        "vlm_result_topic": "/vlm/result",
                        "vlm_health_topic": "/vlm/health",
                    }
                ],
                output="screen",
            ),
            Node(
                package="btops_gateway",
                executable="btops_gateway",
                name="btops_gateway",
                parameters=[use_sim_time],
                output="screen",
            ),
            Node(
                package="auto_apms_behavior_tree",
                executable="tree_executor",
                name="tree_executor",
                parameters=[
                    {
                        **use_sim_time,
                        "tick_rate": 0.1,
                        "groot2_port": ParameterValue(
                            groot2_port,
                            value_type=int,
                        ),
                        "state_change_logger": True,
                    }
                ],
                output="screen",
            ),
            Node(
                package="shadow_evaluation",
                executable="recorded_transcript_adapter",
                name="recorded_transcript_adapter",
                parameters=[
                    {
                        **use_sim_time,
                        "input_topic": source_transcript_topic,
                        "output_topic": "/shadow/speech/utterance",
                        "case_id": ParameterValue(case_id, value_type=str),
                    }
                ],
                output="screen",
            ),
            Node(
                package="simulation_runtime",
                executable="speech_input_adapter",
                name="speech_input_adapter",
                parameters=[
                    {
                        **use_sim_time,
                        "input_topic": "/shadow/speech/utterance",
                        "output_topic": "/surgery/audio/request_text",
                        "required_speaker_role": "surgeon",
                        "accept_missing_confidence": True,
                        "require_timestamp": True,
                        "max_age_sec": 5.0,
                        "source_timeout_sec": 8.0,
                    }
                ],
                output="screen",
            ),
            # Shadow replay keeps model selection disabled by default while
            # exercising the same natural-language-to-typed-intent boundary.
            Node(
                package="voice_command",
                executable="voice_intent_resolver",
                name="voice_command_resolver",
                parameters=[
                    {
                        **use_sim_time,
                        "input_topic": "/surgery/audio/request_text",
                        "output_topic": "/surgery/voice/intent",
                        "procedure_bundle": spec_dir,
                        "selector_mode": "deterministic",
                    }
                ],
                output="screen",
            ),
            Node(
                package="simulation_runtime",
                executable="source_health_monitor",
                name="source_health_monitor",
                parameters=[
                    {
                        "use_sim_time": False,
                        "flir_topic": flir_image_topic,
                        "cam4_topic": cam4_image_topic,
                        "camera_stale_after_sec": 1.0,
                        "vlm_stale_after_sec": 3.0,
                    }
                ],
                output="screen",
            ),
            Node(
                package="simulation_runtime",
                executable="cv_contract_monitor",
                name="cv_contract_monitor",
                parameters=[
                    {
                        "perception_backend": perception_backend,
                        "perception_provider": perception_provider,
                        "perception_location": perception_location,
                        "perception_endpoint": perception_endpoint,
                        "status_topic": cv_contract_status_topic,
                        "cam4_rgb_topic": cv_cam4_rgb_topic,
                        "cam4_rgb_alias_topic": cam4_image_topic,
                        "cam4_camera_info_topic": cv_cam4_camera_info_topic,
                        "cam4_native_depth_compressed_topic": (
                            cv_cam4_native_depth_compressed_topic
                        ),
                        "cam4_depth_camera_info_topic": (
                            cv_cam4_depth_camera_info_topic
                        ),
                        "cam4_depth_to_color_extrinsics_topic": (
                            cv_cam4_depth_to_color_extrinsics_topic
                        ),
                        "cam4_aligned_depth_compressed_topic": (
                            cv_cam4_aligned_depth_compressed_topic
                        ),
                        "cam4_aligned_depth_camera_info_topic": (
                            cv_cam4_aligned_depth_camera_info_topic
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="vlm_node",
                executable="rfdetr_perception_bridge",
                name="rfdetr_perception_bridge",
                condition=IfCondition(builtin_rfdetr_adapter_enabled),
                parameters=[
                    {
                        # Perception control and health must remain responsive
                        # while interactive replay has its source clock paused.
                        "use_sim_time": False,
                        "service_url": perception_endpoint,
                        "flir_input_topic": flir_image_topic,
                        "cam4_input_topic": cam4_image_topic,
                        "flir_output_topic": segmented_flir_image_topic,
                        "cam4_overlay_topic": cam4_overlay_image_topic,
                        "cam4_semantics_topic": cam4_semantics_topic,
                        "max_source_skew_sec": ParameterValue(
                            vlm_multiview_max_skew_sec,
                            value_type=float,
                        ),
                        "max_rate_hz": 15.0,
                        "segmented_output_rate_hz": 2.0,
                    }
                ],
                output="screen",
            ),
            Node(
                package="vlm_node",
                executable="pnu_perception_bridge",
                name="pnu_perception_bridge",
                condition=IfCondition(pnu_adapter_enabled),
                parameters=[
                    {
                        # Replay stamps are historical by design; the worker
                        # still binds every response to the exact source stamp.
                        "use_sim_time": False,
                        "service_url": perception_endpoint,
                        "rgb_input_topic": cam4_image_topic,
                        "color_camera_info_topic": cv_cam4_camera_info_topic,
                        "depth_input_topic": (
                            cv_cam4_aligned_depth_compressed_topic
                        ),
                        "depth_camera_info_topic": (
                            cv_cam4_aligned_depth_camera_info_topic
                        ),
                        "cam4_overlay_topic": cam4_overlay_image_topic,
                        "cam4_semantics_topic": cam4_semantics_topic,
                        "cam4_mayo_observation_topic": (
                            "/surgery/perception/cam4/mayo_tool_observations"
                        ),
                        "diagnostics_topic": (
                            "/surgery/perception/rfdetr/diagnostics/json"
                        ),
                        "health_topic": "/surgery/perception/rfdetr/health",
                        "expected_model_digests_json": ParameterValue(
                            pnu_expected_model_digests_json,
                            value_type=str,
                        ),
                        "expected_tool_support_plane_config_version": ParameterValue(
                            pnu_expected_tool_support_plane_config_version,
                            value_type=str,
                        ),
                        "api_token_file": pnu_api_token_file,
                        "allow_insecure_remote_http": ParameterValue(
                            pnu_allow_insecure_remote_http,
                            value_type=bool,
                        ),
                        "allow_unauthenticated_remote": ParameterValue(
                            pnu_allow_unauthenticated_remote,
                            value_type=bool,
                        ),
                        "depth_scale_m_per_unit": ParameterValue(
                            pnu_depth_scale_m_per_unit,
                            value_type=float,
                        ),
                        "depth_scale_validated": ParameterValue(
                            pnu_depth_scale_validated,
                            value_type=bool,
                        ),
                        "depth_alignment_validated": ParameterValue(
                            pnu_depth_alignment_validated,
                            value_type=bool,
                        ),
                        "depth_alignment_id": pnu_depth_alignment_id,
                        "requested_algorithms": ["tool", "blood", "hand"],
                        "max_source_age_sec": 315360000.0,
                        "max_rate_hz": 15.0,
                    }
                ],
                output="screen",
            ),
            Node(
                package="vlm_node",
                executable="real_vlm",
                name="real_vlm_node",
                parameters=[
                    {
                        **use_sim_time,
                        "spec_dir": spec_dir,
                        "base_url": vlm_base_url,
                        "provider_id": vlm_provider_id,
                        "model_id": vlm_model_id,
                        "api_mode": vlm_api_mode,
                        "publish_period_sec": vlm_publish_period_sec,
                        "source_time_triggered_live": True,
                        "model_input_max_source_lag_sec": ParameterValue(
                            vlm_model_input_max_source_lag_sec,
                            value_type=float,
                        ),
                        "request_timeout_sec": ParameterValue(
                            vlm_request_timeout_sec,
                            value_type=float,
                        ),
                        "retry_count": ParameterValue(
                            vlm_retry_count,
                            value_type=int,
                        ),
                        "image_max_side_px": ParameterValue(
                            vlm_image_max_side_px,
                            value_type=int,
                        ),
                        "open_set_phase_bootstrap_observations": ParameterValue(
                            vlm_open_set_phase_bootstrap_observations,
                            value_type=int,
                        ),
                        "max_output_tokens": vlm_max_output_tokens,
                        "generation_seed": ParameterValue(
                            vlm_generation_seed,
                            value_type=int,
                        ),
                        "response_format": vlm_response_format,
                        "reasoning_effort": vlm_reasoning_effort,
                        "task_profile": vlm_task_profile,
                        "response_mode": vlm_response_mode,
                        "replay_response_path": vlm_replay_response_path,
                        "context_mode": "actor_log",
                        "field_image_topic": segmented_flir_image_topic,
                        "raw_field_image_topic": flir_image_topic,
                        "cam4_image_topic": cam4_image_topic,
                        "cam4_overlay_image_topic": cam4_overlay_image_topic,
                        "composite_image_topic": composite_image_topic,
                        "require_cam4_image": False,
                        "multiview_max_skew_sec": ParameterValue(
                            vlm_multiview_max_skew_sec,
                            value_type=float,
                        ),
                        "cam4_dynamic_crop": True,
                        "cam4_crop_x_norm": ParameterValue(
                            cam4_crop_x_norm,
                            value_type=float,
                        ),
                        "cam4_crop_y_norm": ParameterValue(
                            cam4_crop_y_norm,
                            value_type=float,
                        ),
                        "cam4_crop_width_norm": ParameterValue(
                            cam4_crop_width_norm,
                            value_type=float,
                        ),
                        "cam4_crop_height_norm": ParameterValue(
                            cam4_crop_height_norm,
                            value_type=float,
                        ),
                        "perception_bboxes_topic": "",
                        "perception_segmentation_topic": "",
                        "cam4_semantics_topic": cam4_semantics_topic,
                        "perception_image_max_skew_sec": ParameterValue(
                            vlm_perception_image_max_skew_sec,
                            value_type=float,
                        ),
                        "tray_image_topic": tray_image_topic,
                        "synthetic_image_topic": "/shadow/no_synthetic_image",
                        "image_stale_sec": 3.0,
                        "require_field_image": True,
                        "output_prefix": "/vlm",
                        "context_prefix": "/context",
                    }
                ],
                remappings=[
                    ("/vlm/result", replay_vlm_result_output_topic),
                    ("/vlm/health", replay_vlm_health_output_topic),
                ],
                output="screen",
            ),
            Node(
                package="or_digital_twin",
                executable="or_digital_twin",
                name="or_digital_twin",
                parameters=[
                    {
                        **use_sim_time,
                        "spec_dir": spec_dir,
                        "validation_mode": "bt_twin",
                        "vlm_mode": "real",
                        "phase_authority": "reducer",
                        "tool_predict_evidence_confidence_threshold": 0.5,
                        "tool_predict_stability_sec": 3.0,
                        "vlm_implicit_request_confidence_threshold": 0.8,
                        "vlm_implicit_request_stability_sec": 0.7,
                        "vlm_implicit_request_release_sec": 1.5,
                        "accept_validation_actor_events": False,
                        "accept_non_override_structured_requests": False,
                        "allow_shadow_request_capacity_reconciliation": True,
                        "allow_shadow_type_instance_requests": ParameterValue(
                            allow_type_instance_assumption,
                            value_type=bool,
                        ),
                        "allow_open_set_phase_bootstrap": True,
                        "evaluation_observation_topic": ParameterValue(
                            evaluation_observation_topic,
                            value_type=str,
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="bt_orchestrator",
                executable="decision_bridge",
                name="bt_decision_bridge",
                parameters=[
                    {
                        **use_sim_time,
                        "target_node_name": "/tree_executor",
                        "mirror_period_sec": 0.2,
                    }
                ],
                output="screen",
            ),
            Node(
                package="bt_orchestrator",
                executable="bed_robot_arm_group_orchestrator",
                name="bed_robot_arm_group_orchestrator",
                condition=IfCondition(bed_robot_contract_enabled),
                parameters=[
                    {
                        **use_sim_time,
                        "spec_dir": spec_dir,
                        # Replay keeps the same single transcript path, but
                        # does not make live model calls while evaluating a
                        # recorded case.
                        "retractor_voice_normalization_enabled": True,
                        "retractor_voice_interpreter_mode": "deterministic",
                    }
                ],
                output="screen",
            ),
            Node(
                package="surgical_interop_execution",
                executable="fault_action_emulator",
                name="shadow_robot_contract_emulator",
                condition=IfCondition(bed_robot_contract_enabled),
                parameters=[
                    {
                        # The external controller contract must remain observable
                        # while replay source time is paused or held.
                        "use_sim_time": False,
                        "profile_path": PathJoinSubstitution(
                            [
                                FindPackageShare("bringup"),
                                "config",
                                "robot_contract_success.yaml",
                            ]
                        ),
                        "procedure_type": ParameterValue(
                            bed_robot_contract_procedure_type,
                            value_type=str,
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="surgical_interop_execution",
                executable="surgical_interop_execution_bridge",
                name="shadow_surgical_interop_execution_bridge",
                condition=IfCondition(bed_robot_contract_enabled),
                parameters=[
                    {
                        **use_sim_time,
                        "spec_dir": spec_dir,
                        "tool_handover_endpoint": "/surgery/tool_handover",
                        "retraction_service_name": "/surgery/retraction/command",
                        "bed_robot_status_endpoint": (
                            "/external/bed_robot_arms/status"
                        ),
                        "server_wait_timeout_sec": 3.0,
                        "require_bed_robot_status": True,
                    }
                ],
                remappings=[
                    (
                        "/bt/skill_command",
                        "/shadow/no_direct_skill_command",
                    )
                ],
                output="screen",
            ),
            Node(
                package="shadow_evaluation",
                executable="shadow_skill_sink",
                name="shadow_skill_sink",
                parameters=[
                    {
                        **use_sim_time,
                        "counterfactual_success_feedback": ParameterValue(
                            counterfactual_success_feedback,
                            value_type=bool,
                        ),
                        "allow_type_instance_assumption": ParameterValue(
                            allow_type_instance_assumption,
                            value_type=bool,
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="shadow_evaluation",
                executable="shadow_trace_recorder",
                name="shadow_trace_recorder",
                parameters=[
                    {
                        **use_sim_time,
                        "output_path": trace_path,
                        # Launch's YAML parameter normalization can interpret
                        # IDs such as ``0704_6`` as an integer. Preserve the
                        # public run identifier exactly as supplied.
                        "run_id": ParameterValue(run_id, value_type=str),
                        "mode": mode,
                        "field_image_topic": field_image_topic,
                        "flir_image_topic": flir_image_topic,
                        "cam4_image_topic": cam4_image_topic,
                        "composite_image_topic": composite_image_topic,
                        "perception_bboxes_topic": perception_bboxes_topic,
                        "perception_segmentation_topic": (
                            perception_segmentation_topic
                        ),
                        "cam4_semantics_topic": cam4_semantics_topic,
                        "tray_image_topic": tray_image_topic,
                        "source_transcript_topic": source_transcript_topic,
                        "fault_status_topic": "/test/fault/status",
                    }
                ],
                output="screen",
            ),
            Node(
                package="shadow_evaluation",
                executable="reference_reconciler",
                name="reference_reconciler",
                condition=IfCondition(non_strict),
                parameters=[
                    {
                        **use_sim_time,
                        "reference_path": reference_path,
                        "tool_catalog_path": tool_catalog_path,
                        "mode": mode,
                        "post_event_delay_sec": 0.001,
                    }
                ],
                output="screen",
            ),
            Node(
                package="simulation_runtime",
                executable="simulation_manager",
                name="simulation_manager",
                parameters=[
                    {
                        **use_sim_time,
                        "default_bundle": default_bundle,
                        "surgeon_actor_mode": "off",
                        "groot2_port": ParameterValue(
                            groot2_port,
                            value_type=int,
                        ),
                    }
                ],
                output="screen",
            ),
            rosbridge_process,
            rosapi_node,
        ]
    )
