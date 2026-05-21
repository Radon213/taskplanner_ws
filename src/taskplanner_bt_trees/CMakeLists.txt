cmake_minimum_required(VERSION 3.16)
project(taskplanner_bt_trees)

find_package(ament_cmake REQUIRED)
find_package(auto_apms_behavior_tree REQUIRED)
find_package(taskplanner_bt_nodes REQUIRED)

auto_apms_behavior_tree_register_trees(
  behavior/surgical_assist_v1.xml
  NODE_MANIFEST taskplanner_bt_nodes::taskplanner_bt_nodes
)

ament_package()
