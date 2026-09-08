from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from types import ModuleType

import pytest

from procedure_spec import compute_bundle_config_revision, scenario_config_payload

# This focused unit test does not instantiate a ROS subscription, but the
# source router imports SpeechUtterance at module load.  The checked-in local
# install can be built for a different Python minor version, so supply only the
# marker type when generated messages are unavailable to this interpreter.
try:
    from surgical_msgs.msg import SpeechUtterance as _SpeechUtterance
except ImportError:
    _surgical_msgs = sys.modules.get("surgical_msgs")
    if _surgical_msgs is None:
        _surgical_msgs = ModuleType("surgical_msgs")
        sys.modules["surgical_msgs"] = _surgical_msgs
    _surgical_msgs_msg = ModuleType("surgical_msgs.msg")
    _surgical_msgs_msg.SpeechUtterance = type("SpeechUtterance", (), {})
    _surgical_msgs_msg.VoiceCommandIntent = type("VoiceCommandIntent", (), {})
    sys.modules["surgical_msgs.msg"] = _surgical_msgs_msg
    _surgical_msgs.msg = _surgical_msgs_msg
    _surgical_msgs_srv = ModuleType("surgical_msgs.srv")
    _surgical_msgs_srv.ControlSimulation = type("ControlSimulation", (), {})
    _surgical_msgs_srv.InjectSurgeonOverride = type(
        "InjectSurgeonOverride", (), {}
    )
    sys.modules["surgical_msgs.srv"] = _surgical_msgs_srv
    _surgical_msgs.srv = _surgical_msgs_srv

from voice_command.command_catalog import (
    CATALOG_SCHEMA,
    CatalogCommand,
    CommandCatalogError,
    CommandCatalogReloader,
    CommandRouter,
    EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT,
    EXECUTION_TOOL_HANDOVER_PROXY_ENDPOINT,
    RETRACTION_SERVICE_TYPE,
    RoutedCommand,
    deterministic_command_id,
    default_command_catalog_path,
    load_command_catalog,
)
from voice_command.command_router import (
    CommandRouterNode,
    default_start_phase_id_for_bundle,
)


class _DeferredFuture:
    """Small ROS-future stand-in that makes Action callbacks deterministic."""

    def __init__(self) -> None:
        self._callbacks = []
        self._value = None
        self._error: Exception | None = None
        self._done = False

    def add_done_callback(self, callback) -> None:
        self._callbacks.append(callback)
        if self._done:
            callback(self)

    def resolve(self, value) -> None:
        self._value = value
        self._done = True
        for callback in tuple(self._callbacks):
            callback(self)

    def result(self):
        if self._error is not None:
            raise self._error
        return self._value


class _ActionLogger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def info(self, message: str, *_args, **_kwargs) -> None:
        self.entries.append(("info", message))

    def warning(self, message: str, *_args, **_kwargs) -> None:
        self.entries.append(("warning", message))

    def error(self, message: str, *_args, **_kwargs) -> None:
        self.entries.append(("error", message))


def _action_routed(tmp_path: Path, *, timeout_sec: float = 0.25):
    path = tmp_path / "action_commands.yaml"
    path.write_text(
        f"""schema: {CATALOG_SCHEMA}
commands:
  - name: move_scope
    phrases: [move scope]
    command_id_prefix: move-scope
    dispatch:
      kind: action
      type: lab_interfaces/action/MoveScope
      endpoint: /lab/move_scope
      timeout_sec: {timeout_sec}
      payload: {{target: mayo}}
""",
        encoding="utf-8",
    )
    routed = CommandRouter(
        load_command_catalog(path), command_id_factory=lambda prefix: f"{prefix}-1"
    ).route("move scope")
    assert routed is not None
    return routed


def _router_node() -> tuple[CommandRouterNode, list[object]]:
    """Construct only the state needed for router transport unit tests."""

    reloader = CommandCatalogReloader(default_command_catalog_path())
    changed, error = reloader.reload_if_changed(force=True)
    assert changed is True, error
    node = CommandRouterNode.__new__(CommandRouterNode)
    node._catalog_reloader = reloader
    node._router = None
    node.get_logger = lambda: _ActionLogger()
    dispatched: list[object] = []
    node._dispatch = dispatched.append
    node._set_catalog(reloader)
    return node, dispatched


@pytest.mark.parametrize(
    "utterance",
    [
        "  SUCTION ",
        "suction 시작",
        "suction 시작해줘",
        "suction 들어와",
        "suction 들어와줘",
        "suction 켜줘",
        "suction start",
    ],
)
def test_default_catalog_routes_suction_to_fixed_execution_proxy(
    utterance: str,
) -> None:
    catalog = load_command_catalog(default_command_catalog_path())
    router = CommandRouter(
        catalog,
        command_id_factory=lambda prefix: f"{prefix}-20260827-test",
    )

    routed = router.route(utterance)

    assert routed is not None
    assert routed.command.name == "suction"
    assert routed.command.kind == "service"
    assert routed.command.interface_type == RETRACTION_SERVICE_TYPE
    assert routed.endpoint == EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT
    assert dict(routed.payload) == {
        "protocol_version": 1,
        "source_id": "taskplanner",
        "command_id": "suction-start-20260827-test",
        "command": 7,
        "target_side": 0,
        "distance_m": 0.0,
    }


def test_suction_catalog_uses_the_asr_english_token_without_korean_broadening() -> None:
    """Puzzle ASR emits ``suction``; a Korean homophone is not a new command."""

    router = CommandRouter(
        load_command_catalog(default_command_catalog_path()),
        command_id_factory=lambda prefix: f"{prefix}-test",
    )

    assert router.route("suction 시작") is not None
    assert router.route("석션 시작") is None


@pytest.mark.parametrize(
    "utterance",
    [
        "suction 빼",
        "suction 빼줘",
        "suction 빠져",
        "suction 제거해줘",
        "suction 종료",
        "suction 꺼줘",
        "suction out",
        "suction stop",
    ],
)
def test_default_catalog_routes_suction_out_as_command_eight(
    utterance: str,
) -> None:
    catalog = load_command_catalog(default_command_catalog_path())
    router = CommandRouter(
        catalog,
        command_id_factory=lambda prefix: f"{prefix}-20260829-test",
    )

    routed = router.route(utterance)

    assert routed is not None
    assert routed.command.name == "suction_out"
    assert routed.endpoint == EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT
    assert dict(routed.payload) == {
        "protocol_version": 1,
        "source_id": "taskplanner",
        "command_id": "suction-out-20260829-test",
        "command": 8,
        "target_side": 0,
        "distance_m": 0.0,
    }


def test_catalog_routes_suction_out_from_noisy_mixed_asr_text() -> None:
    catalog = load_command_catalog(default_command_catalog_path())
    router = CommandRouter(
        catalog,
        command_id_factory=lambda prefix: f"{prefix}-mixed-test",
    )

    routed = router.route("안녕하세요 저는 누구 입니다 suction 하나나 빠져")

    assert routed is not None
    assert routed.command.name == "suction_out"
    assert dict(routed.payload)["command"] == 8


def test_catalog_keyword_fallback_keeps_questions_negations_and_quotes_silent() -> None:
    catalog = load_command_catalog(default_command_catalog_path())
    router = CommandRouter(catalog)

    assert router.route("안녕하세요 suction 빠져도 될까요?") is None
    assert router.route("왜 suction 빠져") is None
    assert router.route("suction 빠져도 돼") is None
    assert router.route("안녕하세요 suction 빼지 마") is None
    assert router.route("suction 빠져라고 말했다") is None
    assert router.route('저는 "suction 빠져"라고 했어요') is None
    assert router.route("저는 suction 빠져 라고 했어요") is None
    assert router.route('"suction 빠져"') is None
    assert router.route('안녕하세요 "suction 빠져"') is None
    assert router.route("앞 문장입니다. “suction 빠져”.") is None
    assert router.route("suction 시작해줘 그리고 suction 빠져") is None


def test_catalog_accepts_only_a_bounded_complete_final_clause() -> None:
    catalog = load_command_catalog(default_command_catalog_path())
    router = CommandRouter(
        catalog,
        command_id_factory=lambda prefix: f"{prefix}-test",
    )

    routed = router.route("앞 문장에는 잡음이 있어요. suction 들어와")

    assert routed is not None
    assert routed.command.name == "suction"
    assert router.route("suction 들어와 같은 문장을 말했다") is None
    assert router.route("앞 문장이 아주 길어요. suction 들어와도 될까요?") is None


@pytest.mark.parametrize(
    "command_name, command_code, target_side, distance_m, wire_target_side",
    [
        ("start_direct_teach", 1, "none", 0.0, 0),
        ("finish_direct_teach", 2, "none", 0.0, 0),
        ("start_retraction", 3, "none", 0.0, 0),
        ("adjust_retraction", 4, "both", 0.005, 3),
        ("adjust_retraction", 4, "right", -0.01, 2),
        ("change_tool", 5, "none", 0.0, 0),
        ("stop_retraction", 6, "none", 0.0, 0),
    ],
)
def test_typed_retraction_commands_keep_the_peer_v1_codes(
    command_name: str,
    command_code: int,
    target_side: str,
    distance_m: float,
    wire_target_side: int,
) -> None:
    node, _ = _router_node()

    routed = node._typed_retraction_command(
        SimpleNamespace(
            retractor_command=command_name,
            target_side=target_side,
            distance_m=distance_m,
        ),
        source="taskplanner_asr:lan",
        utterance_id=f"utterance-{command_code}",
    )

    assert routed is not None
    assert routed.endpoint == EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT
    assert dict(routed.payload)["protocol_version"] == 1
    assert dict(routed.payload)["command"] == command_code
    assert dict(routed.payload)["target_side"] == wire_target_side
    assert dict(routed.payload)["distance_m"] == distance_m


def test_typed_retraction_adjustment_rejects_zero_distance() -> None:
    node, _ = _router_node()

    routed = node._typed_retraction_command(
        SimpleNamespace(
            retractor_command="adjust_retraction",
            target_side="right",
            distance_m=0.0,
        ),
        source="taskplanner_asr:lan",
        utterance_id="utterance-zero-adjustment",
    )

    assert routed is None


def test_voice_procedure_start_uses_the_active_bundle_default_phase() -> None:
    workspace = Path(__file__).resolve().parents[3]
    bundle = (
        workspace
        / "src/procedure_spec/procedure_spec/specs/thyroidectomy_demo"
    )
    assert default_start_phase_id_for_bundle(str(bundle)) == "P03"

    node, dispatched = _router_node()
    node._simulation_control_service = "/simulation/control"
    node._default_start_phase_id = "P03"

    node._dispatch_resolved_intent(
        SimpleNamespace(
            disposition="propose",
            requires_confirmation=False,
            source="taskplanner_asr:lan",
            utterance_id="voice-start-1",
            intent="procedure_start",
        )
    )

    assert len(dispatched) == 1
    assert dispatched[0].command.name == "voice_procedure_start"
    assert dict(dispatched[0].payload) == {
        "command": "start",
        "start_phase_id": "P03",
    }


def test_voice_procedure_stop_requests_graceful_finish_not_immediate_stop() -> None:
    node, dispatched = _router_node()
    node._simulation_control_service = "/simulation/control"

    node._dispatch_resolved_intent(
        SimpleNamespace(
            disposition="propose",
            requires_confirmation=False,
            source="taskplanner_asr:lan",
            utterance_id="voice-finish-1",
            intent="procedure_stop",
        )
    )

    assert len(dispatched) == 1
    assert dispatched[0].command.name == "voice_procedure_finish"
    assert dispatched[0].endpoint == "/simulation/control"
    assert dict(dispatched[0].payload) == {
        "command": "finish",
        "start_phase_id": "",
    }


def test_operational_voice_retrieve_uses_the_operational_override_endpoint() -> None:
    node, dispatched = _router_node()
    node._operational_surgeon_override_service = (
        "/simulation/operational_surgeon_override"
    )

    node._dispatch_resolved_intent(
        SimpleNamespace(
            disposition="propose",
            requires_confirmation=False,
            source="taskplanner_asr:lan",
            utterance_id="voice-retrieve-1",
            raw_text="보비 정리해줘",
            intent="tool_retrieve",
            tool_id="T04",
        )
    )

    assert len(dispatched) == 1
    assert dispatched[0].command.name == "voice_tool_retrieve"
    assert dispatched[0].endpoint == "/simulation/operational_surgeon_override"
    assert dict(dispatched[0].payload) == {
        "event_type": "return_tool",
        "requested_tool": "T04",
        "voice_text": "보비 정리해줘",
        "ready_for_handover": False,
        "ready_for_retrieval": True,
        "clear_pending_requests": False,
    }


def test_scenario_store_snapshot_replaces_voice_start_phase() -> None:
    workspace = Path(__file__).resolve().parents[3]
    bundle = (
        workspace
        / "src/procedure_spec/procedure_spec/specs/thyroidectomy_demo"
    )
    node = CommandRouterNode.__new__(CommandRouterNode)
    node._default_start_phase_id = "P01"
    node._scenario_config_revision = ""
    node._scenario_config_bundle = ""
    node._scenario_config_spec_dir = ""
    node.get_logger = lambda: _ActionLogger()

    payload = scenario_config_payload(
        bundle_name="thyroidectomy_demo",
        spec_dir=str(bundle),
        revision=compute_bundle_config_revision(bundle),
    )
    node._on_scenario_config(SimpleNamespace(data=json.dumps(payload)))

    assert node._default_start_phase_id == "P03"
    assert node._scenario_config_bundle == "thyroidectomy_demo"


def test_exact_admitted_catalog_command_relays_once_and_uses_stable_id() -> None:
    node, dispatched = _router_node()
    observed: list[object] = []
    node._observed_utterance_publisher = SimpleNamespace(publish=observed.append)
    utterance = SimpleNamespace(
        text="suction",
        source="taskplanner_asr:lan",
        utterance_id="utterance-42",
    )

    node._on_utterance(utterance)

    assert observed == [utterance]
    assert len(dispatched) == 1
    assert dispatched[0].command_id == deterministic_command_id(
        "suction-start",
        source="taskplanner_asr:lan",
        utterance_id="utterance-42",
        command_name="suction",
    )


def test_catalog_miss_is_forwarded_once_and_only_correlated_reply_dispatches() -> None:
    node, dispatched = _router_node()
    observed: list[object] = []
    forwarded: list[object] = []
    resolved: list[object] = []
    node._observed_utterance_publisher = SimpleNamespace(publish=observed.append)
    node._resolver_input_publisher = SimpleNamespace(publish=forwarded.append)
    node._resolver_pending = {}
    node._dispatch_resolved_intent = resolved.append
    utterance = SimpleNamespace(
        text="보비 주세요",
        source="taskplanner_asr:lan",
        utterance_id="utterance-43",
    )

    node._on_utterance(utterance)

    assert observed == [utterance]
    assert forwarded == [utterance]
    assert dispatched == []
    proposal = SimpleNamespace(
        source=utterance.source,
        utterance_id=utterance.utterance_id,
        raw_text=utterance.text,
    )
    node._on_resolved_intent(proposal)
    node._on_resolved_intent(proposal)

    assert resolved == [proposal]


def test_invalid_correlated_resolver_reply_cannot_consume_valid_pending_turn() -> None:
    node, _ = _router_node()
    forwarded: list[object] = []
    resolved: list[object] = []
    node._resolver_input_publisher = SimpleNamespace(publish=forwarded.append)
    node._resolver_pending = {}
    node._dispatch_resolved_intent = resolved.append
    utterance = SimpleNamespace(
        text="보비 주세요",
        source="taskplanner_asr:lan",
        utterance_id="utterance-pending-guard",
    )

    node._forward_to_resolver(utterance)
    assert forwarded == [utterance]

    node._on_resolved_intent(
        SimpleNamespace(
            source=utterance.source,
            utterance_id=utterance.utterance_id,
            raw_text="forged text",
        )
    )
    assert resolved == []

    valid = SimpleNamespace(
        source=utterance.source,
        utterance_id=utterance.utterance_id,
        raw_text=utterance.text,
    )
    node._on_resolved_intent(valid)
    assert resolved == [valid]


@pytest.mark.parametrize(
    "utterance",
    [
        "오른쪽 1cm 더 당겨줘 suction 빠져",
        "suction 시작해줘 그리고 suction 빠져",
        "보비 줘 suction 빠져",
        "suction 시작 그리고 suction 시작",
        "suction 시작. suction 시작",
        "suction 시작, suction 시작",
    ],
)
def test_router_drops_all_multi_command_deliveries_before_catalog_or_resolver(
    utterance: str,
) -> None:
    node, dispatched = _router_node()
    observed: list[object] = []
    forwarded: list[object] = []
    node._observed_utterance_publisher = SimpleNamespace(publish=observed.append)
    node._resolver_input_publisher = SimpleNamespace(publish=forwarded.append)
    node._resolver_pending = {}
    message = SimpleNamespace(
        text=utterance,
        source="taskplanner_asr:lan",
        utterance_id=f"compound-{abs(hash(utterance))}",
    )

    node._on_utterance(message)

    assert observed == [message]
    assert dispatched == []
    assert forwarded == []


def test_resolver_source_refuses_admitted_asr_ingress() -> None:
    resolver_source = (
        Path(__file__).resolve().parents[1] / "voice_command" / "node.py"
    ).read_text(encoding="utf-8")

    assert '"input_topic", "/surgery/voice/resolver_utterance"' in resolver_source
    assert "must not subscribe to admitted ASR" in resolver_source


def test_admitted_asr_has_no_non_router_subscription_site() -> None:
    """Keep command_router the one executable consumer of admitted ASR.

    The resolver has a defensive parameter check, but it must not acquire a
    direct subscription while another node is being refactored.  This focused
    source check covers every former voice-admission consumer, including the
    VLM observer (which now follows the router's observed relay instead).
    """

    workspace = Path(__file__).resolve().parents[3]
    non_router_sources = (
        workspace / "src/voice_command/voice_command/node.py",
        workspace
        / "src/bt_orchestrator/bt_orchestrator/bed_robot_arm_group_orchestrator.py",
        workspace / "src/or_digital_twin/or_digital_twin/node.py",
        workspace / "src/simulation_runtime/simulation_runtime/simulation_manager.py",
        workspace / "src/vlm_node/vlm_node/real_vlm.py",
    )
    for source_path in non_router_sources:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        subscription_calls = [
            ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_subscription"
        ]
        assert all(
            "/surgery/audio/admitted_utterance" not in call
            for call in subscription_calls
        ), source_path


def test_router_uses_fixed_physical_proxy_without_route_state_dependency() -> None:
    node, dispatched = _router_node()
    utterance = SimpleNamespace(text="suction")

    # The router never receives or interprets execution route state.  Its one
    # fixed proxy target stays usable while the execution owner changes route.
    node._on_utterance(utterance)
    node._on_utterance(utterance)
    assert [item.endpoint for item in dispatched] == [
        EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT,
        EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT,
    ]


def test_router_dispatches_absolute_catalog_endpoint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "commands.yaml"
    path.write_text(
        f"""schema: {CATALOG_SCHEMA}
commands:
  - name: lab_note
    phrases: [lab note]
    dispatch:
      kind: topic
      type: std_msgs/msg/String
      endpoint: /lab/note
      payload: {{data: recorded}}
""",
        encoding="utf-8",
    )
    reloader = CommandCatalogReloader(path)
    changed, error = reloader.reload_if_changed(force=True)
    assert changed is True, error
    node = CommandRouterNode.__new__(CommandRouterNode)
    node._catalog_reloader = reloader
    node._router = None
    node.get_logger = lambda: _ActionLogger()
    dispatched: list[object] = []
    node._dispatch = dispatched.append
    node._set_catalog(reloader)

    node._on_utterance(SimpleNamespace(text="lab note"))

    assert len(dispatched) == 1
    assert dispatched[0].endpoint == "/lab/note"


def test_router_routes_physical_action_catalog_entries_to_fixed_proxy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "commands.yaml"
    path.write_text(
        f"""schema: {CATALOG_SCHEMA}
commands:
  - name: handover
    phrases: [give forceps]
    dispatch:
      kind: action
      type: surgical_interop_msgs/action/ExecuteToolHandover
      endpoint: /taskplanner/execution/tool_handover
      timeout_sec: 10
      payload: {{command_id: "{{command_id}}"}}
""",
        encoding="utf-8",
    )
    reloader = CommandCatalogReloader(path)
    changed, error = reloader.reload_if_changed(force=True)
    assert changed is True, error
    node = CommandRouterNode.__new__(CommandRouterNode)
    node._catalog_reloader = reloader
    node._router = None
    node.get_logger = lambda: _ActionLogger()
    dispatched: list[object] = []
    node._dispatch = dispatched.append
    node._set_catalog(reloader)

    node._on_utterance(SimpleNamespace(text="give forceps"))

    assert len(dispatched) == 1
    assert dispatched[0].endpoint == EXECUTION_TOOL_HANDOVER_PROXY_ENDPOINT


def test_suction_is_owned_by_the_hot_reloadable_direct_catalog() -> None:
    catalog = load_command_catalog(default_command_catalog_path())

    routed = CommandRouter(
        catalog,
        command_id_factory=lambda prefix: f"{prefix}-test",
    ).route("suction")

    assert routed is not None
    assert routed.command.kind == "service"
    assert routed.command.interface_type == RETRACTION_SERVICE_TYPE
    assert routed.endpoint == EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT


def test_service_adapter_sends_the_catalog_request_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = load_command_catalog(default_command_catalog_path())
    routed = CommandRouter(
        catalog,
        command_id_factory=lambda prefix: f"{prefix}-test",
    ).route("suction")
    assert routed is not None

    sent = []

    class Request:
        pass

    Service = type("Service", (), {"Request": Request})

    class Future:
        def add_done_callback(self, _callback) -> None:
            pass

    class Client:
        def service_is_ready(self) -> bool:
            return True

        def call_async(self, request):
            sent.append(request)
            return Future()

    class Logger:
        def info(self, *_args, **_kwargs) -> None:
            pass

        def warning(self, *_args, **_kwargs) -> None:
            pass

    client = Client()
    node = CommandRouterNode.__new__(CommandRouterNode)
    node._service_clients = {}
    node.create_client = lambda *_args: client
    node.get_logger = lambda: Logger()
    monkeypatch.setattr(
        "voice_command.command_router.get_service", lambda _type: Service
    )
    monkeypatch.setattr(
        "voice_command.command_router.set_message_fields",
        lambda request, values: request.__dict__.update(values),
    )

    node._dispatch_service(routed)

    assert len(sent) == 1
    assert sent[0].__dict__ == dict(routed.payload)


def test_catalog_hot_reload_retains_last_good_mapping(tmp_path: Path) -> None:
    source = default_command_catalog_path().read_text(encoding="utf-8")
    path = tmp_path / "commands.yaml"
    path.write_text(source, encoding="utf-8")
    reloader = CommandCatalogReloader(path)

    changed, error = reloader.reload_if_changed(force=True)

    assert changed is True
    assert error == ""
    first = reloader.catalog
    assert first is not None

    path.write_text("schema: broken\ncommands: []\n", encoding="utf-8")
    changed, error = reloader.reload_if_changed()

    assert changed is False
    assert "schema" in error
    assert reloader.catalog is first


def test_catalog_can_add_a_topic_without_python_or_registry_changes(tmp_path: Path) -> None:
    path = tmp_path / "commands.yaml"
    path.write_text(
        f"""schema: {CATALOG_SCHEMA}
commands:
  - name: lab_note
    phrases: [lab note]
    dispatch:
      kind: topic
      type: std_msgs/msg/String
      endpoint: /lab/note
      payload: {{data: recorded}}
""",
        encoding="utf-8",
    )

    router = CommandRouter(
        load_command_catalog(path),
        command_id_factory=lambda prefix: f"{prefix}-id",
    )
    routed = router.route("LAB NOTE")

    assert routed is not None
    assert routed.command.kind == "topic"
    assert routed.endpoint == "/lab/note"
    assert dict(routed.payload) == {"data": "recorded"}


@pytest.mark.parametrize(
    "payload_line",
    [
        "protocol_version: 2",
        "source_id: researcher_console",
    ],
)
def test_each_retraction_catalog_service_requires_protocol_version_one(
    tmp_path: Path,
    payload_line: str,
) -> None:
    path = tmp_path / "commands.yaml"
    path.write_text(
        f"""schema: {CATALOG_SCHEMA}
commands:
  - name: adjust_retraction
    phrases: [adjust left]
    dispatch:
      kind: service
      type: surgical_interop_msgs/srv/ExecuteRetractionCommand
      endpoint: /surgery/retraction/command
      payload:
        {payload_line}
""",
        encoding="utf-8",
    )

    with pytest.raises(
        CommandCatalogError,
        match="ExecuteRetractionCommand protocol_version is fixed to 1",
    ):
        load_command_catalog(path)


def test_retraction_catalog_allows_research_owned_command_fields_and_keeps_command_id(
    tmp_path: Path,
) -> None:
    path = tmp_path / "commands.yaml"
    path.write_text(
        f"""schema: {CATALOG_SCHEMA}
commands:
  - name: adjust_retraction
    phrases: [adjust left]
    command_id_prefix: adjust-left
    dispatch:
      kind: service
      type: surgical_interop_msgs/srv/ExecuteRetractionCommand
      endpoint: /taskplanner/execution/retraction/command
      payload:
        protocol_version: 1
        source_id: researcher_console
        command_id: "{{command_id}}"
        command: 4
        target_side: 1
        distance_m: 0.025
""",
        encoding="utf-8",
    )

    routed = CommandRouter(
        load_command_catalog(path),
        command_id_factory=lambda prefix: f"{prefix}-test",
    ).route("adjust left")

    assert routed is not None
    assert routed.command.name == "adjust_retraction"
    assert routed.command_id == "adjust-left-test"
    assert dict(routed.payload) == {
        "protocol_version": 1,
        "source_id": "researcher_console",
        "command_id": "adjust-left-test",
        "command": 4,
        "target_side": 1,
        "distance_m": 0.025,
    }


@pytest.mark.parametrize(
    ("interface_type", "kind", "endpoint"),
    [
        (
            RETRACTION_SERVICE_TYPE,
            "service",
            "/surgery/retraction/command",
        ),
        (
            "surgical_interop_msgs/action/ExecuteToolHandover",
            "action",
            "/surgery/tool_handover",
        ),
    ],
)
def test_catalog_rejects_physical_commands_that_bypass_execution_proxy(
    tmp_path: Path,
    interface_type: str,
    kind: str,
    endpoint: str,
) -> None:
    path = tmp_path / "commands.yaml"
    path.write_text(
        f"""schema: {CATALOG_SCHEMA}
commands:
  - name: physical_command
    phrases: [physical command]
    dispatch:
      kind: {kind}
      type: {interface_type}
      endpoint: {endpoint}
      payload:
        protocol_version: 1
        source_id: taskplanner
        command_id: "{{command_id}}"
        command: 7
        target_side: 0
        distance_m: 0.0
""",
        encoding="utf-8",
    )

    with pytest.raises(CommandCatalogError, match="must use execution proxy"):
        load_command_catalog(path)


def test_catalog_rejects_removed_launch_endpoint_aliases(tmp_path: Path) -> None:
    path = tmp_path / "commands.yaml"
    path.write_text(
        f"""schema: {CATALOG_SCHEMA}
commands:
  - name: legacy_alias
    phrases: [legacy alias]
    dispatch:
      kind: topic
      type: std_msgs/msg/String
      endpoint: "@legacy_route"
      payload: {{data: ignored}}
""",
        encoding="utf-8",
    )

    with pytest.raises(CommandCatalogError, match="absolute ROS name"):
        load_command_catalog(path)


def test_runtime_boundary_rejects_constructed_physical_bypass() -> None:
    command = CatalogCommand(
        name="physical_bypass",
        phrases=(),
        kind="service",
        interface_type=RETRACTION_SERVICE_TYPE,
        endpoint="/surgery/retraction/command",
        payload={},
        command_id_prefix="physical-bypass",
        action_timeout_sec=None,
    )
    routed = RoutedCommand(
        command=command,
        command_id="physical-bypass-id",
        endpoint=command.endpoint,
        payload={},
    )

    with pytest.raises(CommandCatalogError, match="must use the execution proxy"):
        CommandRouterNode._assert_execution_proxy_boundary(routed)


def test_action_catalog_uses_a_per_command_bounded_timeout(tmp_path: Path) -> None:
    routed = _action_routed(tmp_path, timeout_sec=2.5)

    assert routed.command.kind == "action"
    assert routed.command.action_timeout_sec == 2.5


def test_action_catalog_rejects_timeout_on_non_action(tmp_path: Path) -> None:
    path = tmp_path / "bad_commands.yaml"
    path.write_text(
        f"""schema: {CATALOG_SCHEMA}
commands:
  - name: note
    phrases: [note]
    dispatch:
      kind: topic
      type: std_msgs/msg/String
      endpoint: /lab/note
      timeout_sec: 1.0
      payload: {{data: note}}
""",
        encoding="utf-8",
    )

    with pytest.raises(CommandCatalogError, match="only valid for actions"):
        load_command_catalog(path)


def test_action_adapter_observes_feedback_and_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    routed = _action_routed(tmp_path)

    class Goal:
        pass

    Action = type("Action", (), {"Goal": Goal})

    class GoalHandle:
        accepted = True

        def __init__(self) -> None:
            self.result_future = _DeferredFuture()
            self.cancel_future = _DeferredFuture()
            self.cancel_calls = 0

        def get_result_async(self):
            return self.result_future

        def cancel_goal_async(self):
            self.cancel_calls += 1
            return self.cancel_future

    class Client:
        def __init__(self) -> None:
            self.goal_response = _DeferredFuture()
            self.goal = None
            self.feedback_callback = None

        def server_is_ready(self) -> bool:
            return True

        def send_goal_async(self, goal, *, feedback_callback):
            self.goal = goal
            self.feedback_callback = feedback_callback
            return self.goal_response

    client = Client()
    logger = _ActionLogger()
    node = CommandRouterNode.__new__(CommandRouterNode)
    node._action_clients = {(routed.command.interface_type, routed.endpoint): client}
    node._active_actions = {}
    node.get_logger = lambda: logger
    monkeypatch.setattr(
        "voice_command.command_router.get_action", lambda _type: Action
    )
    monkeypatch.setattr(
        "voice_command.command_router.set_message_fields",
        lambda goal, values: goal.__dict__.update(values),
    )

    node._dispatch_action(routed)

    assert client.goal.__dict__ == {"target": "mayo"}
    assert routed.command_id in node._active_actions
    assert client.feedback_callback is not None

    handle = GoalHandle()
    client.goal_response.resolve(handle)
    client.feedback_callback(SimpleNamespace(feedback=SimpleNamespace(progress=0.5)))
    handle.result_future.resolve(
        SimpleNamespace(status=4, result=SimpleNamespace(success=True))
    )

    assert routed.command_id not in node._active_actions
    messages = "\n".join(message for _level, message in logger.entries)
    assert "action feedback" in messages
    assert "action result" in messages
    assert "status=4" in messages
    assert handle.cancel_calls == 0


def test_action_timeout_requests_cancel_and_keeps_result_observable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    routed = _action_routed(tmp_path, timeout_sec=0.25)
    clock = {"now": 10.0}
    monkeypatch.setattr(
        "voice_command.command_router.time.monotonic", lambda: clock["now"]
    )

    class Goal:
        pass

    Action = type("Action", (), {"Goal": Goal})

    class GoalHandle:
        accepted = True

        def __init__(self) -> None:
            self.result_future = _DeferredFuture()
            self.cancel_future = _DeferredFuture()
            self.cancel_calls = 0

        def get_result_async(self):
            return self.result_future

        def cancel_goal_async(self):
            self.cancel_calls += 1
            return self.cancel_future

    class Client:
        def __init__(self) -> None:
            self.goal_response = _DeferredFuture()

        def server_is_ready(self) -> bool:
            return True

        def send_goal_async(self, _goal, *, feedback_callback):
            self.feedback_callback = feedback_callback
            return self.goal_response

    client = Client()
    logger = _ActionLogger()
    node = CommandRouterNode.__new__(CommandRouterNode)
    node._action_clients = {(routed.command.interface_type, routed.endpoint): client}
    node._active_actions = {}
    node.get_logger = lambda: logger
    monkeypatch.setattr(
        "voice_command.command_router.get_action", lambda _type: Action
    )
    monkeypatch.setattr(
        "voice_command.command_router.set_message_fields", lambda *_args: None
    )

    node._dispatch_action(routed)
    handle = GoalHandle()
    client.goal_response.resolve(handle)

    clock["now"] = 10.3
    node._poll_action_timeouts()

    active = node._active_actions[routed.command_id]
    assert active.cancel_requested is True
    assert active.cancel_reason == "completion_timeout"
    assert handle.cancel_calls == 1

    handle.cancel_future.resolve(SimpleNamespace(goals_canceling=[object()]))
    handle.result_future.resolve(
        SimpleNamespace(status=5, result=SimpleNamespace(success=False))
    )

    assert routed.command_id not in node._active_actions
    messages = "\n".join(message for _level, message in logger.entries)
    assert "cancellation requested" in messages
    assert "accepted=True" in messages
    assert "action result" in messages


def test_action_cancel_before_goal_acceptance_is_sent_after_acceptance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    routed = _action_routed(tmp_path)

    class Goal:
        pass

    Action = type("Action", (), {"Goal": Goal})

    class GoalHandle:
        accepted = True

        def __init__(self) -> None:
            self.result_future = _DeferredFuture()
            self.cancel_future = _DeferredFuture()
            self.cancel_calls = 0

        def get_result_async(self):
            return self.result_future

        def cancel_goal_async(self):
            self.cancel_calls += 1
            return self.cancel_future

    class Client:
        def __init__(self) -> None:
            self.goal_response = _DeferredFuture()

        def server_is_ready(self) -> bool:
            return True

        def send_goal_async(self, _goal, *, feedback_callback):
            self.feedback_callback = feedback_callback
            return self.goal_response

    client = Client()
    node = CommandRouterNode.__new__(CommandRouterNode)
    node._action_clients = {(routed.command.interface_type, routed.endpoint): client}
    node._active_actions = {}
    node.get_logger = lambda: _ActionLogger()
    monkeypatch.setattr(
        "voice_command.command_router.get_action", lambda _type: Action
    )
    monkeypatch.setattr(
        "voice_command.command_router.set_message_fields", lambda *_args: None
    )

    node._dispatch_action(routed)

    assert node.cancel_action(routed.command_id, reason="focused_restart") is True
    handle = GoalHandle()
    client.goal_response.resolve(handle)

    assert handle.cancel_calls == 1
    assert node.cancel_action("missing-command") is False
