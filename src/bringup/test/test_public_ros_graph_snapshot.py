from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
from typing import Any

from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    IncludeLaunchDescription,
)
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
from launch_ros.utilities import evaluate_parameters
import yaml


ROOT = Path(__file__).resolve().parents[3]
RUNTIME_CORE_LAUNCH = (
    ROOT / "src/bringup/bringup/runtime_core_launch.py"
)
MOCK_LAUNCH = ROOT / "src/bringup/launch/taskplanner_mock.launch.py"
LIVE_LAUNCH = ROOT / "src/bringup/launch/taskplanner_live.launch.py"
SNAPSHOT_PATH = (
    ROOT / "docs/contracts/taskplanner_public_ros_graph.snapshot.yaml"
)
SPEC_ROOT = ROOT / "src/procedure_spec/procedure_spec/specs"
EXECUTION_ROUTE_CONTRACT = ROOT / (
    "src/surgical_interop_execution/surgical_interop_execution/virtual_endpoints.py"
)
EXECUTION_BRIDGE = ROOT / (
    "src/surgical_interop_execution/surgical_interop_execution/bridge.py"
)
INTEGRATION_PREFLIGHT = ROOT / (
    "src/simulation_runtime/simulation_runtime/integration_preflight.py"
)
WEB_ROS_BRIDGE = ROOT / "webapp/src/hooks/useRosBridge.ts"

for source_root in (
    ROOT / "src/procedure_spec",
    ROOT / "src/simulation_runtime",
    ROOT / "src/bringup",
):
    sys.path.insert(0, str(source_root))


def _snapshot() -> dict[str, Any]:
    payload = yaml.safe_load(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _load_launch(path: Path, *, suffix: str):
    spec = importlib.util.spec_from_file_location(
        f"taskplanner_graph_snapshot_{path.stem}_{suffix}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _launch_environment_names(*paths: Path) -> set[str]:
    names: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function_name = ""
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
            if function_name not in {"EnvironmentVariable", "_env"}:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
    return names


def _clear_launch_environment(monkeypatch) -> None:
    for name in _launch_environment_names(
        RUNTIME_CORE_LAUNCH,
        MOCK_LAUNCH,
        LIVE_LAUNCH,
    ):
        monkeypatch.delenv(name, raising=False)


def _node_records(description) -> list[dict[str, Any]]:
    return [
        {
            "package": entity.node_package,
            "executable": entity.node_executable,
            "name": entity._Node__node_name,  # noqa: SLF001 - launch audit
            "conditional": entity.condition is not None,
        }
        for entity in description.entities
        if isinstance(entity, Node)
    ]


def _visit_declaration_defaults(
    description,
    *,
    skip: frozenset[str] = frozenset(),
) -> LaunchContext:
    context = LaunchContext()
    for entity in description.entities:
        if isinstance(entity, DeclareLaunchArgument) and entity.name not in skip:
            entity.visit(context)
    return context


def _mock_default_context(module, description) -> LaunchContext:
    # The source package is deliberately used directly. The validation does
    # not source or require a built workspace install.
    context = _visit_declaration_defaults(
        description,
        skip=frozenset({"spec_dir"}),
    )
    bundle = str(context.launch_configurations["default_bundle"])
    context.launch_configurations["spec_dir"] = str(SPEC_ROOT / bundle)
    for action in module._bed_robot_contract_configuration(context):
        action.visit(context)
    return context


def _parameter_name(key: object) -> str:
    return "".join(part.text for part in key)


def _endpoint_kind(parameter_name: str) -> str:
    # ``retraction`` contains the substring ``action``; classify explicit
    # Service parameters before checking Action names.
    if "service" in parameter_name:
        return "services"
    if parameter_name == "action_name" or "action" in parameter_name:
        return "actions"
    if "tool_handover" in parameter_name:
        return "actions"
    return "topics"


def _empty_endpoint_snapshot() -> dict[str, dict[str, dict[str, str]]]:
    return {"topics": {}, "services": {}, "actions": {}}


def _node_endpoint_bindings(
    description,
    context: LaunchContext,
) -> dict[str, dict[str, dict[str, str]]]:
    result = _empty_endpoint_snapshot()
    for entity in description.entities:
        if not isinstance(entity, Node):
            continue
        node_name = str(entity._Node__node_name)  # noqa: SLF001 - launch audit
        for parameter_group in entity._Node__parameters:  # noqa: SLF001
            if not isinstance(parameter_group, dict):
                continue
            for key, value in parameter_group.items():
                parameter_name = _parameter_name(key)
                if not any(
                    token in parameter_name
                    for token in ("topic", "action", "service", "endpoint")
                ):
                    continue
                evaluated = evaluate_parameters(context, [{key: value}])[0]
                endpoint = evaluated[parameter_name]
                if not isinstance(endpoint, str) or not endpoint.startswith("/"):
                    continue
                kind = _endpoint_kind(parameter_name)
                result[kind].setdefault(node_name, {})[parameter_name] = endpoint
    return result


def _include_endpoint_bindings(
    include: IncludeLaunchDescription,
    context: LaunchContext,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in include.launch_arguments:
        if not any(
            token in name for token in ("topic", "action", "service", "endpoint")
        ):
            continue
        endpoint = (
            value
            if isinstance(value, str)
            else perform_substitutions(context, [value])
        )
        if isinstance(endpoint, str) and endpoint.startswith("/"):
            result[name] = endpoint
    return result


def _imported_ros_types(tree: ast.AST) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.endswith((".msg", ".srv", ".action")):
            continue
        prefix = node.module.replace(".", "/")
        for alias in node.names:
            result[alias.asname or alias.name] = f"{prefix}/{alias.name}"
    return result


def _literal_publishers(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_types = _imported_ros_types(tree)
    result: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "create_publisher"
            or len(node.args) < 2
            or not isinstance(node.args[0], ast.Name)
            or not isinstance(node.args[1], ast.Constant)
            or not isinstance(node.args[1].value, str)
        ):
            continue
        message_type = imported_types.get(node.args[0].id)
        if message_type:
            result[node.args[1].value] = message_type
    return result


def _call_type_names(path: Path, call_name: str, type_index: int) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) <= type_index:
            continue
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name != call_name or not isinstance(node.args[type_index], ast.Name):
            continue
        result.add(node.args[type_index].id)
    return result


def _named_string_constants(path: Path, names: set[str]) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in names:
                result[target.id] = value.value
    return result


def _symbolic_endpoint_calls(path: Path) -> set[tuple[str, str, str]]:
    """Return direction, endpoint-symbol, and ROS type witnesses."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_types = _imported_ros_types(tree)
    directions = {
        "create_publisher": "publisher",
        "create_subscription": "subscription",
        "create_service": "service_server",
        "create_client": "service_client",
    }
    result: set[tuple[str, str, str]] = set()
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr not in directions
            or len(node.args) < 2
            or not isinstance(node.args[0], ast.Name)
            or not isinstance(node.args[1], ast.Name)
        ):
            continue
        message_type = imported_types.get(node.args[0].id)
        if message_type:
            result.add(
                (directions[node.func.attr], node.args[1].id, message_type)
            )
    return result


def _declared_node_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "__init__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            result.add(node.args[0].value)
    return result


def _flatten_snapshot_endpoints(value: object) -> set[str]:
    if isinstance(value, str):
        return {value} if value.startswith("/") else set()
    if isinstance(value, dict):
        result: set[str] = set()
        for child in value.values():
            result.update(_flatten_snapshot_endpoints(child))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for child in value:
            result.update(_flatten_snapshot_endpoints(child))
        return result
    return set()


def test_snapshot_is_validation_only_and_has_no_runtime_reader() -> None:
    snapshot = _snapshot()
    metadata = snapshot["snapshot"]

    assert snapshot["schema_version"] == (
        "taskplanner.public_ros_graph.snapshot.v1"
    )
    assert metadata["kind"] == "validation_snapshot"
    assert metadata["runtime_authority"] is False
    assert metadata["runtime_consumers"] == []
    assert metadata["sources"] == [
        "src/bringup/bringup/runtime_core_launch.py",
        "src/bringup/launch/taskplanner_mock.launch.py",
        "src/bringup/launch/taskplanner_live.launch.py",
        "src/surgical_interop_gateway/surgical_interop_gateway/node.py",
        "src/surgical_interop_gateway/surgical_interop_gateway/camera_alias_relay.py",
        "src/surgical_interop_execution/surgical_interop_execution/virtual_endpoints.py",
        "src/surgical_interop_execution/surgical_interop_execution/bridge.py",
        "src/simulation_runtime/simulation_runtime/integration_preflight.py",
    ]
    runtime_sources = [
        path
        for runtime_root in (ROOT / "src", ROOT / "scripts", ROOT / "docker")
        for path in runtime_root.rglob("*")
        if path.is_file()
        and "test" not in path.parts
        and (
            path.suffix in {".py", ".sh", ".yaml", ".yml"}
            or path.name in {"Dockerfile", "taskplanner"}
        )
    ]
    runtime_sources.append(ROOT / "docker-compose.yml")
    for runtime_source in runtime_sources:
        assert SNAPSHOT_PATH.name not in runtime_source.read_text(encoding="utf-8")


def test_mock_and_live_node_composition_matches_snapshot(monkeypatch) -> None:
    _clear_launch_environment(monkeypatch)
    snapshot = _snapshot()["profiles"]
    mock_module = _load_launch(MOCK_LAUNCH, suffix="mock_nodes")
    live_module = _load_launch(LIVE_LAUNCH, suffix="live_nodes")
    mock_description = mock_module.generate_launch_description()
    live_description = live_module.generate_launch_description()

    assert _node_records(mock_description) == snapshot["mock_base"]["nodes"]
    assert _node_records(live_description) == snapshot["live"]["overlay_nodes"]

    includes = [
        entity
        for entity in live_description.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    assert len(includes) == 1
    assert snapshot["live"]["includes"] == [
        "src/bringup/launch/taskplanner_mock.launch.py"
    ]
    assert "taskplanner_mock.launch.py" in str(
        includes[0].launch_description_source.location
    )

    processes = [
        entity
        for entity in mock_description.entities
        if isinstance(entity, ExecuteProcess) and not isinstance(entity, Node)
    ]
    assert len(processes) == 1
    process = processes[0]
    context = _mock_default_context(mock_module, mock_description)
    command = " ".join(
        perform_substitutions(context, part) for part in process.cmd
    )
    expected_process = snapshot["mock_base"]["ros_processes"]
    assert expected_process == [
        {
            "package": "rosbridge_server",
            "executable": "rosbridge_websocket",
            "conditional": True,
            "respawn": True,
            "respawn_delay_sec": 5.0,
        }
    ]
    assert "ros2 run rosbridge_server rosbridge_websocket" in command
    assert process.condition is not None
    assert process._ExecuteLocal__respawn is True  # noqa: SLF001
    assert process._ExecuteLocal__respawn_delay == 5.0  # noqa: SLF001


def test_mock_default_endpoint_bindings_match_snapshot(monkeypatch) -> None:
    _clear_launch_environment(monkeypatch)
    module = _load_launch(MOCK_LAUNCH, suffix="mock_endpoints")
    description = module.generate_launch_description()
    context = _mock_default_context(module, description)

    assert _node_endpoint_bindings(description, context) == _snapshot()[
        "launch_endpoint_binding_snapshot"
    ]["mock_base"]


def test_live_include_and_overlay_endpoint_bindings_match_snapshot(
    monkeypatch,
) -> None:
    _clear_launch_environment(monkeypatch)
    module = _load_launch(LIVE_LAUNCH, suffix="live_endpoints")
    description = module.generate_launch_description()
    context = _visit_declaration_defaults(description)
    include = next(
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    )
    expected = _snapshot()["launch_endpoint_binding_snapshot"]

    assert _include_endpoint_bindings(include, context) == expected[
        "live_include_arguments"
    ]["topics"]
    assert _node_endpoint_bindings(description, context) == {
        "topics": expected["live_overlay"]["topics"],
        "services": {},
        "actions": {},
    }


def test_public_gateway_topic_names_and_types_match_snapshot() -> None:
    interfaces = _snapshot()["public_interfaces"]["topics"]
    expected_gateway = {
        item["name"]: item["type"]
        for item in interfaces
        if item["owner"] == "surgical_interop_gateway"
    }
    actual_gateway = {
        name: message_type
        for name, message_type in _literal_publishers(
            ROOT / "src/surgical_interop_gateway/surgical_interop_gateway/node.py"
        ).items()
        if name.startswith("/surgery/")
    }

    assert len(expected_gateway) == 11
    assert actual_gateway == expected_gateway
    camera_topics = [
        item
        for item in interfaces
        if item["owner"] == "surgical_camera_alias_relay"
    ]
    assert {item["name"] for item in camera_topics} == {
        "/surgery/images/flir/compressed",
        "/surgery/images/cam4/compressed",
    }
    assert {item["type"] for item in camera_topics} == {
        "sensor_msgs/msg/CompressedImage"
    }
    camera_source = ROOT / (
        "src/surgical_interop_gateway/surgical_interop_gateway/"
        "camera_alias_relay.py"
    )
    assert "CompressedImage" in _call_type_names(
        camera_source, "create_publisher", 0
    )


def test_public_interface_names_have_implemented_graph_witnesses() -> None:
    snapshot = _snapshot()
    route_constant_names = {
        "EXECUTION_ROUTE_STATE_TOPIC",
        "EXECUTION_ROUTE_COMMAND_SERVICE",
        "EXECUTION_ROUTE_PREFLIGHT_ACK_SERVICE",
    }
    route_constants = _named_string_constants(
        EXECUTION_ROUTE_CONTRACT,
        route_constant_names,
    )
    assert set(route_constants) == route_constant_names
    required_route_interfaces = {
        "topics": {
            route_constants["EXECUTION_ROUTE_STATE_TOPIC"]: {
                "type": "std_msgs/msg/String",
                "visibility": "local_route_projection",
                "owner": "surgical_interop_execution_bridge",
            },
        },
        "services": {
            route_constants["EXECUTION_ROUTE_COMMAND_SERVICE"]: {
                "type": "surgical_msgs/srv/IntegrationDebugCommand",
                "visibility": "local_stopped_route_control",
                "owner": "surgical_interop_execution_bridge",
            },
            route_constants["EXECUTION_ROUTE_PREFLIGHT_ACK_SERVICE"]: {
                "type": "surgical_msgs/srv/IntegrationDebugCommand",
                "visibility": "local_route_application_barrier",
                "owner": "integration_preflight",
            },
        },
    }
    for kind, required in required_route_interfaces.items():
        declared = {
            entry["name"]: {
                "type": entry["type"],
                "visibility": entry["visibility"],
                "owner": entry["owner"],
            }
            for entry in snapshot["public_interfaces"][kind]
        }
        assert {name: declared.get(name) for name in required} == required

    bridge_calls = _symbolic_endpoint_calls(EXECUTION_BRIDGE)
    preflight_calls = _symbolic_endpoint_calls(INTEGRATION_PREFLIGHT)
    assert (
        "publisher",
        "EXECUTION_ROUTE_STATE_TOPIC",
        "std_msgs/msg/String",
    ) in bridge_calls
    assert (
        "service_server",
        "EXECUTION_ROUTE_COMMAND_SERVICE",
        "surgical_msgs/srv/IntegrationDebugCommand",
    ) in bridge_calls
    assert (
        "service_client",
        "EXECUTION_ROUTE_PREFLIGHT_ACK_SERVICE",
        "surgical_msgs/srv/IntegrationDebugCommand",
    ) in bridge_calls
    assert (
        "service_server",
        "EXECUTION_ROUTE_PREFLIGHT_ACK_SERVICE",
        "surgical_msgs/srv/IntegrationDebugCommand",
    ) in preflight_calls
    assert "surgical_interop_execution_bridge" in _declared_node_names(
        EXECUTION_BRIDGE
    )
    assert "integration_preflight" in _declared_node_names(INTEGRATION_PREFLIGHT)

    web_source = WEB_ROS_BRIDGE.read_text(encoding="utf-8")
    assert route_constants["EXECUTION_ROUTE_STATE_TOPIC"] in web_source
    assert route_constants["EXECUTION_ROUTE_COMMAND_SERVICE"] in web_source

    witnesses = _flatten_snapshot_endpoints(
        snapshot["launch_endpoint_binding_snapshot"]
    )
    witnesses.update(
        _literal_publishers(
            ROOT / "src/surgical_interop_gateway/surgical_interop_gateway/node.py"
        )
    )
    witnesses.update(route_constants.values())
    for kind in ("topics", "services", "actions"):
        entries = snapshot["public_interfaces"][kind]
        names = [entry["name"] for entry in entries]
        assert len(names) == len(set(names))
        assert all(name.startswith("/") for name in names)
        assert set(names).issubset(witnesses)


def test_action_and_service_types_match_implementations() -> None:
    interfaces = _snapshot()["public_interfaces"]
    assert {item["type"] for item in interfaces["services"]} == {
        "surgical_interop_msgs/srv/ExecuteRetractionCommand",
        "surgical_msgs/srv/IntegrationDebugCommand",
    }
    action_types = {item["name"]: item["type"] for item in interfaces["actions"]}
    assert action_types == {
        "/surgery/tool_handover": (
            "surgical_interop_msgs/action/ExecuteToolHandover"
        ),
        "/integration/virtual/surgery/tool_handover": (
            "surgical_interop_msgs/action/ExecuteToolHandover"
        ),
        "/skill/execute": "surgical_msgs/action/ExecuteSkill",
    }

    execution_source = ROOT / (
        "src/surgical_interop_execution/surgical_interop_execution/bridge.py"
    )
    emulator_source = ROOT / (
        "src/surgical_interop_execution/surgical_interop_execution/"
        "fault_action_emulator.py"
    )
    mock_skill_source = ROOT / "src/skill_execution/skill_execution/mock_server.py"
    assert "ExecuteToolHandover" in _call_type_names(
        execution_source, "ActionClient", 1
    )
    assert "ExecuteToolHandover" in _call_type_names(
        emulator_source, "ActionServer", 1
    )
    assert "ExecuteRetractionCommand" in _call_type_names(
        execution_source, "create_client", 0
    )
    assert "ExecuteRetractionCommand" in _call_type_names(
        emulator_source, "create_service", 0
    )
    assert "ExecuteSkill" in _call_type_names(
        mock_skill_source, "ActionServer", 1
    )
