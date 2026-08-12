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
  "msg/InstrumentState.msg"
  "msg/InstrumentStateArray.msg"
  "msg/RobotState.msg"
  "msg/RobotStateArray.msg"
  "msg/SurgeryContext.msg"
  "msg/SurgeryEvent.msg"
  "msg/SurgeryHealth.msg"
  "srv/RequestToolChange.srv"
  DEPENDENCIES action_msgs builtin_interfaces
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
