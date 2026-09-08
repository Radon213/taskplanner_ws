"""Direct launch wiring for Taskplanner's endpoint-routing and execution owner."""

from __future__ import annotations

import os
from typing import Final

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, SetLaunchConfiguration
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution, PythonExpression
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare

from bringup.runtime_profile import RUNTIME_PROFILE_NAMES, RuntimeProfileError, resolve_runtime_profile

_EXECUTION_PROFILE_ARGUMENT_NAMES: Final[tuple[str, ...]] = (
    "default_bundle",
    "execution_backend",
    "robot_endpoint_source",
    "retraction_endpoint_source",
    "enable_runtime_route_control",
    "external_controller_contract_id",
    "external_capability_policy_id",
)


def _execution_profile_actions(context) -> list[SetLaunchConfiguration]:
    """Resolve only the endpoint-routing owner profile values.

    This owner owns the typed Action/Service adapter wiring, not a second
    scenario-admission decision.  It deliberately does not import the legacy
    launch graph just to inherit unrelated ASR, VLM, camera, or browser
    settings.
    """

    profile_name = LaunchConfiguration("runtime_profile").perform(context)
    try:
        profile = resolve_runtime_profile(profile_name, environment=os.environ)
    except RuntimeProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    values = profile.arguments_for("execution")
    return [
        SetLaunchConfiguration(name, values[name])
        for name in _EXECUTION_PROFILE_ARGUMENT_NAMES
        if name in values
    ]


def _execution_actions() -> list[object]:
    """Build the execution owner without the retained monolithic graph.

    The conditions and endpoint names intentionally mirror the previous
    launch wiring.  The bridge remains the single owner of endpoint routing,
    availability/type checks, command-id idempotency, cancellation, and
    result handling.
    """

    spec_dir = LaunchConfiguration("spec_dir")
    execution_backend = LaunchConfiguration("execution_backend")
    robot_endpoint_source = LaunchConfiguration("robot_endpoint_source")
    retraction_endpoint_source = LaunchConfiguration("retraction_endpoint_source")
    enable_runtime_route_control = LaunchConfiguration("enable_runtime_route_control")
    external_controller_contract_id = LaunchConfiguration(
        "external_controller_contract_id"
    )
    external_capability_policy_id = LaunchConfiguration(
        "external_capability_policy_id"
    )
    bed_robot_contract_enabled = LaunchConfiguration("bed_robot_contract_enabled")
    bed_robot_contract_procedure_type = LaunchConfiguration(
        "bed_robot_contract_procedure_type"
    )
    tool_handover_contract_enabled = LaunchConfiguration(
        "tool_handover_contract_enabled"
    )
    retraction_workflow_state_enforced = LaunchConfiguration(
        "retraction_workflow_state_enforced"
    )
    execution_route_state_topic = LaunchConfiguration("execution_route_state_topic")
    retraction_proxy_service = LaunchConfiguration("retraction_proxy_service")
    tool_handover_proxy_action = LaunchConfiguration("tool_handover_proxy_action")
    service_receipt_timeout_sec = LaunchConfiguration("service_receipt_timeout_sec")
    route_selection_state_path = LaunchConfiguration("route_selection_state_path")
    route_selection_runtime_mode = LaunchConfiguration("route_selection_runtime_mode")

    direct_execution_bridge_enabled = PythonExpression(
        [
            "'",
            execution_backend,
            "' != 'mock' or '",
            bed_robot_contract_enabled,
            "'.lower() == 'true'",
        ]
    )
    robot_contract_emulator_enabled = PythonExpression(
        [
            "'",
            execution_backend,
            "' == 'mock' and '",
            bed_robot_contract_enabled,
            "'.lower() == 'true' and '",
            robot_endpoint_source,
            "'.strip().lower() != 'virtual'",
        ]
    )
    tool_handover_endpoint = PythonExpression(
        [
            "'/integration/virtual/surgery/tool_handover' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else '/surgery/tool_handover'",
        ]
    )
    retraction_service_name = PythonExpression(
        [
            "'/integration/virtual/surgery/retraction/command' if '",
            retraction_endpoint_source,
            "'.strip().lower() == 'virtual' else '/surgery/retraction/command'",
        ]
    )
    controller_contract_topic = PythonExpression(
        [
            "'/integration/virtual/surgery/controller_contract' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else '/surgery/controller_contract'",
        ]
    )
    expected_controller_contract_id = PythonExpression(
        [
            "'taskplanner-virtual-eir-nuc.v1' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else '",
            external_controller_contract_id,
            "'",
        ]
    )
    expected_capability_policy_id = PythonExpression(
        [
            "'taskplanner-virtual-full-inventory.v1' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else '",
            external_capability_policy_id,
            "'",
        ]
    )
    retraction_state_machine_suppression_enabled = PythonExpression(
        [
            "'",
            retraction_endpoint_source,
            "'.strip().lower() == 'virtual' or '",
            retraction_workflow_state_enforced,
            "'.strip().lower() != 'true'",
        ]
    )
    physical_stop_confirmation_required = PythonExpression(
        [
            "'",
            robot_endpoint_source,
            "'.strip().lower() == 'external' and '",
            execution_backend,
            "'.strip().lower() == 'external'",
        ]
    )
    external_physical_stop_confirmation_required = PythonExpression(
        [
            "'",
            execution_backend,
            "'.strip().lower() == 'external'",
        ]
    )
    emulator_contract_id = PythonExpression(
        [
            "'taskplanner-virtual-eir-nuc.v1' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else 'taskplanner-generic-emulator.v1'",
        ]
    )
    emulator_capability_policy_id = PythonExpression(
        [
            "'taskplanner-virtual-full-inventory.v1' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else 'taskplanner-generic-emulator.v1'",
        ]
    )
    robot_contract_profile = PathJoinSubstitution(
        [FindPackageShare("bringup"), "config", "robot_contract_success.yaml"]
    )

    return [
        # Stable command-facing endpoints. The execution bridge remains the
        # sole route owner and republishes its transient-local selected route;
        # this proxy never snapshots scenario or endpoint choice at launch.
        Node(
            package="surgical_interop_execution",
            executable="execution_command_proxy",
            name="execution_command_proxy",
            parameters=[
                {
                    "route_state_topic": execution_route_state_topic,
                    "retraction_proxy_service": retraction_proxy_service,
                    "tool_handover_proxy_action": tool_handover_proxy_action,
                    "service_receipt_timeout_sec": ParameterValue(
                        service_receipt_timeout_sec,
                        value_type=float,
                    ),
                }
            ],
            output="screen",
        ),
        Node(
            package="surgical_interop_execution",
            executable="fault_action_emulator",
            name="robot_contract_emulator",
            condition=IfCondition(robot_contract_emulator_enabled),
            parameters=[
                {
                    "profile_path": robot_contract_profile,
                    "procedure_type": ParameterValue(
                        bed_robot_contract_procedure_type,
                        value_type=str,
                    ),
                    "robot_endpoint_source": robot_endpoint_source,
                    "tool_handover_endpoint": tool_handover_endpoint,
                    "retraction_service_name": "/surgery/retraction/command",
                    "controller_contract_topic": controller_contract_topic,
                    "controller_contract_id": emulator_contract_id,
                    "capability_policy_id": emulator_capability_policy_id,
                    "publish_bed_robot_status": ParameterValue(
                        PythonExpression(
                            [
                                "not ('",
                                retraction_endpoint_source,
                                "'.strip().lower() == 'virtual')",
                            ]
                        ),
                        value_type=bool,
                    ),
                }
            ],
            output="screen",
        ),
        Node(
            package="surgical_interop_execution",
            executable="fault_action_emulator",
            name="virtual_robot_contract_emulator",
            condition=IfCondition(direct_execution_bridge_enabled),
            parameters=[
                {
                    "profile_path": robot_contract_profile,
                    "procedure_type": ParameterValue(
                        bed_robot_contract_procedure_type,
                        value_type=str,
                    ),
                    "robot_endpoint_source": "virtual",
                    "tool_handover_endpoint": (
                        "/integration/virtual/surgery/tool_handover"
                    ),
                    "retraction_service_name": (
                        "/integration/virtual/surgery/retraction/command"
                    ),
                    "controller_contract_topic": (
                        "/integration/virtual/surgery/controller_contract"
                    ),
                    "controller_contract_id": "taskplanner-virtual-eir-nuc.v1",
                    "capability_policy_id": "taskplanner-virtual-full-inventory.v1",
                    "publish_bed_robot_status": False,
                }
            ],
            output="screen",
        ),
        Node(
            package="surgical_interop_execution",
            executable="surgical_interop_execution_bridge",
            name="surgical_interop_execution_bridge",
            condition=IfCondition(direct_execution_bridge_enabled),
            parameters=[
                {
                    "spec_dir": spec_dir,
                    "robot_endpoint_source": robot_endpoint_source,
                    "retraction_endpoint_source": retraction_endpoint_source,
                    "retraction_state_machine_suppressed": ParameterValue(
                        retraction_state_machine_suppression_enabled,
                        value_type=bool,
                    ),
                    "enable_runtime_route_control": ParameterValue(
                        enable_runtime_route_control,
                        value_type=bool,
                    ),
                    "direct_hand_dispatch_ledger_path": EnvironmentVariable(
                        "TASKPLANNER_DIRECT_HAND_LEDGER_PATH",
                        default_value="/tmp/taskplanner-direct-hand-dispatch.sqlite3",
                    ),
                    "route_selection_state_path": route_selection_state_path,
                    "route_selection_runtime_mode": route_selection_runtime_mode,
                    "direct_hand_state_max_age_sec": 1.0,
                    "tool_handover_endpoint": tool_handover_endpoint,
                    "external_tool_handover_endpoint": "/surgery/tool_handover",
                    "virtual_tool_handover_endpoint": (
                        "/integration/virtual/surgery/tool_handover"
                    ),
                    "tool_handover_enabled": ParameterValue(
                        tool_handover_contract_enabled,
                        value_type=bool,
                    ),
                    "retraction_service_name": retraction_service_name,
                    "external_retraction_service_name": "/surgery/retraction/command",
                    "virtual_retraction_service_name": (
                        "/integration/virtual/surgery/retraction/command"
                    ),
                    "bed_robot_status_endpoint": "/external/bed_robot_arms/status",
                    "require_bed_robot_status": ParameterValue(
                        PythonExpression(["False"]), value_type=bool
                    ),
                    "controller_contract_topic": controller_contract_topic,
                    "external_controller_contract_topic": (
                        "/surgery/controller_contract"
                    ),
                    "virtual_controller_contract_topic": (
                        "/integration/virtual/surgery/controller_contract"
                    ),
                    "expected_controller_contract_id": expected_controller_contract_id,
                    "external_expected_controller_contract_id": (
                        external_controller_contract_id
                    ),
                    "virtual_expected_controller_contract_id": (
                        "taskplanner-virtual-eir-nuc.v1"
                    ),
                    "expected_capability_policy_id": expected_capability_policy_id,
                    "external_expected_capability_policy_id": (
                        external_capability_policy_id
                    ),
                    "virtual_expected_capability_policy_id": (
                        "taskplanner-virtual-full-inventory.v1"
                    ),
                    "external_require_bed_robot_status": False,
                    "external_require_physical_stop_confirmation": ParameterValue(
                        external_physical_stop_confirmation_required,
                        value_type=bool,
                    ),
                    "integration_readiness_topic": "/integration/readiness",
                    "require_physical_stop_confirmation": ParameterValue(
                        physical_stop_confirmation_required,
                        value_type=bool,
                    ),
                    "server_wait_timeout_sec": 3.0,
                }
            ],
            output="screen",
        ),
    ]


def _generate_execution_launch_description() -> LaunchDescription:
    """Build the scoped endpoint-routing owner without legacy graph parsing."""

    default_bundle = LaunchConfiguration("default_bundle")
    spec_default = PathJoinSubstitution(
        [FindPackageShare("procedure_spec"), "specs", default_bundle]
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "runtime_profile",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_RUNTIME_MODE", default_value="mock"
                ),
                choices=RUNTIME_PROFILE_NAMES,
                description="Mode-level endpoint-routing defaults.",
            ),
            OpaqueFunction(
                function=lambda context: _execution_profile_actions(context)
            ),
            DeclareLaunchArgument(
                "default_bundle",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEFAULT_BUNDLE", default_value="thyroidectomy"
                ),
            ),
            DeclareLaunchArgument("spec_dir", default_value=spec_default),
            DeclareLaunchArgument(
                "bed_robot_contract_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_BED_ROBOT", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "bed_robot_contract_procedure_type",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_PROCEDURE_TYPE",
                    default_value="thyroidectomy",
                ),
            ),
            DeclareLaunchArgument(
                "tool_handover_contract_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_TOOL_HANDOVER", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "retraction_workflow_state_enforced",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_RETRACTION_WORKFLOW_STATE",
                    default_value="false",
                ),
            ),
            DeclareLaunchArgument(
                "execution_route_state_topic",
                default_value="/integration/execution_route/state",
            ),
            DeclareLaunchArgument(
                "retraction_proxy_service",
                default_value="/taskplanner/execution/retraction/command",
            ),
            DeclareLaunchArgument(
                "tool_handover_proxy_action",
                default_value="/taskplanner/execution/tool_handover",
            ),
            DeclareLaunchArgument(
                "service_receipt_timeout_sec", default_value="2.0"
            ),
            DeclareLaunchArgument(
                "route_selection_state_path",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_EXECUTION_ROUTE_SELECTION_PATH",
                    default_value="/taskplanner-execution-state/route_selection.json",
                ),
            ),
            DeclareLaunchArgument(
                "route_selection_runtime_mode",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_RUNTIME_MODE", default_value=""
                ),
            ),
            DeclareLaunchArgument("execution_backend", default_value="mock"),
            DeclareLaunchArgument(
                "robot_endpoint_source",
                default_value="external",
                choices=("external", "virtual"),
            ),
            DeclareLaunchArgument(
                "retraction_endpoint_source",
                default_value=LaunchConfiguration("robot_endpoint_source"),
                choices=("external", "virtual"),
            ),
            DeclareLaunchArgument(
                "enable_runtime_route_control", default_value="false"
            ),
            DeclareLaunchArgument(
                "external_controller_contract_id",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_EXTERNAL_CONTROLLER_CONTRACT_ID",
                    default_value="eir-nuc-tool-handover.real.v1",
                ),
            ),
            DeclareLaunchArgument(
                "external_capability_policy_id",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_EXTERNAL_CAPABILITY_POLICY_ID",
                    default_value="eir-nuc-tool-handover.v1",
                ),
            ),
            *_execution_actions(),
        ]
    )
