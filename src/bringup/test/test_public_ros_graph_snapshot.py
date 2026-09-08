from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
from typing import Any

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
EXECUTION_ROUTE_CONTRACT = ROOT / (
    "src/surgical_interop_execution/surgical_interop_execution/virtual_endpoints.py"
)
EXECUTION_BRIDGE = ROOT / (
    "src/surgical_interop_execution/surgical_interop_execution/bridge.py"
)
INTEGRATION_PREFLIGHT = ROOT / (
    "src/simulation_runtime/simulation_runtime/integration_preflight.py"
)
VOICE_COMMAND_NODE = ROOT / (
    "src/voice_command/voice_command/node.py"
)
COMMAND_ROUTER_NODE = ROOT / (
    "src/voice_command/voice_command/command_router.py"
)
SPEECH_INPUT_ADAPTER = ROOT / (
    "src/simulation_runtime/simulation_runtime/speech_input_adapter.py"
)
GATEWAY_NODE = ROOT / (
    "src/surgical_interop_gateway/surgical_interop_gateway/node.py"
)
CAMERA_ALIAS_RELAY = ROOT / (
    "src/surgical_interop_gateway/surgical_interop_gateway/"
    "camera_alias_relay.py"
)
EXECUTION_EMULATOR = ROOT / (
    "src/surgical_interop_execution/surgical_interop_execution/"
    "fault_action_emulator.py"
)
REAL_VLM_NODE = ROOT / "src/vlm_node/vlm_node/real_vlm.py"
OPERATIONAL_ASR_NODE = ROOT / (
    "src/integration_debug/integration_debug/operational_asr_node.py"
)
PNU_PERCEPTION_BRIDGE = ROOT / "src/vlm_node/vlm_node/pnu_perception_bridge.py"

OWNER_WITNESS_SOURCES: dict[str, tuple[Path, ...]] = {
    "surgical_interop_gateway": (GATEWAY_NODE,),
    "surgical_camera_alias_relay": (CAMERA_ALIAS_RELAY,),
    "taskplanner_asr": (OPERATIONAL_ASR_NODE, LIVE_LAUNCH),
    "external_camera_runtime": (LIVE_LAUNCH, CAMERA_ALIAS_RELAY),
    "external_rfdetr_1_7": (LIVE_LAUNCH, PNU_PERCEPTION_BRIDGE),
    "external_controller": (EXECUTION_BRIDGE, EXECUTION_EMULATOR),
    "integration_preflight": (
        INTEGRATION_PREFLIGHT,
        EXECUTION_ROUTE_CONTRACT,
    ),
    "surgical_interop_execution_bridge": (
        EXECUTION_BRIDGE,
        EXECUTION_ROUTE_CONTRACT,
    ),
    # The VLM derives this public endpoint from its context prefix; the
    # preflight subscriber is the local literal-name witness.
    "real_vlm_node": (REAL_VLM_NODE, INTEGRATION_PREFLIGHT),
    # The resolver receives its output topic through launch parameters, while
    # the node owns the generated message type.
    "voice_command_resolver": (VOICE_COMMAND_NODE, MOCK_LAUNCH),
    "speech_input_adapter": (SPEECH_INPUT_ADAPTER, MOCK_LAUNCH),
    "command_router": (COMMAND_ROUTER_NODE, MOCK_LAUNCH),
    # Virtual endpoints are named in the shared endpoint contract and served
    # by the emulator implementation.
    "virtual_robot_contract_emulator": (
        EXECUTION_EMULATOR,
        EXECUTION_ROUTE_CONTRACT,
    ),
}

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


def _source_text(paths: tuple[Path, ...]) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _interface_symbol(interface_type: object) -> str:
    value = str(interface_type or "")
    parts = value.split("/")
    assert len(parts) == 3 and parts[1] in {"msg", "srv", "action"}
    assert parts[-1]
    return parts[-1]


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


def test_snapshot_is_validation_only_and_has_no_runtime_reader() -> None:
    snapshot = _snapshot()
    metadata = snapshot["snapshot"]

    assert snapshot["schema_version"] == (
        "taskplanner.public_ros_interface_reference.v1"
    )
    assert metadata["kind"] == "advisory_interface_reference"
    assert metadata["runtime_authority"] is False
    assert metadata["runtime_consumers"] == []
    assert metadata["validation_tier"] == "diagnostic_smoke"
    assert metadata["topology_contract"] == "none"
    assert "not a release" in metadata["purpose"].lower()
    assert metadata["sources"]
    assert all(
        isinstance(path, str) and path.startswith("src/")
        for path in metadata["sources"]
    )
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


def test_launch_files_load_as_a_non_authoritative_smoke(monkeypatch) -> None:
    _clear_launch_environment(monkeypatch)
    mock_module = _load_launch(MOCK_LAUNCH, suffix="mock_smoke")
    live_module = _load_launch(LIVE_LAUNCH, suffix="live_smoke")
    # This catches malformed launch modules but intentionally does not freeze
    # the optional node list, internal parameter names, or endpoint wiring.
    assert mock_module.generate_launch_description().entities
    assert live_module.generate_launch_description().entities


def test_voice_router_declares_admitted_observed_and_private_proposal_lanes() -> None:
    snapshot = _snapshot()
    voice_interfaces = [
        entry
        for entry in snapshot["public_interfaces"]["topics"]
        if entry["name"]
        in {
            "/surgery/audio/admitted_utterance",
            "/surgery/audio/observed_utterance",
            "/surgery/voice/proposal",
        }
    ]
    assert voice_interfaces == [
        {
            "name": "/surgery/audio/admitted_utterance",
            "type": "surgical_msgs/msg/SpeechUtterance",
            "visibility": "local_command_ingress",
            "owner": "speech_input_adapter",
        },
        {
            "name": "/surgery/audio/observed_utterance",
            "type": "surgical_msgs/msg/SpeechUtterance",
            "visibility": "local_read_only_observation",
            "owner": "command_router",
        },
        {
            "name": "/surgery/voice/proposal",
            "type": "surgical_msgs/msg/VoiceCommandIntent",
            "visibility": "local_private_proposal",
            "owner": "voice_command_resolver",
        },
    ]
    publisher_types = _call_type_names(
        VOICE_COMMAND_NODE,
        "create_publisher",
        0,
    )
    assert publisher_types == {"VoiceCommandIntent"}
    snapshot_text = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert "/surgery/voice/intent" not in snapshot_text
    assert "/surgery/audio/request_text" not in snapshot_text
    assert "/surgery/voice/function_proposal" not in snapshot_text
    router_source = COMMAND_ROUTER_NODE.read_text(encoding="utf-8")
    assert "/surgery/audio/admitted_utterance" in router_source
    assert "/surgery/audio/observed_utterance" in router_source
    assert "/surgery/voice/resolver_utterance" in router_source
    assert "/surgery/voice/proposal" in router_source


def test_integration_readiness_is_declared_as_diagnostic_observation() -> None:
    snapshot = _snapshot()
    readiness = [
        entry
        for entry in snapshot["public_interfaces"]["topics"]
        if entry["name"] == "/integration/readiness"
    ]
    assert readiness == [
        {
            "name": "/integration/readiness",
            "type": "std_msgs/msg/String",
            "visibility": "local_diagnostic_observation",
            "owner": "integration_preflight",
        }
    ]


def test_declared_gateway_topics_have_publishers() -> None:
    interfaces = _snapshot()["public_interfaces"]["topics"]
    expected_gateway = {
        item["name"]: item["type"]
        for item in interfaces
        if item["owner"] == "surgical_interop_gateway"
    }
    actual_gateway = {
        name: message_type
        for name, message_type in _literal_publishers(GATEWAY_NODE).items()
        if name.startswith("/surgery/")
    }

    assert expected_gateway
    assert expected_gateway.items() <= actual_gateway.items()
    camera_topics = [
        item
        for item in interfaces
        if item["owner"] == "surgical_camera_alias_relay"
    ]
    assert camera_topics
    assert all(
        item["type"] == "sensor_msgs/msg/CompressedImage"
        for item in camera_topics
    )
    assert "CompressedImage" in _call_type_names(
        CAMERA_ALIAS_RELAY, "create_publisher", 0
    )
    camera_source_text = CAMERA_ALIAS_RELAY.read_text(encoding="utf-8")
    assert all(item["name"] in camera_source_text for item in camera_topics)


def test_public_interface_declarations_have_owner_witnesses() -> None:
    """Keep declared public surfaces honest without freezing topology.

    The interface reference is intentionally a subset of the live ROS graph.
    A researcher can add an internal node, command catalog entry, topic, or
    launch parameter without editing this document.  A declaration that *is*
    present must still point at local source that names both its endpoint and
    interface type.
    """

    interfaces = _snapshot()["public_interfaces"]
    for kind, expected_segment in (
        ("topics", "msg"),
        ("services", "srv"),
        ("actions", "action"),
    ):
        entries = interfaces[kind]
        assert isinstance(entries, list) and entries
        names = [str(entry.get("name", "")) for entry in entries]
        assert len(names) == len(set(names))
        for entry in entries:
            assert isinstance(entry, dict)
            name = str(entry.get("name", ""))
            interface_type = str(entry.get("type", ""))
            owner = str(entry.get("owner", ""))
            assert name.startswith("/")
            assert f"/{expected_segment}/" in interface_type
            sources = OWNER_WITNESS_SOURCES.get(owner)
            assert sources is not None, f"no witness source configured for {owner!r}"
            source_text = _source_text(sources)
            assert name in source_text, (
                f"{kind} {name} has no endpoint witness for owner {owner}"
            )
            assert _interface_symbol(interface_type) in source_text, (
                f"{kind} {name} has no type witness for owner {owner}"
            )


def test_declared_action_and_service_types_have_ros_call_witnesses() -> None:
    interfaces = _snapshot()["public_interfaces"]
    service_type_symbols = set()
    action_type_symbols = set()
    for path in (EXECUTION_BRIDGE, EXECUTION_EMULATOR, INTEGRATION_PREFLIGHT):
        service_type_symbols.update(_call_type_names(path, "create_client", 0))
        service_type_symbols.update(_call_type_names(path, "create_service", 0))
    for path in (EXECUTION_BRIDGE, EXECUTION_EMULATOR):
        action_type_symbols.update(_call_type_names(path, "ActionClient", 1))
        action_type_symbols.update(_call_type_names(path, "ActionServer", 1))

    assert all(
        _interface_symbol(entry["type"]) in service_type_symbols
        for entry in interfaces["services"]
    )
    assert all(
        _interface_symbol(entry["type"]) in action_type_symbols
        for entry in interfaces["actions"]
    )
