cmake_minimum_required(VERSION 3.16)
project(taskplanner_bt_nodes)

find_package(ament_cmake REQUIRED)
find_package(auto_apms_behavior_tree REQUIRED)
find_package(auto_apms_behavior_tree_core REQUIRED)
find_package(behaviortree_cpp REQUIRED)
find_package(rclcpp REQUIRED)
find_package(surgical_msgs REQUIRED)

add_library(${PROJECT_NAME} SHARED
  src/taskplanner_bt_nodes.cpp
)
target_compile_features(${PROJECT_NAME} PUBLIC cxx_std_17)
ament_target_dependencies(${PROJECT_NAME}
  auto_apms_behavior_tree
  auto_apms_behavior_tree_core
  behaviortree_cpp
  rclcpp
  surgical_msgs
)

auto_apms_behavior_tree_register_nodes(${PROJECT_NAME}
  taskplanner_bt_nodes::LoadWorldState
  taskplanner_bt_nodes::IsProcedureActive
  taskplanner_bt_nodes::IsPhaseCertain
  taskplanner_bt_nodes::HasExplicitRequest
  taskplanner_bt_nodes::HasImplicitRequest
  taskplanner_bt_nodes::NeedsRecovery
  taskplanner_bt_nodes::IsToolAvailable
  taskplanner_bt_nodes::CanHandover
  taskplanner_bt_nodes::CanPreposition
  taskplanner_bt_nodes::SelectExplicitTool
  taskplanner_bt_nodes::SelectImplicitTool
  taskplanner_bt_nodes::SelectExpectedTool
  taskplanner_bt_nodes::SelectRecoveryTool
  taskplanner_bt_nodes::SetIdleDecision
  taskplanner_bt_nodes::ApplyActionGuard
  taskplanner_bt_nodes::ConfigureHumanoidCommand
  taskplanner_bt_nodes::ShouldDispatchDecision
  taskplanner_bt_nodes::EmitBTDecision
  taskplanner_bt_nodes::PublishSkillCommand
  NODE_MANIFEST config/taskplanner_bt_nodes.yaml
)

install(TARGETS ${PROJECT_NAME}
  LIBRARY DESTINATION lib
)
install(DIRECTORY config/
  DESTINATION share/${PROJECT_NAME}/config
)

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(
    test_decision_boundary
    test/test_decision_boundary.py
  )
endif()

ament_package()
