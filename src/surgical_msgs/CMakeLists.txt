cmake_minimum_required(VERSION 3.16)
project(surgical_msgs)

find_package(ament_cmake REQUIRED)
find_package(action_msgs REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "action/ExecuteSkill.action"
  "msg/BTDecision.msg"
  "msg/BTContextSnapshot.msg"
  "msg/EventDigest.msg"
  "msg/FilteredPhase.msg"
  "msg/InstrumentState.msg"
  "msg/PhaseEvidence.msg"
  "msg/PerceptionScene.msg"
  "msg/PhaseTransitionCue.msg"
  "msg/ReducerDecisionEvent.msg"
  "msg/SimulationEvent.msg"
  "msg/SimulationState.msg"
  "msg/SurgeonActorEvent.msg"
  "msg/SurgeonOutwardSignal.msg"
  "msg/SkillCommand.msg"
  "msg/SkillStatus.msg"
  "msg/SurgeonGestureEvidence.msg"
  "msg/SurgeonRequest.msg"
  "msg/SurgeonState.msg"
  "msg/ToolObservation.msg"
  "msg/TwinEvent.msg"
  "msg/VLMHealth.msg"
  "msg/VLMInferenceProposal.msg"
  "msg/VLMRequestContext.msg"
  "msg/VLMReducerDecision.msg"
  "msg/VLMResult.msg"
  "msg/WorldState.msg"
  "srv/ControlSimulation.srv"
  "srv/InjectSurgeonOverride.srv"
  "srv/SelectSimulationBundle.srv"
  DEPENDENCIES action_msgs builtin_interfaces
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
