#include <algorithm>
#include <cstdint>
#include <memory>
#include <sstream>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

#include "auto_apms_behavior_tree_core/node.hpp"
#include "auto_apms_behavior_tree_core/node/ros_publisher_node.hpp"
#include "auto_apms_behavior_tree_core/node/ros_subscriber_node.hpp"
#include "behaviortree_cpp/action_node.h"
#include "behaviortree_cpp/condition_node.h"
#include "behaviortree_cpp/tree_node.h"
#include "builtin_interfaces/msg/time.hpp"
#include "rclcpp/rclcpp.hpp"
#include "surgical_msgs/msg/bt_decision.hpp"
#include "surgical_msgs/msg/skill_command.hpp"
#include "surgical_msgs/msg/world_state.hpp"

namespace taskplanner_bt_nodes
{

using RosContext = auto_apms_behavior_tree::core::RosNodeContext;

namespace
{

template <typename T>
bool readBlackboard(const BT::TreeNode & node, const std::string & key, T & out)
{
  const auto blackboard = node.config().blackboard;
  return blackboard && blackboard->get(key, out);
}

template <typename T>
void writeBlackboard(const BT::TreeNode & node, const std::string & key, const T & value)
{
  const auto blackboard = node.config().blackboard;
  if (blackboard) {
    blackboard->set(key, value);
  }
}

std::string joinCsv(const std::vector<std::string> & items)
{
  std::ostringstream stream;
  for (size_t index = 0; index < items.size(); ++index) {
    if (index > 0) {
      stream << ",";
    }
    stream << items[index];
  }
  return stream.str();
}

std::vector<std::string> splitCsv(const std::string & value)
{
  std::vector<std::string> items;
  std::stringstream stream(value);
  std::string token;
  while (std::getline(stream, token, ',')) {
    if (!token.empty()) {
      items.push_back(token);
    }
  }
  return items;
}

std::string makeToolKey(const std::string & instrument_id, const std::string & suffix)
{
  return "tool." + instrument_id + "." + suffix;
}

bool isExplicitSurgeonIntent(const std::string & surgeon_intent)
{
  static const std::unordered_set<std::string> intents = {
    "request_tool", "voice_request", "extend_hand_for_handover"};
  return intents.count(surgeon_intent) > 0;
}

bool isAvailableStatus(const std::string & status)
{
  static const std::unordered_set<std::string> statuses = {"available", "prepared", "held"};
  return statuses.count(status) > 0;
}

std::string toolLifecycle(const BT::TreeNode & node, const std::string & tool_id)
{
  std::string value;
  readBlackboard(node, makeToolKey(tool_id, "lifecycle"), value);
  return value;
}

std::string toolNextRequiredTransition(const BT::TreeNode & node, const std::string & tool_id)
{
  std::string value;
  readBlackboard(node, makeToolKey(tool_id, "next_required_transition"), value);
  return value;
}

bool isSurgeonSideHoldingArea(const std::string & location_type)
{
  static const std::unordered_set<std::string> location_types = {
    "surgical_field", "surgeon_hand", "return_zone", "mayo_recovery_zone", "mayo_reuse_zone"};
  return location_types.count(location_type) > 0;
}

std::string firstInputOrBlackboard(BT::TreeNode & node, const std::string & port_key, const std::string & bb_key)
{
  if (const auto input = node.getInput<std::string>(port_key); input && !input.value().empty()) {
    return input.value();
  }
  std::string value;
  readBlackboard(node, bb_key, value);
  return value;
}

builtin_interfaces::msg::Time toBuiltinTime(const rclcpp::Time & time)
{
  builtin_interfaces::msg::Time msg;
  const auto nanoseconds = time.nanoseconds();
  msg.sec = static_cast<int32_t>(nanoseconds / 1000000000LL);
  msg.nanosec = static_cast<uint32_t>(nanoseconds % 1000000000LL);
  return msg;
}

void clearCommandFields(BT::TreeNode & node, bool clear_selected_tool)
{
  writeBlackboard(node, "bt.arm", std::string{});
  writeBlackboard(node, "bt.source_location_id", std::string{});
  writeBlackboard(node, "bt.source_location_type", std::string{});
  writeBlackboard(node, "bt.target_location_id", std::string{});
  writeBlackboard(node, "bt.target_location_type", std::string{});
  writeBlackboard(node, "bt.target_owner", std::string{});
  writeBlackboard(node, "bt.cleaning_required", false);
  writeBlackboard(node, "bt.mode", std::string{});
  if (clear_selected_tool) {
    writeBlackboard(node, "selected.tool", std::string{});
  }
}

std::vector<std::string> allTools(const BT::TreeNode & node)
{
  std::string csv;
  readBlackboard(node, "all_tools.csv", csv);
  return splitCsv(csv);
}

bool toolIsActive(const BT::TreeNode & node, const std::string & tool_id)
{
  if (tool_id.empty()) {
    return false;
  }
  bool active = false;
  readBlackboard(node, makeToolKey(tool_id, "active"), active);
  return active;
}

bool hasBlockingSafetyFlag(const BT::TreeNode & node)
{
  std::string safety_flags;
  readBlackboard(node, "safety.flags.csv", safety_flags);
  return !splitCsv(safety_flags).empty();
}

bool toolHasStatus(const BT::TreeNode & node, const std::string & tool_id, const std::string & status)
{
  if (!toolIsActive(node, tool_id)) {
    return false;
  }
  std::string value;
  readBlackboard(node, makeToolKey(tool_id, "status"), value);
  return value == status;
}

bool toolHasAnyStatus(
  const BT::TreeNode & node, const std::string & tool_id, const std::unordered_set<std::string> & statuses)
{
  if (!toolIsActive(node, tool_id)) {
    return false;
  }
  std::string value;
  readBlackboard(node, makeToolKey(tool_id, "status"), value);
  return statuses.count(value) > 0;
}

bool otherToolHasAnyStatus(
  const BT::TreeNode & node, const std::string & excluded_tool,
  const std::unordered_set<std::string> & statuses)
{
  for (const auto & tool_id : allTools(node)) {
    if (tool_id == excluded_tool) {
      continue;
    }
    if (toolHasAnyStatus(node, tool_id, statuses)) {
      return true;
    }
  }
  return false;
}

bool otherToolOccupiesSurgeonHand(const BT::TreeNode & node, const std::string & excluded_tool)
{
  for (const auto & tool_id : allTools(node)) {
    if (tool_id == excluded_tool) {
      continue;
    }
    if (!toolIsActive(node, tool_id)) {
      continue;
    }
    std::string status;
    std::string location_type;
    std::string owner;
    readBlackboard(node, makeToolKey(tool_id, "status"), status);
    readBlackboard(node, makeToolKey(tool_id, "location_type"), location_type);
    readBlackboard(node, makeToolKey(tool_id, "owner"), owner);
    if (status == "handed_over" || location_type == "surgeon_hand" || owner == "surgeon") {
      return true;
    }
  }
  return false;
}

bool toolIsRecoverableFromSurgeon(const BT::TreeNode & node, const std::string & tool_id)
{
  if (!toolIsActive(node, tool_id)) {
    return false;
  }
  const auto lifecycle = toolLifecycle(node, tool_id);
  return lifecycle == "surgeon_owned" || lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery" ||
         lifecycle == "recovering_left" || lifecycle == "cleaning_left" ||
         lifecycle == "cleaned_left";
}

bool toolIsAnticipatoryCandidate(const BT::TreeNode & node, const std::string & tool_id)
{
  if (!toolIsActive(node, tool_id)) {
    return false;
  }
  std::string status;
  bool contaminated = false;
  readBlackboard(node, makeToolKey(tool_id, "status"), status);
  readBlackboard(node, makeToolKey(tool_id, "contaminated"), contaminated);
  const auto lifecycle = toolLifecycle(node, tool_id);
  const auto next_required_transition = toolNextRequiredTransition(node, tool_id);

  if (contaminated) {
    return false;
  }
  if (!next_required_transition.empty()) {
    return false;
  }
  return lifecycle == "home_rack" || lifecycle == "returned_home";
}

bool hasRecoveryContext(const BT::TreeNode & node)
{
  bool required = false;
  bool ready_for_retrieval = false;
  bool cleaner_busy = false;
  std::string surgeon_intent;
  std::string surgeon_request_tool;
  std::string left_hand_tool;
  readBlackboard(node, "recovery.required", required);
  readBlackboard(node, "surgeon.ready_retrieval", ready_for_retrieval);
  readBlackboard(node, "cleaner.busy", cleaner_busy);
  readBlackboard(node, "surgeon.intent", surgeon_intent);
  readBlackboard(node, "request.surgeon_tool", surgeon_request_tool);
  readBlackboard(node, "robot.left_hand_tool", left_hand_tool);

  if (required || ready_for_retrieval || cleaner_busy || !left_hand_tool.empty()) {
    return true;
  }

  std::string pending_csv;
  readBlackboard(node, "pending_transition_tools.csv", pending_csv);
  if (!pending_csv.empty()) {
    for (const auto & tool_id : splitCsv(pending_csv)) {
      const auto next_required_transition = toolNextRequiredTransition(node, tool_id);
      if (
        next_required_transition == "recover_left" || next_required_transition == "clean_left" ||
        next_required_transition == "return_home")
      {
        return true;
      }
    }
  }

  return !surgeon_request_tool.empty() &&
         (surgeon_intent == "return_tool" || surgeon_intent == "extend_hand_for_retrieval" ||
          surgeon_intent == "awaiting_retrieval") &&
         toolIsRecoverableFromSurgeon(node, surgeon_request_tool);
}

bool hasActiveRobotTask(const BT::TreeNode & node)
{
  std::string task_id;
  readBlackboard(node, "robot.active_task_id", task_id);
  return !task_id.empty();
}

std::string findActiveLeftArmTool(const BT::TreeNode & node)
{
  for (const auto & tool_id : allTools(node)) {
    const auto lifecycle = toolLifecycle(node, tool_id);
    if (lifecycle == "recovering_left") {
      return tool_id;
    }
  }
  return {};
}

}  // namespace

class LoadWorldState : public auto_apms_behavior_tree::core::RosSubscriberNode<surgical_msgs::msg::WorldState>
{
public:
  explicit LoadWorldState(const std::string & name, const BT::NodeConfig & config, RosContext context)
  : auto_apms_behavior_tree::core::RosSubscriberNode<surgical_msgs::msg::WorldState>(
      name, config, context)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus onTick(const std::shared_ptr<surgical_msgs::msg::WorldState> & last_msg_ptr) override
  {
    if (last_msg_ptr) {
      last_world_state_ = last_msg_ptr;
    }
    if (!last_world_state_) {
      return BT::NodeStatus::FAILURE;
    }
    return applyWorldState(*last_world_state_);
  }

private:
  BT::NodeStatus applyWorldState(const surgical_msgs::msg::WorldState & msg)
  {
    if (msg.procedure_id != last_procedure_id_) {
      last_procedure_id_ = msg.procedure_id;
      ++bundle_generation_;
    }
    writeBlackboard(*this, "procedure.id", msg.procedure_id);
    writeBlackboard(*this, "bundle.generation", bundle_generation_);
    writeBlackboard(*this, "phase.id", msg.filtered_phase);
    writeBlackboard(*this, "runtime.running", static_cast<bool>(msg.running));
    writeBlackboard(*this, "runtime.execution_state", msg.execution_state);
    writeBlackboard(*this, "phase.confidence", static_cast<double>(msg.phase_confidence));
    writeBlackboard(*this, "phase.uncertain", static_cast<bool>(msg.phase_uncertain));
    writeBlackboard(*this, "phase.stability", static_cast<double>(msg.phase_stability));
    writeBlackboard(*this, "request.explicit_tool", msg.explicit_request_tool);
    writeBlackboard(*this, "request.surgeon_tool", msg.surgeon_request_tool);
    writeBlackboard(*this, "surgeon.intent", msg.surgeon_intent);
    writeBlackboard(*this, "surgeon.ready_handover", static_cast<bool>(msg.surgeon_ready_for_handover));
    writeBlackboard(*this, "surgeon.ready_retrieval", static_cast<bool>(msg.surgeon_ready_for_retrieval));
    writeBlackboard(*this, "robot.state", msg.robot_state);
    writeBlackboard(*this, "robot.right_hand_tool", msg.right_hand_tool);
    writeBlackboard(*this, "robot.left_hand_tool", msg.left_hand_tool);
    writeBlackboard(*this, "robot.prepositioned_tool", msg.prepositioned_tool);
    writeBlackboard(*this, "robot.active_task_id", msg.active_robot_task_id);
    writeBlackboard(*this, "robot.active_task_type", msg.active_robot_task_type);
    writeBlackboard(*this, "robot.active_task_tool_id", msg.active_robot_task_tool_id);
    writeBlackboard(*this, "robot.active_task_arm", msg.active_robot_task_arm);
    writeBlackboard(*this, "cleaner.busy", static_cast<bool>(msg.cleaner_busy));
    writeBlackboard(*this, "action.guard.handover_allowed", static_cast<bool>(msg.handover_allowed));
    writeBlackboard(*this, "recovery.required", static_cast<bool>(msg.recovery_required));
    writeBlackboard(*this, "safety.flags.csv", joinCsv(msg.safety_flags));
    writeBlackboard(*this, "pending_transition_tools.csv", joinCsv(msg.pending_transition_tools));
    writeBlackboard(*this, "active_recovery_tools.csv", joinCsv(msg.active_recovery_tools));
    writeBlackboard(*this, "expected_tools.csv", joinCsv(msg.expected_instruments));
    writeBlackboard(*this, "available_tools.csv", joinCsv(msg.available_instruments));
    std::vector<std::string> all_tools;
    all_tools.reserve(msg.instrument_states.size());
    std::unordered_set<std::string> active_tools;

    for (const auto & instrument : msg.instrument_states) {
      all_tools.push_back(instrument.instrument_id);
      active_tools.insert(instrument.instrument_id);
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "active"), true);
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "home_location"), instrument.home_location_id);
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "home_type"), instrument.home_location_type);
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "status"), instrument.status);
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "location"), instrument.location_id);
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "location_type"), instrument.location_type);
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "owner"), instrument.owner);
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "available"), isAvailableStatus(instrument.status));
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "contaminated"), static_cast<bool>(instrument.contaminated));
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "cleanliness"), instrument.cleanliness_state);
      writeBlackboard(*this, makeToolKey(instrument.instrument_id, "lifecycle"), instrument.lifecycle_stage);
      writeBlackboard(
        *this, makeToolKey(instrument.instrument_id, "next_required_transition"),
        instrument.next_required_transition);
    }
    for (const auto & tool_id : previous_tools_) {
      if (active_tools.count(tool_id) > 0) {
        continue;
      }
      writeBlackboard(*this, makeToolKey(tool_id, "active"), false);
      writeBlackboard(*this, makeToolKey(tool_id, "available"), false);
    }
    previous_tools_ = std::move(active_tools);
    writeBlackboard(*this, "all_tools.csv", joinCsv(all_tools));
    return BT::NodeStatus::SUCCESS;
  }

  std::shared_ptr<surgical_msgs::msg::WorldState> last_world_state_;
  std::string last_procedure_id_;
  int64_t bundle_generation_ = 0;
  std::unordered_set<std::string> previous_tools_;
};

class IsProcedureActive : public BT::ConditionNode
{
public:
  explicit IsProcedureActive(const std::string & name, const BT::NodeConfig & config)
  : BT::ConditionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    std::string execution_state;
    readBlackboard(*this, "runtime.execution_state", execution_state);
    if (execution_state == "completed") {
      return BT::NodeStatus::FAILURE;
    }
    return BT::NodeStatus::SUCCESS;
  }
};

class IsPhaseCertain : public BT::ConditionNode
{
public:
  explicit IsPhaseCertain(const std::string & name, const BT::NodeConfig & config)
  : BT::ConditionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    bool uncertain = true;
    readBlackboard(*this, "phase.uncertain", uncertain);
    return uncertain ? BT::NodeStatus::FAILURE : BT::NodeStatus::SUCCESS;
  }
};

class HasExplicitRequest : public BT::ConditionNode
{
public:
  explicit HasExplicitRequest(const std::string & name, const BT::NodeConfig & config)
  : BT::ConditionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    std::string explicit_request;
    std::string surgeon_request;
    std::string surgeon_intent;
    readBlackboard(*this, "request.explicit_tool", explicit_request);
    readBlackboard(*this, "request.surgeon_tool", surgeon_request);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    if (!isExplicitSurgeonIntent(surgeon_intent)) {
      surgeon_request.clear();
    }
    return (explicit_request.empty() && surgeon_request.empty()) ?
      BT::NodeStatus::FAILURE : BT::NodeStatus::SUCCESS;
  }
};

class NeedsRecovery : public BT::ConditionNode
{
public:
  explicit NeedsRecovery(const std::string & name, const BT::NodeConfig & config)
  : BT::ConditionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    return hasRecoveryContext(*this) ? BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
  }
};

class IsToolAvailable : public BT::ConditionNode
{
public:
  explicit IsToolAvailable(const std::string & name, const BT::NodeConfig & config)
  : BT::ConditionNode(name, config)
  {
  }

  static BT::PortsList providedPorts()
  {
    return {BT::InputPort<std::string>("tool_id", "Explicit tool id to validate.")};
  }

  BT::NodeStatus tick() override
  {
    const auto tool_id = firstInputOrBlackboard(*this, "tool_id", "selected.tool");
    if (tool_id.empty()) {
      return BT::NodeStatus::FAILURE;
    }
    if (!toolIsActive(*this, tool_id)) {
      return BT::NodeStatus::FAILURE;
    }
    const auto lifecycle = toolLifecycle(*this, tool_id);
    bool contaminated = false;
    readBlackboard(*this, makeToolKey(tool_id, "contaminated"), contaminated);
    const bool immediately_usable =
      lifecycle == "home_rack" || lifecycle == "returned_home" || lifecycle == "prepositioned_right";
    const bool already_surgeon_side = lifecycle == "surgeon_owned" || lifecycle == "mayo_reuse";
    if (already_surgeon_side) {
      return BT::NodeStatus::SUCCESS;
    }
    return immediately_usable && !contaminated ? BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
  }
};

class CanHandover : public BT::ConditionNode
{
public:
  explicit CanHandover(const std::string & name, const BT::NodeConfig & config)
  : BT::ConditionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    if (hasBlockingSafetyFlag(*this)) {
      return BT::NodeStatus::FAILURE;
    }
    if (hasActiveRobotTask(*this)) {
      return BT::NodeStatus::FAILURE;
    }
    std::string selected_tool;
    readBlackboard(*this, "selected.tool", selected_tool);
    if (!toolIsActive(*this, selected_tool)) {
      return BT::NodeStatus::FAILURE;
    }
    if (!selected_tool.empty() && otherToolOccupiesSurgeonHand(*this, selected_tool)) {
      return BT::NodeStatus::FAILURE;
    }
    const auto lifecycle = toolLifecycle(*this, selected_tool);
    if (lifecycle == "surgeon_owned" || lifecycle == "mayo_reuse") {
      return BT::NodeStatus::SUCCESS;
    }
    bool allowed = false;
    readBlackboard(*this, "action.guard.handover_allowed", allowed);
    return allowed ? BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
  }
};

class SelectExplicitTool : public BT::SyncActionNode
{
public:
  explicit SelectExplicitTool(
    const std::string & name, const BT::NodeConfig & config, [[maybe_unused]] RosContext context)
  : BT::SyncActionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    std::string tool_id;
    std::string surgeon_intent;
    std::string surgeon_tool;
    bool ready_for_retrieval = false;
    readBlackboard(*this, "request.explicit_tool", tool_id);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    readBlackboard(*this, "request.surgeon_tool", surgeon_tool);
    readBlackboard(*this, "surgeon.ready_retrieval", ready_for_retrieval);
    if (tool_id.empty() && isExplicitSurgeonIntent(surgeon_intent)) {
      tool_id = surgeon_tool;
    }
    if (tool_id.empty() && !surgeon_tool.empty() && !ready_for_retrieval) {
      tool_id = surgeon_tool;
    }
    if (tool_id.empty()) {
      return BT::NodeStatus::FAILURE;
    }
    if (!toolIsActive(*this, tool_id)) {
      return BT::NodeStatus::FAILURE;
    }
    writeBlackboard(*this, "selected.tool", tool_id);
    return BT::NodeStatus::SUCCESS;
  }
};

class SelectExpectedTool : public BT::SyncActionNode
{
public:
  explicit SelectExpectedTool(
    const std::string & name, const BT::NodeConfig & config, [[maybe_unused]] RosContext context)
  : BT::SyncActionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    std::string execution_state;
    readBlackboard(*this, "runtime.execution_state", execution_state);
    if (execution_state != "running") {
      return BT::NodeStatus::FAILURE;
    }
    std::string explicit_request;
    std::string surgeon_request;
    std::string surgeon_intent;
    readBlackboard(*this, "request.explicit_tool", explicit_request);
    readBlackboard(*this, "request.surgeon_tool", surgeon_request);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    std::string prepositioned_tool;
    readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
    const bool active_return_intent =
      !surgeon_request.empty() &&
      (surgeon_intent == "return_tool" || surgeon_intent == "extend_hand_for_retrieval");
    if (
      hasRecoveryContext(*this) || !explicit_request.empty() || !surgeon_request.empty() ||
      active_return_intent || hasActiveRobotTask(*this) || !prepositioned_tool.empty())
    {
      return BT::NodeStatus::FAILURE;
    }

    std::string expected_csv;
    readBlackboard(*this, "expected_tools.csv", expected_csv);
    const auto expected_tools = splitCsv(expected_csv);
    for (const auto & tool_id : expected_tools) {
      if (toolIsAnticipatoryCandidate(*this, tool_id)) {
        writeBlackboard(*this, "selected.tool", tool_id);
        return BT::NodeStatus::SUCCESS;
      }
    }
    std::string available_csv;
    readBlackboard(*this, "available_tools.csv", available_csv);
    const auto available_tools = splitCsv(available_csv);
    for (const auto & tool_id : expected_tools) {
      if (
        toolIsActive(*this, tool_id) &&
        std::find(available_tools.begin(), available_tools.end(), tool_id) != available_tools.end())
      {
        writeBlackboard(*this, "selected.tool", tool_id);
        return BT::NodeStatus::SUCCESS;
      }
    }
    return BT::NodeStatus::FAILURE;
  }
};

class SelectRecoveryTool : public BT::SyncActionNode
{
public:
  explicit SelectRecoveryTool(
    const std::string & name, const BT::NodeConfig & config, [[maybe_unused]] RosContext context)
  : BT::SyncActionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    for (const auto & tool_id : allTools(*this)) {
      if (toolNextRequiredTransition(*this, tool_id) == "recover_left") {
        return selectTool(tool_id);
      }
    }

    std::string surgeon_request_tool;
    std::string surgeon_intent;
    readBlackboard(*this, "request.surgeon_tool", surgeon_request_tool);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    if (!surgeon_request_tool.empty()) {
      const auto lifecycle = toolLifecycle(*this, surgeon_request_tool);
      if (
        toolIsActive(*this, surgeon_request_tool) &&
        (lifecycle == "mayo_recovery" || lifecycle == "mayo_reuse"))
      {
        return selectTool(surgeon_request_tool);
      }
    }

    std::string active_recovery_csv;
    readBlackboard(*this, "active_recovery_tools.csv", active_recovery_csv);
    for (const auto & tool_id : splitCsv(active_recovery_csv)) {
      const auto lifecycle = toolLifecycle(*this, tool_id);
      if (toolIsActive(*this, tool_id) && (lifecycle == "mayo_recovery" || lifecycle == "mayo_reuse")) {
        return selectTool(tool_id);
      }
    }

    for (const auto & tool_id : allTools(*this)) {
      const auto lifecycle = toolLifecycle(*this, tool_id);
      if (lifecycle == "mayo_recovery") {
        return selectTool(tool_id);
      }
    }

    return BT::NodeStatus::FAILURE;
  }

private:
  BT::NodeStatus selectTool(const std::string & tool_id)
  {
    std::string home_location_id;
    std::string home_location_type;
    readBlackboard(*this, makeToolKey(tool_id, "home_location"), home_location_id);
    readBlackboard(*this, makeToolKey(tool_id, "home_type"), home_location_type);
    writeBlackboard(*this, "selected.tool", tool_id);
    writeBlackboard(*this, "bt.target_location_id", home_location_id);
    writeBlackboard(*this, "bt.target_location_type", home_location_type);
    return BT::NodeStatus::SUCCESS;
  }
};

class SetIdleDecision : public BT::SyncActionNode
{
public:
  explicit SetIdleDecision(
    const std::string & name, const BT::NodeConfig & config, [[maybe_unused]] RosContext context)
  : BT::SyncActionNode(name, config)
  {
  }

  static BT::PortsList providedPorts()
  {
    return {
      BT::InputPort<std::string>("decision", "Decision label to mirror."),
      BT::InputPort<std::string>("action", "Action label to mirror."),
      BT::InputPort<std::string>("rationale", "Rationale text for the decision."),
      BT::InputPort<bool>("clear_selected_tool", true, "Whether to clear the selected.tool blackboard entry."),
    };
  }

  BT::NodeStatus tick() override
  {
    auto decision = firstInputOrBlackboard(*this, "decision", "bt.decision");
    std::string action;
    if (const auto input = getInput<std::string>("action")) {
      action = input.value();
    } else {
      readBlackboard(*this, "bt.action", action);
    }
    auto rationale = firstInputOrBlackboard(*this, "rationale", "bt.rationale");
    std::string explicit_request;
    std::string surgeon_request;
    std::string surgeon_intent;
    readBlackboard(*this, "request.explicit_tool", explicit_request);
    readBlackboard(*this, "request.surgeon_tool", surgeon_request);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    const bool higher_priority_pending =
      decision == "idle" &&
      (
        hasRecoveryContext(*this) || !explicit_request.empty() || !surgeon_request.empty() ||
        surgeon_intent == "return_tool" || surgeon_intent == "extend_hand_for_retrieval");
    if (higher_priority_pending) {
      decision = "hold";
      action = "";
      rationale = "higher-priority request or recovery context still pending";
      writeBlackboard(*this, "bt.mode", std::string("guard_wait"));
    }
    writeBlackboard(*this, "bt.decision", decision);
    writeBlackboard(*this, "bt.action", action);
    writeBlackboard(*this, "bt.rationale", rationale);
    writeBlackboard(*this, "bt.decision_reason", rationale);
    writeBlackboard(
      *this, "bt.blocking_guard",
      higher_priority_pending ? std::string("pending_transition") : std::string{});
    writeBlackboard(*this, "bt.selected_tool_lifecycle", std::string{});
    writeBlackboard(*this, "bt.next_required_transition", std::string{});
    bool clear_selected_tool = true;
    if (const auto input = getInput<bool>("clear_selected_tool")) {
      clear_selected_tool = input.value();
    }
    clearCommandFields(*this, clear_selected_tool);
    return BT::NodeStatus::SUCCESS;
  }
};

class ApplyActionGuard : public BT::SyncActionNode
{
public:
  explicit ApplyActionGuard(
    const std::string & name, const BT::NodeConfig & config, [[maybe_unused]] RosContext context)
  : BT::SyncActionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    const auto selected_tool = firstInputOrBlackboard(*this, "tool_id", "selected.tool");
    bool world_allowed = false;
    bool uncertain = true;
    std::string robot_state;
    std::string active_task_id;
    std::string explicit_request;
    std::string surgeon_request;
    std::string surgeon_intent;
    std::string owner;
    const auto lifecycle = toolLifecycle(*this, selected_tool);
    bool contaminated = false;
    bool cleaner_busy = false;
    bool ready_for_handover = false;
    readBlackboard(*this, "action.guard.handover_allowed", world_allowed);
    readBlackboard(*this, "phase.uncertain", uncertain);
    readBlackboard(*this, "robot.state", robot_state);
    readBlackboard(*this, "robot.active_task_id", active_task_id);
    readBlackboard(*this, "request.explicit_tool", explicit_request);
    readBlackboard(*this, "request.surgeon_tool", surgeon_request);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    readBlackboard(*this, "surgeon.ready_handover", ready_for_handover);
    readBlackboard(*this, "cleaner.busy", cleaner_busy);
    readBlackboard(*this, makeToolKey(selected_tool, "contaminated"), contaminated);
    readBlackboard(*this, makeToolKey(selected_tool, "owner"), owner);
    const bool explicit_request_selected =
      isExplicitSurgeonIntent(surgeon_intent) &&
      (!selected_tool.empty()) &&
      (selected_tool == explicit_request || selected_tool == surgeon_request);
    const bool active_tool = toolIsActive(*this, selected_tool);
    const bool prepositioned_right = lifecycle == "prepositioned_right";
    const bool holder_available = owner.empty() || owner == "none" || (prepositioned_right && owner == "robot_right_hand");
    const bool usable_lifecycle = lifecycle == "home_rack" || lifecycle == "returned_home" || prepositioned_right;
    const bool surgeon_side = lifecycle == "surgeon_owned" || lifecycle == "mayo_reuse";
    const bool blocked_by_safety = hasBlockingSafetyFlag(*this);

    const bool allowed =
      active_tool &&
      !blocked_by_safety &&
      (
        surgeon_side ||
        (
          usable_lifecycle && holder_available && !contaminated && active_task_id.empty() &&
          !cleaner_busy && !uncertain && robot_state != "fault" &&
          ((explicit_request_selected && ready_for_handover) || (!explicit_request_selected && world_allowed))
        )
      );

    writeBlackboard(*this, "action.guard.handover_allowed", allowed);
    return BT::NodeStatus::SUCCESS;
  }
};

class ConfigureHumanoidCommand : public BT::SyncActionNode
{
public:
  explicit ConfigureHumanoidCommand(
    const std::string & name, const BT::NodeConfig & config, [[maybe_unused]] RosContext context)
  : BT::SyncActionNode(name, config)
  {
  }

  static BT::PortsList providedPorts()
  {
    return {
      BT::InputPort<std::string>("decision", "Decision label to mirror."),
      BT::InputPort<std::string>("mode", "Humanoid execution mode."),
      BT::InputPort<std::string>("rationale", "Decision rationale."),
    };
  }

  BT::NodeStatus tick() override
  {
    auto decision = firstInputOrBlackboard(*this, "decision", "bt.decision");
    auto mode = firstInputOrBlackboard(*this, "mode", "bt.mode");
    auto rationale = firstInputOrBlackboard(*this, "rationale", "bt.rationale");
    std::string selected_tool;
    readBlackboard(*this, "selected.tool", selected_tool);
    const auto lifecycle = toolLifecycle(*this, selected_tool);
    const auto next_required_transition = toolNextRequiredTransition(*this, selected_tool);

    writeBlackboard(*this, "bt.decision", decision);
    writeBlackboard(*this, "bt.rationale", rationale);
    writeBlackboard(*this, "bt.decision_reason", rationale);
    writeBlackboard(*this, "bt.blocking_guard", std::string{});
    writeBlackboard(*this, "bt.mode", mode);
    writeBlackboard(*this, "bt.target_owner", std::string{});
    writeBlackboard(*this, "bt.cleaning_required", false);
    writeBlackboard(*this, "bt.arm", std::string{});
    writeBlackboard(*this, "bt.selected_tool_lifecycle", lifecycle);
    writeBlackboard(*this, "bt.next_required_transition", next_required_transition);

    if (mode == "safety") {
      writeBlackboard(*this, "bt.action", std::string{});
      writeBlackboard(*this, "bt.blocking_guard", std::string("phase_uncertain"));
      clearCommandFields(*this, true);
      writeBlackboard(*this, "bt.mode", mode);
      return BT::NodeStatus::SUCCESS;
    }

    if (mode == "idle") {
      std::string explicit_request;
      std::string surgeon_request;
      std::string surgeon_intent;
      readBlackboard(*this, "request.explicit_tool", explicit_request);
      readBlackboard(*this, "request.surgeon_tool", surgeon_request);
      readBlackboard(*this, "surgeon.intent", surgeon_intent);
      const bool higher_priority_pending =
        hasRecoveryContext(*this) || !explicit_request.empty() || !surgeon_request.empty() ||
        surgeon_intent == "return_tool" || surgeon_intent == "extend_hand_for_retrieval";
      if (higher_priority_pending) {
        decision = "hold";
        mode = "guard_wait";
        rationale = "higher-priority request or recovery context still pending";
        writeBlackboard(*this, "bt.blocking_guard", std::string("pending_transition"));
      } else {
        writeBlackboard(*this, "bt.action", std::string{});
      }
      writeBlackboard(*this, "bt.decision", decision);
      writeBlackboard(*this, "bt.rationale", rationale);
      writeBlackboard(*this, "bt.decision_reason", rationale);
      clearCommandFields(*this, true);
      writeBlackboard(*this, "bt.mode", mode);
      return BT::NodeStatus::SUCCESS;
    }

    if (selected_tool.empty()) {
      return BT::NodeStatus::FAILURE;
    }
    if (!toolIsActive(*this, selected_tool)) {
      writeBlackboard(*this, "bt.blocking_guard", std::string("inactive_tool"));
      return BT::NodeStatus::FAILURE;
    }

    std::string tool_location;
    std::string tool_location_type;
    std::string tool_status;
    std::string home_location_id;
    std::string home_location_type;
    bool contaminated = false;
    readBlackboard(*this, makeToolKey(selected_tool, "location"), tool_location);
    readBlackboard(*this, makeToolKey(selected_tool, "location_type"), tool_location_type);
    readBlackboard(*this, makeToolKey(selected_tool, "status"), tool_status);
    readBlackboard(*this, makeToolKey(selected_tool, "home_location"), home_location_id);
    readBlackboard(*this, makeToolKey(selected_tool, "home_type"), home_location_type);
    readBlackboard(*this, makeToolKey(selected_tool, "contaminated"), contaminated);
    std::string right_hand_tool;
    readBlackboard(*this, "robot.right_hand_tool", right_hand_tool);

    if (mode == "explicit_request") {
      if (lifecycle == "mayo_recovery" || (contaminated && lifecycle != "surgeon_owned" && lifecycle != "mayo_reuse")) {
        return BT::NodeStatus::FAILURE;
      }
      if (lifecycle == "surgeon_owned" || lifecycle == "mayo_reuse") {
        writeBlackboard(*this, "bt.action", std::string{});
        writeBlackboard(*this, "bt.decision_reason", std::string("requested tool already surgeon-side"));
        writeBlackboard(*this, "bt.rationale", std::string("requested tool already surgeon-side"));
        clearCommandFields(*this, false);
        writeBlackboard(*this, "bt.mode", std::string("explicit_fulfilled"));
        return BT::NodeStatus::SUCCESS;
      }
      if (right_hand_tool == selected_tool || lifecycle == "prepositioned_right") {
        writeBlackboard(*this, "bt.action", std::string("direct_handover"));
        writeBlackboard(*this, "bt.source_location_id", std::string("robot_right_hand"));
        writeBlackboard(*this, "bt.source_location_type", std::string("robot_right_hand"));
      } else if (!right_hand_tool.empty()) {
        writeBlackboard(*this, "bt.action", std::string("put_down_and_handover"));
        writeBlackboard(
          *this, "bt.source_location_id",
          tool_location.empty() ? home_location_id : tool_location);
        writeBlackboard(
          *this, "bt.source_location_type",
          tool_location_type.empty() ? home_location_type : tool_location_type);
      } else {
        writeBlackboard(*this, "bt.action", std::string("pick_up_and_handover"));
        writeBlackboard(
          *this, "bt.source_location_id",
          tool_location.empty() ? home_location_id : tool_location);
        writeBlackboard(
          *this, "bt.source_location_type",
          tool_location_type.empty() ? home_location_type : tool_location_type);
      }
      writeBlackboard(*this, "bt.arm", std::string("right"));
      writeBlackboard(*this, "bt.target_location_id", std::string("surgeon_receive_zone"));
      writeBlackboard(*this, "bt.target_location_type", std::string("handover_zone"));
      writeBlackboard(*this, "bt.target_owner", std::string("surgeon"));
      writeBlackboard(*this, "bt.cleaning_required", false);
      return BT::NodeStatus::SUCCESS;
    }

    if (mode == "anticipatory") {
      if (lifecycle != "home_rack" && lifecycle != "returned_home") {
        return BT::NodeStatus::FAILURE;
      }
      writeBlackboard(*this, "bt.action", std::string("predict_tool"));
      writeBlackboard(*this, "bt.arm", std::string("right"));
      writeBlackboard(*this, "bt.source_location_id", tool_location);
      writeBlackboard(*this, "bt.source_location_type", tool_location_type);
      writeBlackboard(*this, "bt.target_location_id", std::string("robot_right_hand"));
      writeBlackboard(*this, "bt.target_location_type", std::string("robot_right_hand"));
      writeBlackboard(*this, "bt.target_owner", std::string("robot_right_hand"));
      writeBlackboard(*this, "bt.cleaning_required", false);
      return BT::NodeStatus::SUCCESS;
    }

    if (mode == "recovery") {
      if (
        (lifecycle != "mayo_recovery" && lifecycle != "mayo_reuse") ||
        next_required_transition != "recover_left")
      {
        return BT::NodeStatus::FAILURE;
      }
      const auto recovery_source_location = lifecycle == "mayo_reuse" ?
        std::string("mayo_reuse_zone") : std::string("mayo_recovery_zone");
      writeBlackboard(*this, "bt.action", std::string("retrieve_from_mayo"));
      writeBlackboard(*this, "bt.arm", std::string("left"));
      writeBlackboard(*this, "bt.source_location_id", tool_location.empty() ? recovery_source_location : tool_location);
      writeBlackboard(
        *this, "bt.source_location_type",
        tool_location_type.empty() ? recovery_source_location : tool_location_type);
      writeBlackboard(*this, "bt.target_location_id", home_location_id);
      writeBlackboard(*this, "bt.target_location_type", home_location_type);
      writeBlackboard(*this, "bt.target_owner", std::string("none"));
      writeBlackboard(*this, "bt.cleaning_required", true);
      writeBlackboard(*this, "bt.decision_reason", std::string("mayo stand tool requires retrieve action"));
      writeBlackboard(*this, "bt.rationale", std::string("mayo stand tool requires retrieve action"));
      return BT::NodeStatus::SUCCESS;
    }

    return BT::NodeStatus::FAILURE;
  }
};

class ShouldDispatchDecision : public BT::SyncActionNode
{
public:
  explicit ShouldDispatchDecision(const std::string & name, const BT::NodeConfig & config, RosContext context)
  : BT::SyncActionNode(name, config),
    context_(std::move(context))
  {
  }

  static BT::PortsList providedPorts()
  {
    return {
      BT::InputPort<std::string>("decision", "Decision label override used for dispatch gating."),
      BT::InputPort<std::string>("action", "Action label override used for dispatch gating."),
      BT::InputPort<std::string>("rationale", "Rationale override used for dispatch gating."),
      BT::InputPort<std::string>("target_location_id", "Optional target location id used for dispatch gating."),
      BT::InputPort<std::string>("target_location_type", "Optional target location type used for dispatch gating."),
      BT::InputPort<std::string>("mode", "Optional execution mode used for dispatch gating."),
      BT::InputPort<std::string>("arm", "Optional arm used for dispatch gating."),
      BT::InputPort<std::string>("selected_tool_lifecycle", "Optional lifecycle used for dispatch gating."),
      BT::InputPort<std::string>("next_required_transition", "Optional lifecycle transition used for dispatch gating."),
      BT::InputPort<double>("cooldown_sec", 2.0, "Minimum interval before re-dispatching the same intent."),
    };
  }

  BT::NodeStatus tick() override
  {
    const auto decision = firstInputOrBlackboard(*this, "decision", "bt.decision");
    const auto action = firstInputOrBlackboard(*this, "action", "bt.action");
    const auto rationale = firstInputOrBlackboard(*this, "rationale", "bt.rationale");
    const auto target_location_id =
      firstInputOrBlackboard(*this, "target_location_id", "bt.target_location_id");
    const auto target_location_type =
      firstInputOrBlackboard(*this, "target_location_type", "bt.target_location_type");
    const auto mode = firstInputOrBlackboard(*this, "mode", "bt.mode");
    const auto arm = firstInputOrBlackboard(*this, "arm", "bt.arm");
    const auto selected_tool_lifecycle =
      firstInputOrBlackboard(*this, "selected_tool_lifecycle", "bt.selected_tool_lifecycle");
    const auto next_required_transition =
      firstInputOrBlackboard(*this, "next_required_transition", "bt.next_required_transition");

    std::string selected_tool;
    readBlackboard(*this, "selected.tool", selected_tool);

    double cooldown_sec = 2.0;
    if (const auto input = getInput<double>("cooldown_sec")) {
      cooldown_sec = std::max(0.0, input.value());
    }

    if (hasActiveRobotTask(*this)) {
      return BT::NodeStatus::FAILURE;
    }

    const auto signature = makeSignature(
      decision, action, rationale, selected_tool, selected_tool_lifecycle, next_required_transition,
      target_location_id, target_location_type, mode, arm);

    std::string last_signature;
    int64_t last_time_ns = 0;
    readBlackboard(*this, "dispatch.last_signature", last_signature);
    readBlackboard(*this, "dispatch.last_time_ns", last_time_ns);

    const auto now_ns = context_.getCurrentTime().nanoseconds();
    const bool signature_changed = signature != last_signature;
    const bool cooldown_expired =
      last_time_ns == 0 || static_cast<double>(now_ns - last_time_ns) / 1e9 >= cooldown_sec;

    if (!signature_changed && !cooldown_expired) {
      return BT::NodeStatus::FAILURE;
    }

    writeBlackboard(*this, "dispatch.last_signature", signature);
    writeBlackboard(*this, "dispatch.last_time_ns", now_ns);
    return BT::NodeStatus::SUCCESS;
  }

private:
  static std::string makeSignature(
    const std::string & decision, const std::string & action, const std::string & rationale,
    const std::string & selected_tool, const std::string & selected_tool_lifecycle,
    const std::string & next_required_transition, const std::string & target_location_id,
    const std::string & target_location_type, const std::string & mode, const std::string & arm)
  {
    std::ostringstream stream;
    stream << decision << "|" << action << "|" << rationale << "|" << selected_tool << "|" <<
      selected_tool_lifecycle << "|" << next_required_transition << "|" << target_location_id << "|" <<
      target_location_type << "|" << mode << "|" << arm;
    return stream.str();
  }

  RosContext context_;
};

class EmitBTDecision
: public auto_apms_behavior_tree::core::RosPublisherNode<surgical_msgs::msg::BTDecision>
{
public:
  explicit EmitBTDecision(const std::string & name, const BT::NodeConfig & config, RosContext context)
  : auto_apms_behavior_tree::core::RosPublisherNode<surgical_msgs::msg::BTDecision>(
      name, config, std::move(context))
  {
  }

  static BT::PortsList providedPorts()
  {
    return providedBasicPorts(
      {
        BT::InputPort<std::string>("decision", "Decision label override."),
        BT::InputPort<std::string>("action", "Action label override."),
        BT::InputPort<std::string>("rationale", "Decision rationale override."),
      });
  }

  bool setMessage(surgical_msgs::msg::BTDecision & msg) override
  {
    msg.stamp = toBuiltinTime(context_.getCurrentTime());
    msg.decision = firstInputOrBlackboard(*this, "decision", "bt.decision");
    msg.action = firstInputOrBlackboard(*this, "action", "bt.action");
    msg.rationale = firstInputOrBlackboard(*this, "rationale", "bt.rationale");
    readBlackboard(*this, "selected.tool", msg.selected_tool);
    readBlackboard(*this, "bt.selected_tool_lifecycle", msg.selected_tool_lifecycle);
    readBlackboard(*this, "bt.next_required_transition", msg.next_required_transition);
    readBlackboard(*this, "bt.decision_reason", msg.decision_reason);
    readBlackboard(*this, "bt.blocking_guard", msg.blocking_guard);
    readBlackboard(*this, "action.guard.handover_allowed", msg.handover_allowed);
    if (msg.decision.empty()) {
      msg.decision = "idle";
    }
    return true;
  }
};

class PublishSkillCommand
: public auto_apms_behavior_tree::core::RosPublisherNode<surgical_msgs::msg::SkillCommand>
{
public:
  explicit PublishSkillCommand(const std::string & name, const BT::NodeConfig & config, RosContext context)
  : auto_apms_behavior_tree::core::RosPublisherNode<surgical_msgs::msg::SkillCommand>(
      name, config, std::move(context))
  {
  }

  static BT::PortsList providedPorts()
  {
    return providedBasicPorts(
      {
        BT::InputPort<std::string>("action", "Skill action override."),
        BT::InputPort<std::string>("rationale", "Skill rationale override."),
        BT::InputPort<std::string>("target_location_id", "Optional target location."),
        BT::InputPort<std::string>("target_location_type", "Optional target location type."),
        BT::InputPort<std::string>("arm", "Optional arm override."),
        BT::InputPort<std::string>("source_location_id", "Optional source location override."),
        BT::InputPort<std::string>("source_location_type", "Optional source location type override."),
        BT::InputPort<std::string>("target_owner", "Optional target owner override."),
        BT::InputPort<std::string>("mode", "Optional execution mode override."),
      });
  }

  bool setMessage(surgical_msgs::msg::SkillCommand & msg) override
  {
    msg.stamp = toBuiltinTime(context_.getCurrentTime());
    msg.action = firstInputOrBlackboard(*this, "action", "bt.action");
    msg.rationale = firstInputOrBlackboard(*this, "rationale", "bt.rationale");
    msg.target_location_id = firstInputOrBlackboard(*this, "target_location_id", "bt.target_location_id");
    msg.target_location_type =
      firstInputOrBlackboard(*this, "target_location_type", "bt.target_location_type");
    msg.arm = firstInputOrBlackboard(*this, "arm", "bt.arm");
    msg.source_location_id = firstInputOrBlackboard(*this, "source_location_id", "bt.source_location_id");
    msg.source_location_type = firstInputOrBlackboard(*this, "source_location_type", "bt.source_location_type");
    msg.target_owner = firstInputOrBlackboard(*this, "target_owner", "bt.target_owner");
    msg.mode = firstInputOrBlackboard(*this, "mode", "bt.mode");
    readBlackboard(*this, "selected.tool", msg.instrument_id);
    readBlackboard(*this, "bt.cleaning_required", msg.cleaning_required);
    msg.command_id = std::to_string(context_.getCurrentTime().nanoseconds());
    return !msg.action.empty();
  }
};

}  // namespace taskplanner_bt_nodes

AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::LoadWorldState)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::IsProcedureActive)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::IsPhaseCertain)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::HasExplicitRequest)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::NeedsRecovery)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::IsToolAvailable)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::CanHandover)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::SelectExplicitTool)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::SelectExpectedTool)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::SelectRecoveryTool)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::SetIdleDecision)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::ApplyActionGuard)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::ConfigureHumanoidCommand)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::ShouldDispatchDecision)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::EmitBTDecision)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::PublishSkillCommand)
