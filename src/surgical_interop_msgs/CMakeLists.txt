cmake_minimum_required(VERSION 3.16)
project(surgical_interop_msgs)

find_package(ament_cmake REQUIRED)
find_package(action_msgs REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "action/ExecuteRetractionAdjustment.action"
  "action/ExecuteToolHandover.action"
  "msg/BedRobotArmState.msg"
  "msg/BedRobotArmStateArray.msg"
  "msg/ClinicalObservation.msg"
  "msg/ClinicalObservationArray.msg"
  "msg/GatewayInfo.msg"
  "msg/InstrumentState.msg"
  "msg/InstrumentStateArray.msg"
  "msg/InstrumentCatalogEntry.msg"
  "msg/PhaseCatalogEntry.msg"
  "msg/ProcedureCatalog.msg"
  "msg/RobotEndEffectorState.msg"
  "msg/RobotEndEffectorStateArray.msg"
  "msg/RobotState.msg"
  "msg/RobotStateArray.msg"
  "msg/SpeechRecognitionState.msg"
  "msg/SurgeryContext.msg"
  "msg/SurgeryEvent.msg"
  "msg/SurgeryHealth.msg"
  "msg/ToolPrediction.msg"
  "msg/ToolPredictionArray.msg"
  "srv/ExecuteRetractionCommand.srv"
  "srv/RequestToolChange.srv"
  DEPENDENCIES action_msgs builtin_interfaces
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
