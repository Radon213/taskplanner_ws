import importlib.util
import json
from pathlib import Path
import sys

_SRC_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SRC_ROOT / "simulation_runtime"))
sys.path.insert(0, str(_SRC_ROOT / "bringup"))

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare
from launch_ros.utilities import evaluate_parameters
import pytest
import yaml


def _load_shadow_launch_module():
    launch_path = (
        Path(__file__).resolve().parents[1]
        / "launch"
        / "taskplanner_shadow.launch.py"
    )
    spec = importlib.util.spec_from_file_location(
        "taskplanner_shadow_launch",
        launch_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shadow_launch_exposes_strict_replay_controls():
    module = _load_shadow_launch_module()
    description = module.generate_launch_description()
    arguments = {
        action.name: action.default_value
        for action in description.entities
        if isinstance(action, DeclareLaunchArgument)
    }

    assert "mode" in arguments
    assert "counterfactual_success_feedback" in arguments
    assert "allow_type_instance_assumption" in arguments
    assert "vlm_response_mode" in arguments
    assert "reference_path" in arguments
    assert "source_cam4_topic" in arguments
    assert "require_vlm" in arguments
    assert arguments["require_vlm"][0].text == "false"
    assert "rfdetr_preflight_timeout_sec" in arguments
    assert "perception_backend" in arguments
    assert "perception_provider" in arguments
    assert "perception_location" in arguments
    assert "perception_endpoint" in arguments
    assert "pnu_allow_insecure_remote_http" in arguments
    assert "pnu_depth_scale_m_per_unit" in arguments
    assert "pnu_depth_scale_validated" in arguments
    assert "pnu_expected_tool_support_plane_config_version" in arguments
    assert "trace_root" in arguments
    assert "fault_scenario_path" in arguments
    assert "publish_shared_state" in arguments
    assert "publish_shared_free_text" in arguments
    assert arguments["publish_shared_state"][0].text == "true"
    assert arguments["publish_shared_free_text"][0].text == "false"
    assert arguments["source_cam4_topic"][0].text == (
        "/surgery/cam4/color/image/compressed"
    )

    bridges = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "rfdetr_perception_bridge"
    ]
    assert len(bridges) == 1
    assert bridges[0].condition is not None
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "perception_provider": "builtin_rfdetr",
            "enable_rfdetr_perception": "true",
        }
    )
    assert bridges[0].condition.evaluate(context) is True
    pnu_bridges = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "pnu_perception_bridge"
    ]
    assert len(pnu_bridges) == 1
    assert pnu_bridges[0].condition is not None
    assert pnu_bridges[0].condition.evaluate(context) is False
    context.launch_configurations["perception_provider"] = "pnu_hand_blood"
    assert bridges[0].condition.evaluate(context) is False
    assert pnu_bridges[0].condition.evaluate(context) is True
    pnu_parameters = {
        "".join(part.text for part in key): value
        for key, value in pnu_bridges[0]._Node__parameters[0].items()
    }
    assert {
        "service_url",
        "rgb_input_topic",
        "color_camera_info_topic",
        "depth_input_topic",
        "depth_camera_info_topic",
        "cam4_semantics_topic",
        "cam4_mayo_observation_topic",
        "diagnostics_topic",
        "health_topic",
        "requested_algorithms",
        "expected_model_digests_json",
        "expected_tool_support_plane_config_version",
        "api_token_file",
        "allow_insecure_remote_http",
        "allow_unauthenticated_remote",
        "depth_scale_m_per_unit",
        "depth_scale_validated",
    }.issubset(pnu_parameters)
    context.launch_configurations["perception_provider"] = "disabled"
    assert bridges[0].condition.evaluate(context) is False
    assert pnu_bridges[0].condition.evaluate(context) is False

    cv_monitor = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "cv_contract_monitor"
    )
    monitor_parameters = {
        "".join(part.text for part in key): value
        for key, value in cv_monitor._Node__parameters[0].items()
    }
    assert {
        "perception_backend",
        "perception_provider",
        "perception_location",
        "perception_endpoint",
        "cam4_rgb_topic",
        "cam4_camera_info_topic",
        "cam4_native_depth_compressed_topic",
        "cam4_depth_camera_info_topic",
        "cam4_depth_to_color_extrinsics_topic",
        "cam4_aligned_depth_compressed_topic",
        "cam4_aligned_depth_camera_info_topic",
    }.issubset(monitor_parameters)

    fault_injectors = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "fault_injector"
    ]
    assert len(fault_injectors) == 1
    assert fault_injectors[0].condition is not None

    rosapi_nodes = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_package == "rosapi"
        and entity.node_executable == "rosapi_node"
    ]
    assert len(rosapi_nodes) == 1
    assert rosapi_nodes[0].condition is not None

    trace_recorders = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "shadow_trace_recorder"
    ]
    assert len(trace_recorders) == 1
    trace_parameters = trace_recorders[0]._Node__parameters[0]
    run_id_parameter = next(
        value
        for key, value in trace_parameters.items()
        if "".join(part.text for part in key) == "run_id"
    )
    assert run_id_parameter._ParameterValue__value_type is str

    contract_nodes = {
        entity.node_executable
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable
        in {
            "fault_action_emulator",
            "surgical_interop_execution_bridge",
        }
    }
    assert contract_nodes == {
        "fault_action_emulator",
        "surgical_interop_execution_bridge",
    }

    public_gateway = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "surgical_interop_gateway"
    )
    assert public_gateway.condition is not None
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "default_bundle": "thyroidectomy_demo",
            "publish_shared_state": "true",
            "publish_shared_free_text": "false",
        }
    )
    public_parameters = evaluate_parameters(
        context,
        public_gateway._Node__parameters,
    )[0]
    assert public_parameters == {
        "default_bundle": "thyroidectomy_demo",
        "publish_free_text": False,
    }

    voice_resolver = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_package == "voice_command"
        and entity.node_executable == "voice_intent_resolver"
    )
    voice_parameters = {
        "".join(part.text for part in key): value
        for key, value in voice_resolver._Node__parameters[0].items()
    }
    context.launch_configurations["spec_dir"] = "/tmp/test-procedure-bundle"
    assert perform_substitutions(
        context, voice_parameters["procedure_bundle"]
    ) == "/tmp/test-procedure-bundle"
    assert perform_substitutions(
        context, voice_parameters["selector_mode"]
    ).startswith("deterministic")


def test_shadow_spec_dir_follows_default_bundle(monkeypatch):
    module = _load_shadow_launch_module()
    description = module.generate_launch_description()
    arguments = {
        action.name: action.default_value
        for action in description.entities
        if isinstance(action, DeclareLaunchArgument)
    }
    context = LaunchContext()
    context.launch_configurations["default_bundle"] = "thyroidectomy_demo"
    monkeypatch.setattr(
        FindPackageShare,
        "find",
        lambda _self, package_name: f"/share/{package_name}",
    )

    spec_dir = perform_substitutions(context, arguments["spec_dir"])

    assert spec_dir.endswith(
        "/procedure_spec/specs/thyroidectomy_demo"
    )


@pytest.mark.parametrize(
    ("bundle_id", "enabled", "procedure_type"),
    [
        ("thyroidectomy", "true", "thyroidectomy"),
        ("thyroidectomy_demo", "true", "thyroidectomy"),
        ("nephrectomy", "true", "nephrectomy"),
        ("inguinal_hernia_repair", "false", ""),
    ],
)
def test_shadow_bed_robot_contract_bundle_mapping(
    bundle_id,
    enabled,
    procedure_type,
):
    module = _load_shadow_launch_module()
    context = LaunchContext()
    context.launch_configurations["default_bundle"] = bundle_id

    for action in module._bed_robot_contract_configuration(context):
        action.visit(context)

    assert context.launch_configurations["bed_robot_contract_enabled"] == enabled
    assert (
        context.launch_configurations["bed_robot_contract_procedure_type"]
        == procedure_type
    )


def test_shadow_contract_nodes_are_disabled_for_inguinal_bundle():
    module = _load_shadow_launch_module()
    description = module.generate_launch_description()
    nodes = {
        entity.node_executable: entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable
        in {
            "fault_action_emulator",
            "surgical_interop_execution_bridge",
            "bed_robot_arm_group_orchestrator",
        }
    }
    context = LaunchContext()
    context.launch_configurations["default_bundle"] = "inguinal_hernia_repair"
    for action in module._bed_robot_contract_configuration(context):
        action.visit(context)

    assert nodes.keys() == {
        "fault_action_emulator",
        "surgical_interop_execution_bridge",
        "bed_robot_arm_group_orchestrator",
    }
    assert all(
        node.condition is not None and not node.condition.evaluate(context)
        for node in nodes.values()
    )


def _valid_routes() -> dict[str, str]:
    return {
        "source_field_image_topic": "/source/cam4",
        "source_cam1_topic": "/source/cam1",
        "source_cam2_topic": "/source/cam2",
        "source_cam3_topic": "/source/cam3",
        "source_cam4_topic": "/source/cam4",
        "source_flir_topic": "/source/flir",
        "source_bbox_topic": "/source/cam4/bboxes",
        "source_segmentation_topic": "/source/cam4/segments",
        "source_transcript_topic": "/source/transcript",
        "field_image_topic": "/normalized/field",
        "flir_image_topic": "/normalized/flir",
        "cam4_image_topic": "/normalized/cam4",
        "segmented_flir_image_topic": "/normalized/flir/segmented",
        "cam4_overlay_image_topic": "/normalized/cam4/overlay",
        "cam4_semantics_topic": "/normalized/cam4/semantics",
    }


def test_shadow_route_validation_accepts_explicit_unique_routes():
    module = _load_shadow_launch_module()
    module.validate_shadow_routes(_valid_routes())


@pytest.mark.parametrize(
    ("changed_name", "changed_value", "match"),
    [
        ("source_cam4_topic", "", "must be declared"),
        ("source_cam3_topic", "/source/cam2", "must be unique"),
        (
            "source_field_image_topic",
            "/source/other",
            "must match source_cam4_topic",
        ),
        (
            "segmented_flir_image_topic",
            "/normalized/flir",
            "normalized output routes must be unique",
        ),
        (
            "source_bbox_topic",
            "/source/cam4",
            "must not overlap a source camera route",
        ),
        (
            "field_image_topic",
            "/source/cam1",
            "normalized output routes must not overlap source inputs",
        ),
    ],
)
def test_shadow_route_validation_rejects_ambiguous_routes(
    changed_name,
    changed_value,
    match,
):
    module = _load_shadow_launch_module()
    routes = _valid_routes()
    routes[changed_name] = changed_value
    with pytest.raises(ValueError, match=match):
        module.validate_shadow_routes(routes)


def _write_metadata(path: Path, topics: dict[str, str]) -> None:
    path.mkdir(parents=True)
    rows = [
        {
            "topic_metadata": {
                "name": name,
                "type": message_type,
            },
            "message_count": 1,
        }
        for name, message_type in topics.items()
    ]
    (path / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "rosbag2_bagfile_information": {
                    "topics_with_message_count": rows,
                }
            }
        ),
        encoding="utf-8",
    )


def test_bag_route_preflight_requires_cam4_and_flir(tmp_path):
    module = _load_shadow_launch_module()
    routes = _valid_routes()
    bag = tmp_path / "bag"
    _write_metadata(
        bag,
        {
            routes["source_cam4_topic"]: module.IMAGE_MESSAGE_TYPE,
            routes["source_transcript_topic"]: "std_msgs/msg/String",
        },
    )

    errors, warnings = module.inspect_bag_routes(bag, routes)

    assert any("source_flir_topic" in error for error in errors)
    assert any("source_cam1_topic" in warning for warning in warnings)


def test_bag_route_preflight_checks_image_message_type(tmp_path):
    module = _load_shadow_launch_module()
    routes = _valid_routes()
    bag = tmp_path / "bag"
    _write_metadata(
        bag,
        {
            routes["source_cam4_topic"]: "std_msgs/msg/String",
            routes["source_flir_topic"]: module.IMAGE_MESSAGE_TYPE,
        },
    )

    errors, _ = module.inspect_bag_routes(bag, routes)

    assert errors == [
        "source_cam4_topic '/source/cam4' has type "
        "'std_msgs/msg/String'; expected 'sensor_msgs/msg/CompressedImage'"
    ]


class _HealthResponse:
    status = 200

    def __init__(self, payload):
        self._payload = payload
        self.closed = False

    def read(self):
        return json.dumps(self._payload).encode()

    def close(self):
        self.closed = True


def test_rfdetr_health_preflight_requires_both_models():
    module = _load_shadow_launch_module()
    requests = []

    def opener(url, timeout):
        requests.append((url, timeout))
        return _HealthResponse(
            {
                "status": "ready",
                "models": {
                    "flir": "RFDETRSegSmall",
                    "cam4": "RFDETRSmall",
                },
            }
        )

    payload = module.fetch_rfdetr_health(
        "http://host.docker.internal:8010/",
        1.5,
        opener=opener,
    )

    assert payload["status"] == "ready"
    assert requests == [("http://host.docker.internal:8010/health", 1.5)]


def test_rfdetr_health_preflight_rejects_partial_readiness():
    module = _load_shadow_launch_module()

    def opener(_url, timeout):
        assert timeout == 1.0
        return _HealthResponse(
            {
                "status": "ready",
                "models": {"flir": "RFDETRSegSmall"},
            }
        )

    with pytest.raises(RuntimeError, match="missing the FLIR or CAM4"):
        module.fetch_rfdetr_health(
            "http://127.0.0.1:8010",
            1.0,
            opener=opener,
        )


def test_trace_path_uses_run_id_and_avoids_existing_trace(tmp_path):
    module = _load_shadow_launch_module()
    first = module.select_trace_path(tmp_path, "case-0704_6-run")

    second = module.select_trace_path(tmp_path, "case-0704_6-run")

    assert first == (
        tmp_path / "case-0704_6-run" / "shadow_trace.v1.jsonl"
    )
    assert second == (
        tmp_path / "case-0704_6-run" / "shadow_trace.v1.001.jsonl"
    )
    reservation = first.with_name(f"{first.name}.reserved")
    assert json.loads(reservation.read_text(encoding="utf-8"))["run_id"] == (
        "case-0704_6-run"
    )


def test_explicit_trace_path_never_overwrites(tmp_path):
    module = _load_shadow_launch_module()
    trace_path = tmp_path / "explicit.jsonl"
    trace_path.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        module.select_trace_path(tmp_path, "run-001", str(trace_path))


def test_ros_executable_lookup_uses_sourced_prefix(tmp_path):
    module = _load_shadow_launch_module()
    executable = (
        tmp_path / "lib" / "vlm_node" / "rfdetr_perception_bridge"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert module.find_ros_executable(
        "vlm_node",
        "rfdetr_perception_bridge",
        prefixes=[str(tmp_path)],
    ) == executable


def test_build_marker_environment_overrides_image_file(tmp_path):
    module = _load_shadow_launch_module()
    marker_path = tmp_path / "taskplanner-build.json"
    marker_path.write_text(
        json.dumps(
            {
                "image_version": "old",
                "git_sha": "old",
                "shadow_contract": "old",
            }
        ),
        encoding="utf-8",
    )

    marker = module.read_build_marker(
        marker_path=marker_path,
        environment={
            "TASKPLANNER_IMAGE_VERSION": "0.1.0-dev",
            "TASKPLANNER_IMAGE_GIT_SHA": "abc123",
            "TASKPLANNER_SHADOW_CONTRACT_VERSION": (
                module.SHADOW_CONTRACT_VERSION
            ),
        },
    )

    assert marker == {
        "image_version": "0.1.0-dev",
        "git_sha": "abc123",
        "shadow_contract": module.SHADOW_CONTRACT_VERSION,
    }


def _preflight_context(tmp_path: Path, *, require_vlm: bool) -> LaunchContext:
    context = LaunchContext()
    values = {
        **_valid_routes(),
        "require_vlm": str(require_vlm).lower(),
        "interactive_replay": "false",
        "run_id": "preflight-run",
        "trace_root": str(tmp_path),
        "trace_path": "",
        "rfdetr_service_url": "http://127.0.0.1:8010",
        "perception_endpoint": "http://127.0.0.1:8010",
        "perception_provider": "builtin_rfdetr",
        "perception_location": "local",
        "perception_backend": "local",
        "rfdetr_preflight_timeout_sec": "1.0",
        "enable_rfdetr_perception": "true",
    }
    context.launch_configurations.update(values)
    return context


def test_required_vlm_preflight_fails_before_nodes_start(tmp_path, monkeypatch):
    module = _load_shadow_launch_module()
    monkeypatch.setattr(
        module,
        "find_ros_executable",
        lambda *_args, **_kwargs: Path("/mock/rfdetr_perception_bridge"),
    )

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(module, "fetch_rfdetr_health", unavailable)
    monkeypatch.setattr(
        module,
        "read_build_marker",
        lambda: {"shadow_contract": module.SHADOW_CONTRACT_VERSION},
    )

    with pytest.raises(
        RuntimeError,
        match=r"(?s)require_vlm=true.*connection refused",
    ):
        module._shadow_preflight(
            _preflight_context(tmp_path, require_vlm=True)
        )


def test_optional_vlm_preflight_reports_degraded_and_continues(
    tmp_path,
    monkeypatch,
):
    module = _load_shadow_launch_module()
    monkeypatch.setattr(
        module,
        "find_ros_executable",
        lambda *_args, **_kwargs: Path("/mock/rfdetr_perception_bridge"),
    )

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(module, "fetch_rfdetr_health", unavailable)
    monkeypatch.setattr(
        module,
        "read_build_marker",
        lambda: {"shadow_contract": module.SHADOW_CONTRACT_VERSION},
    )

    actions = module._shadow_preflight(
        _preflight_context(tmp_path, require_vlm=False)
    )
    messages = [
        "".join(
            part.text if hasattr(part, "text") else str(part)
            for part in action.msg
        )
        for action in actions
        if action.__class__.__name__ == "LogInfo"
    ]

    assert any("[DEGRADED]" in message for message in messages)
    assert any("connection refused" in message for message in messages)


def test_disabled_shadow_perception_skips_bridge_and_worker_preflight(
    tmp_path,
    monkeypatch,
):
    module = _load_shadow_launch_module()

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("disabled perception must not probe RF-DETR")

    monkeypatch.setattr(module, "find_ros_executable", unexpected_call)
    monkeypatch.setattr(module, "fetch_rfdetr_health", unexpected_call)
    monkeypatch.setattr(
        module,
        "read_build_marker",
        lambda: {"shadow_contract": module.SHADOW_CONTRACT_VERSION},
    )
    context = _preflight_context(tmp_path, require_vlm=True)
    context.launch_configurations.update(
        {
            "perception_provider": "disabled",
            "perception_location": "local",
            "perception_endpoint": "",
            "perception_backend": "disabled",
        }
    )

    actions = module._shadow_preflight(context)
    messages = [
        "".join(
            part.text if hasattr(part, "text") else str(part)
            for part in action.msg
        )
        for action in actions
        if action.__class__.__name__ == "LogInfo"
    ]
    assert any("provider=disabled" in message for message in messages)
    assert any("bridge=disabled" in message for message in messages)
