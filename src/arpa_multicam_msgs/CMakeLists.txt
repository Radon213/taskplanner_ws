cmake_minimum_required(VERSION 3.8)
project(arpa_multicam_msgs)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

# Local operator-side copy of the two message definitions consumed by
# rosbridge.  Field order matches the deployed ARPA rig exactly so DDS type
# hashes and CaptureStatus deserialization remain compatible.
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/CameraCoverage.msg"
  "msg/CaptureStatus.msg"
  DEPENDENCIES std_msgs
)

ament_package()
