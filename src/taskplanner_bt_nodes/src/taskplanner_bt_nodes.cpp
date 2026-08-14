#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
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

std::atomic<uint64_t> skill_command_sequence{0};

// Preparing a tool is reversible and lower risk than handing it to the surgeon.
// A candidate that remains the reducer's winner across several BT ticks may
// occupy the single right-hand preparation slot; handover keeps stricter guards.
constexpr double kPreparationMinConfidence = 0.65;
constexpr double kPreparationMinStabilitySec = 0.3;
constexpr double kImplicitGestureMinConfidence = 0.8;
constexpr double kImplicitGestureMinStabilitySec = 0.7;
// The reducer already withdraws stale prediction evidence. Keep only a short
// BT-side grace period so a transient blackboard update does not cause churn.
constexpr double kPreparationUnsupportedGraceSec = 0.8;
// A speculative preparation must not monopolize the right hand indefinitely.
// Explicit requests bypass this limit and can still hand over the held tool.
constexpr double kPreparationMaxDwellSec = 6.0;
// A high-confidence prediction that remains the reducer's current winner may
// be held across a longer surgical maneuver. A replacement prediction still
// releases it through the faster reversible replacement branch.
constexpr double kPreparationStrongConfidence = 0.85;
constexpr double kPreparationStrongMaxDwellSec = 30.0;
// A returned candidate is re-armed only after it has remained absent from the
// prediction stream for this long. Merely waiting while the same unstable
// candidate keeps reappearing must not trigger repeated robot motion.
constexpr double kPreparationRetryCooldownSec = 5.0;

double steadyNowSec()
{
  return std::chrono::duration<double>(
    std::chrono::steady_clock::now().time_since_epoch()).count();
}

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
    "request_tool", "voice_request"};
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

std::string toolTypeId(const BT::TreeNode & node, const std::string & tool_id)
{
  std::string value;
  readBlackboard(node, makeToolKey(tool_id, "type_id"), value);
  if (!value.empty()) {
    return value;
  }
  const auto separator = tool_id.find('#');
  return separator == std::string::npos ? tool_id : tool_id.substr(0, separator);
}

bool toolMatchesType(
  const BT::TreeNode & node, const std::string & tool_id,
  const std::string & instrument_type)
{
  return !tool_id.empty() && !instrument_type.empty() &&
         toolTypeId(node, tool_id) == instrument_type;
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
    "surgical_field", "surgeon_hand", "return_zone", "mayo_stand"};
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
  writeBlackboard(node, "selected.policy_transition", std::string{});
  writeBlackboard(node, "selected.policy_basis", std::string{});
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

std::string findActiveInstanceForType(
  const BT::TreeNode & node, const std::string & instrument_type,
  const std::unordered_set<std::string> & allowed_lifecycles = {})
{
  for (const auto & tool_id : allTools(node)) {
    if (!toolMatchesType(node, tool_id, instrument_type) || !toolIsActive(node, tool_id)) {
      continue;
    }
    if (
      allowed_lifecycles.empty() ||
      allowed_lifecycles.count(toolLifecycle(node, tool_id)) > 0)
    {
      return tool_id;
    }
  }
  return {};
}

bool hasBlockingSafetyFlag(const BT::TreeNode & node, bool allow_vlm_unhealthy = false)
{
  std::string safety_flags;
  readBlackboard(node, "safety.flags.csv", safety_flags);
  for (const auto & flag : splitCsv(safety_flags)) {
    if (allow_vlm_unhealthy && flag == "vlm_unhealthy") {
      continue;
    }
    return true;
  }
  return false;
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

constexpr int kMaxSurgeonHeldTools = 2;

bool toolOccupiesSurgeonHand(const BT::TreeNode & node, const std::string & tool_id)
{
  if (!toolIsActive(node, tool_id)) {
    return false;
  }
  std::string status;
  std::string location_type;
  std::string owner;
  const auto lifecycle = toolLifecycle(node, tool_id);
  readBlackboard(node, makeToolKey(tool_id, "status"), status);
  readBlackboard(node, makeToolKey(tool_id, "location_type"), location_type);
  readBlackboard(node, makeToolKey(tool_id, "owner"), owner);
  if (
    location_type == "surgical_field" || location_type == "bed_fixed_tool" ||
    location_type == "return_zone")
  {
    return false;
  }
  if (location_type == "surgeon_hand") {
    return true;
  }
  if (!location_type.empty()) {
    return false;
  }
  return lifecycle == "surgeon_owned" || status == "handed_over" ||
         owner == "surgeon";
}

int surgeonHeldToolCount(const BT::TreeNode & node, const std::string & excluded_tool = {})
{
  int count = 0;
  for (const auto & tool_id : allTools(node)) {
    if (tool_id == excluded_tool) {
      continue;
    }
    if (toolOccupiesSurgeonHand(node, tool_id)) {
      ++count;
    }
  }
  return count;
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
  bool future_use_expected = false;
  readBlackboard(
    node, makeToolKey(tool_id, "future_use_expected"),
    future_use_expected);
  const auto lifecycle = toolLifecycle(node, tool_id);
  const auto next_required_transition = toolNextRequiredTransition(node, tool_id);

  if (contaminated && lifecycle != "mayo_reuse") {
    return false;
  }
  if (!next_required_transition.empty()) {
    return false;
  }
  if (lifecycle == "mayo_reuse") {
    return future_use_expected;
  }
  return lifecycle == "home_rack" || lifecycle == "returned_home";
}

std::string findAnticipatoryInstanceForType(
  const BT::TreeNode & node, const std::string & instrument_type)
{
  for (const auto & tool_id : allTools(node)) {
    if (
      toolMatchesType(node, tool_id, instrument_type) &&
      toolIsAnticipatoryCandidate(node, tool_id))
    {
      return tool_id;
    }
  }
  return {};
}

bool stablePredictionReplacesPreposition(const BT::TreeNode & node)
{
  std::string predicted_tool;
  std::string prepositioned_tool;
  double confidence = 0.0;
  double stability_sec = 0.0;
  readBlackboard(node, "prediction.tool", predicted_tool);
  readBlackboard(node, "robot.prepositioned_tool", prepositioned_tool);
  readBlackboard(node, "prediction.confidence", confidence);
  readBlackboard(node, "prediction.stability_sec", stability_sec);
  const auto replacement_instance = findAnticipatoryInstanceForType(
    node, predicted_tool);
  const bool replacement_available = !replacement_instance.empty();
  return
    !predicted_tool.empty() && !prepositioned_tool.empty() &&
    predicted_tool != prepositioned_tool &&
    replacement_available &&
    confidence >= kPreparationMinConfidence &&
    stability_sec >= kPreparationMinStabilitySec;
}

struct RecoveryPolicyCandidate
{
  std::string tool_id;
  std::string basis;
};

RecoveryPolicyCandidate selectRecoveryPolicyCandidate(const BT::TreeNode & node)
{
  std::string execution_state;
  std::string explicit_request;
  std::string surgeon_request;
  std::string implicit_request;
  std::string predicted_tool;
  std::string prepositioned_tool;
  std::string active_task_id;
  std::string left_hand_tool;
  bool cleaner_busy = false;
  bool phase_uncertain = true;
  readBlackboard(node, "runtime.execution_state", execution_state);
  readBlackboard(node, "request.explicit_tool", explicit_request);
  readBlackboard(node, "request.surgeon_tool", surgeon_request);
  readBlackboard(node, "request.implicit_tool", implicit_request);
  readBlackboard(node, "prediction.tool", predicted_tool);
  readBlackboard(node, "robot.prepositioned_tool", prepositioned_tool);
  readBlackboard(node, "robot.active_task_id", active_task_id);
  readBlackboard(node, "robot.left_hand_tool", left_hand_tool);
  readBlackboard(node, "cleaner.busy", cleaner_busy);
  readBlackboard(node, "phase.uncertain", phase_uncertain);
  if (
    execution_state != "running" && execution_state != "finishing")
  {
    return {};
  }
  if (
    !active_task_id.empty() || !left_hand_tool.empty() || cleaner_busy ||
    hasBlockingSafetyFlag(node))
  {
    return {};
  }
  if (execution_state == "running" && phase_uncertain) {
    return {};
  }

  std::vector<std::string> mayo_tools;
  for (const auto & tool_id : allTools(node)) {
    const auto lifecycle = toolLifecycle(node, tool_id);
    if (
      toolIsActive(node, tool_id) &&
      (lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery"))
    {
      mayo_tools.push_back(tool_id);
    }
  }
  if (mayo_tools.empty()) {
    return {};
  }

  const auto oldest = [&node](
      const std::vector<std::string> & candidates,
      const std::string & basis) -> RecoveryPolicyCandidate
    {
      std::string selected;
      double selected_stamp = std::numeric_limits<double>::max();
      for (const auto & tool_id : candidates) {
        double stamp = 0.0;
        readBlackboard(node, makeToolKey(tool_id, "last_observed_sec"), stamp);
        if (selected.empty() || stamp < selected_stamp) {
          selected = tool_id;
          selected_stamp = stamp;
        }
      }
      return {selected, basis};
    };

  if (execution_state == "finishing") {
    return oldest(mayo_tools, "completion_cleanup");
  }

  const std::unordered_set<std::string> protected_types = {
    explicit_request, surgeon_request, implicit_request, predicted_tool, prepositioned_tool};
  std::vector<std::string> stable_recovery_candidates;
  std::vector<std::string> capacity_candidates;
  for (const auto & tool_id : mayo_tools) {
    const auto tool_type = toolTypeId(node, tool_id);
    if (!tool_type.empty() && protected_types.count(tool_type) > 0) {
      continue;
    }
    bool future_use_expected = true;
    double reuse_confidence = 0.0;
    double reuse_stability_sec = 0.0;
    double recovery_confidence = 0.0;
    double recovery_stability_sec = 0.0;
    std::string placement_evidence;
    readBlackboard(
      node, makeToolKey(tool_id, "future_use_expected"), future_use_expected);
    readBlackboard(
      node, makeToolKey(tool_id, "mayo_reuse_confidence"), reuse_confidence);
    readBlackboard(
      node, makeToolKey(tool_id, "mayo_reuse_stability_sec"), reuse_stability_sec);
    readBlackboard(
      node, makeToolKey(tool_id, "mayo_recovery_confidence"), recovery_confidence);
    readBlackboard(
      node, makeToolKey(tool_id, "mayo_recovery_stability_sec"), recovery_stability_sec);
    readBlackboard(
      node, makeToolKey(tool_id, "mayo_placement_evidence"), placement_evidence);

    const bool stable_reuse =
      reuse_confidence >= 0.5 && reuse_stability_sec >= 5.0;
    const bool stable_recovery =
      recovery_confidence >= 0.5 && recovery_stability_sec >= 5.0;
    if (!future_use_expected && stable_recovery && !stable_reuse) {
      stable_recovery_candidates.push_back(tool_id);
    }
    if (
      mayo_tools.size() > 2 && !future_use_expected && !stable_reuse &&
      !placement_evidence.empty())
    {
      capacity_candidates.push_back(tool_id);
    }
  }
  if (!stable_recovery_candidates.empty()) {
    return oldest(stable_recovery_candidates, "stable_vlm_recovery_evidence");
  }
  if (!capacity_candidates.empty()) {
    return oldest(capacity_candidates, "mayo_capacity_soft_limit");
  }
  return {};
}

bool hasRecoveryContext(const BT::TreeNode & node)
{
  bool required = false;
  bool ready_for_retrieval = false;
  bool cleaner_busy = false;
  std::string surgeon_intent;
  std::string surgeon_request_tool;
  std::string surgeon_request_instance;
  std::string left_hand_tool;
  readBlackboard(node, "recovery.required", required);
  readBlackboard(node, "surgeon.ready_retrieval", ready_for_retrieval);
  readBlackboard(node, "cleaner.busy", cleaner_busy);
  readBlackboard(node, "surgeon.intent", surgeon_intent);
  readBlackboard(node, "request.surgeon_tool", surgeon_request_tool);
  readBlackboard(node, "request.surgeon_instance", surgeon_request_instance);
  readBlackboard(node, "robot.left_hand_tool", left_hand_tool);

  if (required || ready_for_retrieval || cleaner_busy || !left_hand_tool.empty()) {
    return true;
  }
  if (stablePredictionReplacesPreposition(node)) {
    return true;
  }

  std::string pending_csv;
  readBlackboard(node, "pending_transition_tools.csv", pending_csv);
  if (!pending_csv.empty()) {
    for (const auto & pending_tool : splitCsv(pending_csv)) {
      for (const auto & tool_id : allTools(node)) {
        if (
          tool_id != pending_tool &&
          !toolMatchesType(node, tool_id, pending_tool))
        {
          continue;
        }
        const auto next_required_transition =
          toolNextRequiredTransition(node, tool_id);
        if (
          next_required_transition == "recover_left" ||
          next_required_transition == "clean_left" ||
          next_required_transition == "return_home" ||
          next_required_transition == "return_unused_preposition")
        {
          return true;
        }
      }
    }
  }

  if (surgeon_request_instance.empty() && !surgeon_request_tool.empty()) {
    surgeon_request_instance =
      findActiveInstanceForType(node, surgeon_request_tool);
  }
  const bool explicit_recovery =
    !surgeon_request_tool.empty() &&
    (surgeon_intent == "return_tool" || surgeon_intent == "extend_hand_for_retrieval" ||
    surgeon_intent == "awaiting_retrieval") &&
    toolIsRecoverableFromSurgeon(node, surgeon_request_instance);
  return explicit_recovery || !selectRecoveryPolicyCandidate(node).tool_id.empty();
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
    writeBlackboard(
      *this, "request.surgeon_instance", msg.surgeon_request_instance_id);
    writeBlackboard(
      *this, "request.generation", static_cast<int64_t>(msg.surgeon_request_generation));
    writeBlackboard(
      *this, "request.additional_instance_assumed",
      static_cast<bool>(msg.surgeon_request_additional_instance_assumed));
    writeBlackboard(
      *this, "request.voice_backed", static_cast<bool>(msg.explicit_request_voice_backed));
    writeBlackboard(
      *this, "request.implicit_visible",
      static_cast<bool>(msg.implicit_request_visible));
    writeBlackboard(*this, "request.implicit_tool", msg.implicit_request_tool);
    writeBlackboard(
      *this, "request.implicit_hand_pose", msg.implicit_request_hand_pose);
    writeBlackboard(
      *this, "request.implicit_confidence",
      static_cast<double>(msg.implicit_request_confidence));
    writeBlackboard(
      *this, "request.implicit_stability_sec",
      static_cast<double>(msg.implicit_request_stability_sec));
    writeBlackboard(
      *this, "request.implicit_generation",
      static_cast<int64_t>(msg.implicit_request_generation));
    writeBlackboard(*this, "surgeon.intent", msg.surgeon_intent);
    writeBlackboard(*this, "surgeon.ready_handover", static_cast<bool>(msg.surgeon_ready_for_handover));
    writeBlackboard(*this, "surgeon.ready_retrieval", static_cast<bool>(msg.surgeon_ready_for_retrieval));
    writeBlackboard(*this, "robot.state", msg.robot_state);
    writeBlackboard(*this, "robot.right_hand_tool", msg.right_hand_tool);
    writeBlackboard(
      *this, "robot.right_hand_instance", msg.right_hand_tool_instance_id);
    writeBlackboard(*this, "robot.left_hand_tool", msg.left_hand_tool);
    writeBlackboard(
      *this, "robot.left_hand_instance", msg.left_hand_tool_instance_id);
    writeBlackboard(*this, "robot.prepositioned_tool", msg.prepositioned_tool);
    writeBlackboard(
      *this, "robot.prepositioned_instance",
      msg.prepositioned_tool_instance_id);
    writeBlackboard(*this, "prediction.tool", msg.predicted_tool);
    writeBlackboard(*this, "prediction.confidence", static_cast<double>(msg.predicted_tool_confidence));
    writeBlackboard(*this, "prediction.stability_sec", static_cast<double>(msg.predicted_tool_stability_sec));
    writeBlackboard(*this, "robot.active_task_id", msg.active_robot_task_id);
    writeBlackboard(*this, "robot.active_task_type", msg.active_robot_task_type);
    writeBlackboard(*this, "robot.active_task_tool_id", msg.active_robot_task_tool_id);
    writeBlackboard(
      *this, "robot.active_task_tool_instance_id",
      msg.active_robot_task_tool_instance_id);
    writeBlackboard(*this, "robot.active_task_arm", msg.active_robot_task_arm);
    writeBlackboard(*this, "cleaner.busy", static_cast<bool>(msg.cleaner_busy));
    writeBlackboard(
      *this, "state.handover_hint", static_cast<bool>(msg.handover_allowed));
    writeBlackboard(*this, "recovery.required", static_cast<bool>(msg.recovery_required));
    writeBlackboard(*this, "safety.flags.csv", joinCsv(msg.safety_flags));
    writeBlackboard(*this, "pending_transition_tools.csv", joinCsv(msg.pending_transition_tools));
    writeBlackboard(*this, "active_recovery_tools.csv", joinCsv(msg.active_recovery_tools));
    writeBlackboard(
      *this, "active_recovery_instances.csv",
      joinCsv(msg.active_recovery_tool_instances));
    writeBlackboard(*this, "expected_tools.csv", joinCsv(msg.expected_instruments));
    writeBlackboard(*this, "available_tools.csv", joinCsv(msg.available_instruments));
    std::vector<std::string> all_tools;
    all_tools.reserve(msg.instrument_states.size());
    std::unordered_set<std::string> active_tools;

    for (const auto & instrument : msg.instrument_states) {
      const auto instance_id =
        instrument.instance_id.empty() ? instrument.instrument_id : instrument.instance_id;
      if (active_tools.insert(instance_id).second) {
        all_tools.push_back(instance_id);
      }
      writeBlackboard(*this, makeToolKey(instance_id, "active"), true);
      writeBlackboard(
        *this, makeToolKey(instance_id, "type_id"), instrument.instrument_id);
      writeBlackboard(*this, makeToolKey(instance_id, "home_location"), instrument.home_location_id);
      writeBlackboard(*this, makeToolKey(instance_id, "home_type"), instrument.home_location_type);
      writeBlackboard(*this, makeToolKey(instance_id, "status"), instrument.status);
      writeBlackboard(*this, makeToolKey(instance_id, "location"), instrument.location_id);
      writeBlackboard(*this, makeToolKey(instance_id, "location_type"), instrument.location_type);
      writeBlackboard(*this, makeToolKey(instance_id, "owner"), instrument.owner);
      writeBlackboard(*this, makeToolKey(instance_id, "available"), isAvailableStatus(instrument.status));
      writeBlackboard(*this, makeToolKey(instance_id, "contaminated"), static_cast<bool>(instrument.contaminated));
      writeBlackboard(*this, makeToolKey(instance_id, "cleanliness"), instrument.cleanliness_state);
      writeBlackboard(*this, makeToolKey(instance_id, "lifecycle"), instrument.lifecycle_stage);
      writeBlackboard(
        *this, makeToolKey(instance_id, "next_required_transition"),
        instrument.next_required_transition);
      writeBlackboard(
        *this, makeToolKey(instance_id, "future_use_expected"),
        static_cast<bool>(instrument.procedure_future_use_expected));
      writeBlackboard(
        *this, makeToolKey(instance_id, "mayo_placement_evidence"),
        instrument.mayo_placement_evidence);
      writeBlackboard(
        *this, makeToolKey(instance_id, "last_observed_sec"),
        static_cast<double>(instrument.last_observed_sec));
      writeBlackboard(
        *this, makeToolKey(instance_id, "mayo_reuse_confidence"),
        static_cast<double>(instrument.mayo_reuse_confidence));
      writeBlackboard(
        *this, makeToolKey(instance_id, "mayo_reuse_stability_sec"),
        static_cast<double>(instrument.mayo_reuse_stability_sec));
      writeBlackboard(
        *this, makeToolKey(instance_id, "mayo_recovery_confidence"),
        static_cast<double>(instrument.mayo_recovery_confidence));
      writeBlackboard(
        *this, makeToolKey(instance_id, "mayo_recovery_stability_sec"),
        static_cast<double>(instrument.mayo_recovery_stability_sec));
      writeBlackboard(
        *this, makeToolKey(instance_id, "mayo_evidence_source"),
        instrument.mayo_evidence_source);
      writeBlackboard(
        *this, makeToolKey(instance_id, "preposition_origin_location"),
        instrument.preposition_origin_location_id);
      writeBlackboard(
        *this, makeToolKey(instance_id, "preposition_origin_type"),
        instrument.preposition_origin_location_type);
      writeBlackboard(
        *this, makeToolKey(instance_id, "preposition_origin_lifecycle"),
        instrument.preposition_origin_lifecycle_stage);
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
    return (
      execution_state == "running" || execution_state == "finishing") ?
      BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
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

class HasImplicitRequest : public BT::ConditionNode
{
public:
  explicit HasImplicitRequest(const std::string & name, const BT::NodeConfig & config)
  : BT::ConditionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    bool visible = false;
    double confidence = 0.0;
    double stability_sec = 0.0;
    std::string hand_pose;
    std::string implicit_tool;
    std::string predicted_tool;
    std::string prepositioned_tool;
    std::string prepositioned_instance;
    readBlackboard(*this, "request.implicit_visible", visible);
    readBlackboard(*this, "request.implicit_confidence", confidence);
    readBlackboard(*this, "request.implicit_stability_sec", stability_sec);
    readBlackboard(*this, "request.implicit_hand_pose", hand_pose);
    readBlackboard(*this, "request.implicit_tool", implicit_tool);
    readBlackboard(*this, "prediction.tool", predicted_tool);
    readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
    readBlackboard(*this, "robot.prepositioned_instance", prepositioned_instance);
    if (
      !visible || hand_pose != "open_receive" ||
      confidence < kImplicitGestureMinConfidence ||
      stability_sec < kImplicitGestureMinStabilitySec)
    {
      return BT::NodeStatus::FAILURE;
    }
    if (!implicit_tool.empty() && !predicted_tool.empty() && implicit_tool != predicted_tool) {
      return BT::NodeStatus::FAILURE;
    }
    if (implicit_tool.empty()) {
      const auto prepared_instance =
        !prepositioned_instance.empty() ? prepositioned_instance :
        findActiveInstanceForType(
        *this, prepositioned_tool, {"prepositioned_right"});
      if (
        !prepositioned_tool.empty() && !prepared_instance.empty() &&
        toolIsActive(*this, prepared_instance) &&
        toolLifecycle(*this, prepared_instance) == "prepositioned_right")
      {
        return BT::NodeStatus::SUCCESS;
      }
      double prediction_confidence = 0.0;
      double prediction_stability_sec = 0.0;
      readBlackboard(*this, "prediction.confidence", prediction_confidence);
      readBlackboard(*this, "prediction.stability_sec", prediction_stability_sec);
      if (
        predicted_tool.empty() ||
        prediction_confidence < kPreparationMinConfidence ||
        prediction_stability_sec < kPreparationMinStabilitySec)
      {
        return BT::NodeStatus::FAILURE;
      }
    }
    return BT::NodeStatus::SUCCESS;
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
    const bool unsupported_preposition = prepositionEvidenceExpired();
    return (unsupported_preposition || hasRecoveryContext(*this)) ?
      BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
  }

private:
  bool prepositionEvidenceExpired()
  {
    std::string prepositioned_tool;
    std::string prepositioned_instance;
    std::string predicted_tool;
    double prediction_confidence = 0.0;
    readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
    readBlackboard(*this, "robot.prepositioned_instance", prepositioned_instance);
    readBlackboard(*this, "prediction.tool", predicted_tool);
    readBlackboard(*this, "prediction.confidence", prediction_confidence);

    if (prepositioned_tool.empty() || prepositioned_instance.empty()) {
      tracked_prepositioned_instance_.clear();
      prepositioned_since_.reset();
      unsupported_since_.reset();
      writeBlackboard(
        *this, "policy.expired_preposition_instance", std::string{});
      writeBlackboard(
        *this, "policy.expired_preposition_reason", std::string{});
      return false;
    }

    if (tracked_prepositioned_instance_ != prepositioned_instance) {
      tracked_prepositioned_instance_ = prepositioned_instance;
      prepositioned_since_ = std::chrono::steady_clock::now();
      unsupported_since_.reset();
    }

    const auto now = std::chrono::steady_clock::now();
    const auto max_dwell_sec =
      predicted_tool == prepositioned_tool &&
      prediction_confidence >= kPreparationStrongConfidence ?
      kPreparationStrongMaxDwellSec : kPreparationMaxDwellSec;
    if (
      prepositioned_since_.has_value() &&
      std::chrono::duration<double>(
        now - prepositioned_since_.value()).count() >=
      max_dwell_sec)
    {
      writeBlackboard(
        *this, "policy.expired_preposition_instance",
        prepositioned_instance);
      writeBlackboard(
        *this, "policy.expired_preposition_reason",
        std::string("preposition_dwell_expired"));
      return true;
    }

    if (predicted_tool == prepositioned_tool) {
      unsupported_since_.reset();
      writeBlackboard(
        *this, "policy.expired_preposition_instance", std::string{});
      writeBlackboard(
        *this, "policy.expired_preposition_reason", std::string{});
      return false;
    }

    // A stable different prediction is handled by the replacement policy.
    if (stablePredictionReplacesPreposition(*this)) {
      unsupported_since_.reset();
      writeBlackboard(
        *this, "policy.expired_preposition_instance", std::string{});
      writeBlackboard(
        *this, "policy.expired_preposition_reason", std::string{});
      return false;
    }

    if (!unsupported_since_.has_value()) {
      unsupported_since_ = now;
      return false;
    }
    const auto unsupported_sec =
      std::chrono::duration<double>(now - unsupported_since_.value()).count();
    if (unsupported_sec < kPreparationUnsupportedGraceSec) {
      return false;
    }

    writeBlackboard(
      *this, "policy.expired_preposition_instance",
      prepositioned_instance);
    writeBlackboard(
      *this, "policy.expired_preposition_reason",
      std::string("prediction_evidence_expired"));
    return true;
  }

  std::string tracked_prepositioned_instance_;
  std::optional<std::chrono::steady_clock::time_point> prepositioned_since_;
  std::optional<std::chrono::steady_clock::time_point> unsupported_since_;
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
    const bool on_mayo = lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery";
    if (lifecycle == "surgeon_owned" || on_mayo) {
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
    std::string selected_tool;
    std::string explicit_request;
    std::string surgeon_request;
    bool voice_backed = false;
    readBlackboard(*this, "selected.tool", selected_tool);
    readBlackboard(*this, "request.explicit_tool", explicit_request);
    readBlackboard(*this, "request.surgeon_tool", surgeon_request);
    readBlackboard(*this, "request.voice_backed", voice_backed);
    const auto selected_tool_type = toolTypeId(*this, selected_tool);
    const bool voice_backed_selected =
      voice_backed && !selected_tool.empty() &&
      (selected_tool_type == explicit_request ||
      selected_tool_type == surgeon_request);
    if (hasBlockingSafetyFlag(*this, voice_backed_selected)) {
      return BT::NodeStatus::FAILURE;
    }
    if (hasActiveRobotTask(*this)) {
      return BT::NodeStatus::FAILURE;
    }
    if (!toolIsActive(*this, selected_tool)) {
      return BT::NodeStatus::FAILURE;
    }
    const auto lifecycle = toolLifecycle(*this, selected_tool);
    if (lifecycle == "surgeon_owned") {
      return BT::NodeStatus::SUCCESS;
    }
    if (surgeonHeldToolCount(*this, selected_tool) >= kMaxSurgeonHeldTools) {
      return BT::NodeStatus::FAILURE;
    }
    bool allowed = false;
    readBlackboard(*this, "action.guard.handover_allowed", allowed);
    return allowed ? BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
  }
};

class CanPreposition : public BT::ConditionNode
{
public:
  explicit CanPreposition(const std::string & name, const BT::NodeConfig & config)
  : BT::ConditionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    std::string selected_tool;
    std::string execution_state;
    std::string robot_state;
    std::string right_hand_tool;
    bool cleaner_busy = false;
    readBlackboard(*this, "selected.tool", selected_tool);
    readBlackboard(*this, "runtime.execution_state", execution_state);
    readBlackboard(*this, "robot.state", robot_state);
    readBlackboard(*this, "robot.right_hand_tool", right_hand_tool);
    readBlackboard(*this, "cleaner.busy", cleaner_busy);

    if (
      selected_tool.empty() || execution_state != "running" ||
      robot_state == "fault" || hasActiveRobotTask(*this) ||
      cleaner_busy || !right_hand_tool.empty() ||
      hasBlockingSafetyFlag(*this))
    {
      return BT::NodeStatus::FAILURE;
    }
    return toolIsAnticipatoryCandidate(*this, selected_tool) ?
      BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
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
    std::string surgeon_instance;
    std::string right_hand_tool;
    std::string right_hand_instance;
    bool ready_for_retrieval = false;
    readBlackboard(*this, "request.explicit_tool", tool_id);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    readBlackboard(*this, "request.surgeon_tool", surgeon_tool);
    readBlackboard(*this, "request.surgeon_instance", surgeon_instance);
    readBlackboard(*this, "robot.right_hand_tool", right_hand_tool);
    readBlackboard(*this, "robot.right_hand_instance", right_hand_instance);
    readBlackboard(*this, "surgeon.ready_retrieval", ready_for_retrieval);
    if (tool_id.empty() && isExplicitSurgeonIntent(surgeon_intent)) {
      tool_id = surgeon_tool;
    }
    if (tool_id.empty() && !surgeon_tool.empty() && !ready_for_retrieval) {
      tool_id = surgeon_tool;
    }
    const auto requested_tool_type = toolTypeId(*this, tool_id);
    if (
      !right_hand_instance.empty() &&
      toolIsActive(*this, right_hand_instance) &&
      (
        right_hand_tool == requested_tool_type ||
        toolMatchesType(*this, right_hand_instance, requested_tool_type)
      ))
    {
      writeBlackboard(*this, "selected.tool", right_hand_instance);
      writeBlackboard(*this, "selected.policy_transition", std::string{});
      writeBlackboard(
        *this, "selected.policy_basis",
        std::string("explicit_request_preposition_match"));
      return BT::NodeStatus::SUCCESS;
    }
    if (!surgeon_instance.empty() && toolIsActive(*this, surgeon_instance)) {
      writeBlackboard(*this, "selected.tool", surgeon_instance);
      writeBlackboard(*this, "selected.policy_transition", std::string{});
      writeBlackboard(*this, "selected.policy_basis", std::string("explicit_request"));
      return BT::NodeStatus::SUCCESS;
    }
    if (tool_id.empty()) {
      return BT::NodeStatus::FAILURE;
    }
    const auto selected_instance =
      toolIsActive(*this, tool_id) ?
      tool_id : findActiveInstanceForType(*this, tool_id);
    if (selected_instance.empty()) {
      return BT::NodeStatus::FAILURE;
    }
    writeBlackboard(*this, "selected.tool", selected_instance);
    writeBlackboard(*this, "selected.policy_transition", std::string{});
    writeBlackboard(*this, "selected.policy_basis", std::string("explicit_request"));
    return BT::NodeStatus::SUCCESS;
  }
};

class SelectImplicitTool : public BT::SyncActionNode
{
public:
  explicit SelectImplicitTool(
    const std::string & name, const BT::NodeConfig & config, [[maybe_unused]] RosContext context)
  : BT::SyncActionNode(name, config)
  {
  }

  static BT::PortsList providedPorts() { return {}; }

  BT::NodeStatus tick() override
  {
    std::string tool_type;
    std::string selected_instance;
    std::string policy_basis = "implicit_visual_request";
    readBlackboard(*this, "request.implicit_tool", tool_type);
    if (tool_type.empty()) {
      std::string prepositioned_tool;
      std::string prepositioned_instance;
      readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
      readBlackboard(*this, "robot.prepositioned_instance", prepositioned_instance);
      if (
        !prepositioned_tool.empty() && !prepositioned_instance.empty() &&
        toolIsActive(*this, prepositioned_instance) &&
        toolLifecycle(*this, prepositioned_instance) == "prepositioned_right")
      {
        tool_type = prepositioned_tool;
        selected_instance = prepositioned_instance;
        policy_basis = "implicit_visual_preposition_match";
      } else {
        readBlackboard(*this, "prediction.tool", tool_type);
        policy_basis = "implicit_visual_prediction_fallback";
      }
    }
    if (tool_type.empty()) {
      return BT::NodeStatus::FAILURE;
    }
    if (selected_instance.empty()) {
      selected_instance = findActiveInstanceForType(
        *this, tool_type,
        {"prepositioned_right", "home_rack", "returned_home", "mayo_reuse"});
    }
    if (selected_instance.empty()) {
      return BT::NodeStatus::FAILURE;
    }
    writeBlackboard(*this, "selected.tool", selected_instance);
    writeBlackboard(*this, "selected.policy_transition", std::string{});
    writeBlackboard(*this, "selected.policy_basis", policy_basis);
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
    std::string predicted_tool;
    readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
    readBlackboard(*this, "prediction.tool", predicted_tool);
    const bool active_return_intent =
      !surgeon_request.empty() &&
      (surgeon_intent == "return_tool" || surgeon_intent == "extend_hand_for_retrieval");
    const bool blocked_by_existing_preposition =
      !prepositioned_tool.empty() && (predicted_tool.empty() || prepositioned_tool == predicted_tool);
    if (
      hasRecoveryContext(*this) || !explicit_request.empty() || !surgeon_request.empty() ||
      active_return_intent || hasActiveRobotTask(*this) || blocked_by_existing_preposition)
    {
      return BT::NodeStatus::FAILURE;
    }

    double prediction_confidence = 0.0;
    double prediction_stability_sec = 0.0;
    readBlackboard(*this, "prediction.confidence", prediction_confidence);
    readBlackboard(*this, "prediction.stability_sec", prediction_stability_sec);
    std::string cooldown_tool;
    double cooldown_clear_since_sec = 0.0;
    readBlackboard(*this, "policy.preposition_cooldown_tool", cooldown_tool);
    readBlackboard(
      *this, "policy.preposition_cooldown_clear_since_sec",
      cooldown_clear_since_sec);
    const auto now_sec = steadyNowSec();
    if (!cooldown_tool.empty()) {
      if (cooldown_tool == predicted_tool) {
        writeBlackboard(
          *this, "policy.preposition_cooldown_clear_since_sec", 0.0);
        return BT::NodeStatus::FAILURE;
      }
      if (cooldown_clear_since_sec <= 0.0) {
        writeBlackboard(
          *this, "policy.preposition_cooldown_clear_since_sec", now_sec);
      } else if (
        now_sec - cooldown_clear_since_sec >=
        kPreparationRetryCooldownSec)
      {
        writeBlackboard(
          *this, "policy.preposition_cooldown_tool", std::string{});
        writeBlackboard(
          *this, "policy.preposition_cooldown_clear_since_sec", 0.0);
      }
    }
    if (
      !predicted_tool.empty() &&
      prediction_confidence >= kPreparationMinConfidence &&
      prediction_stability_sec >= kPreparationMinStabilitySec)
    {
      const auto predicted_instance = findAnticipatoryInstanceForType(
        *this, predicted_tool);
      if (!predicted_instance.empty())
      {
        writeBlackboard(*this, "selected.tool", predicted_instance);
        writeBlackboard(*this, "selected.policy_transition", std::string{});
        writeBlackboard(*this, "selected.policy_basis", std::string("stable_tool_prediction"));
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
    std::string execution_state;
    std::string left_hand_tool;
    bool cleaner_busy = false;
    readBlackboard(*this, "runtime.execution_state", execution_state);
    readBlackboard(*this, "robot.left_hand_tool", left_hand_tool);
    readBlackboard(*this, "cleaner.busy", cleaner_busy);
    if (
      (execution_state != "running" && execution_state != "finishing") ||
      hasActiveRobotTask(*this) || !left_hand_tool.empty() || cleaner_busy ||
      hasBlockingSafetyFlag(*this))
    {
      return BT::NodeStatus::FAILURE;
    }

    // The instance queue is append ordered. Select only physically present
    // Mayo recovery items so a surgeon-held return request cannot be executed
    // before the tool actually reaches the stand.
    std::string active_recovery_instances_csv;
    readBlackboard(
      *this, "active_recovery_instances.csv",
      active_recovery_instances_csv);
    for (const auto & instance_id : splitCsv(active_recovery_instances_csv)) {
      if (
        toolIsActive(*this, instance_id) &&
        toolLifecycle(*this, instance_id) == "mayo_recovery" &&
        toolNextRequiredTransition(*this, instance_id) == "recover_left")
      {
        return selectTool(
          instance_id, "recover_left", "authoritative_recovery_transaction");
      }
    }

    std::string surgeon_request_tool;
    std::string surgeon_request_instance;
    std::string surgeon_intent;
    readBlackboard(*this, "request.surgeon_tool", surgeon_request_tool);
    readBlackboard(
      *this, "request.surgeon_instance", surgeon_request_instance);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    if (!surgeon_request_instance.empty()) {
      const auto lifecycle = toolLifecycle(*this, surgeon_request_instance);
      if (
        toolIsActive(*this, surgeon_request_instance) &&
        (lifecycle == "mayo_recovery" || lifecycle == "mayo_reuse"))
      {
        return selectTool(
          surgeon_request_instance, "recover_left", "explicit_retrieval_request");
      }
    }

    std::string active_recovery_csv;
    readBlackboard(*this, "active_recovery_tools.csv", active_recovery_csv);
    for (const auto & instrument_type : splitCsv(active_recovery_csv)) {
      const auto instance_id = findActiveInstanceForType(
        *this, instrument_type, {"mayo_recovery"});
      if (
        !instance_id.empty() &&
        toolNextRequiredTransition(*this, instance_id) == "recover_left")
      {
        return selectTool(
          instance_id, "recover_left", "authoritative_recovery_transaction");
      }
    }

    for (const auto & tool_id : allTools(*this)) {
      const auto lifecycle = toolLifecycle(*this, tool_id);
      if (lifecycle == "mayo_recovery") {
        return selectTool(
          tool_id, "recover_left", "observed_mayo_recovery_state");
      }
    }

    for (const auto & tool_id : allTools(*this)) {
      const auto lifecycle = toolLifecycle(*this, tool_id);
      if (
        toolNextRequiredTransition(*this, tool_id) == "return_unused_preposition" &&
        lifecycle == "prepositioned_right")
      {
        return selectTool(
          tool_id, "return_unused_preposition", "unused_preposition");
      }
    }

    std::string expired_preposition_instance;
    readBlackboard(
      *this, "policy.expired_preposition_instance",
      expired_preposition_instance);
    if (
      !expired_preposition_instance.empty() &&
      toolLifecycle(*this, expired_preposition_instance) == "prepositioned_right")
    {
      std::string expiration_reason;
      readBlackboard(
        *this, "policy.expired_preposition_reason",
        expiration_reason);
      return selectTool(
        expired_preposition_instance, "return_unused_preposition",
        expiration_reason.empty() ?
        std::string("prediction_evidence_expired") :
        expiration_reason);
    }

    if (stablePredictionReplacesPreposition(*this)) {
      std::string prepositioned_tool;
      readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
      for (const auto & tool_id : allTools(*this)) {
        if (
          toolLifecycle(*this, tool_id) == "prepositioned_right" &&
          toolMatchesType(*this, tool_id, prepositioned_tool))
        {
          return selectTool(
            tool_id, "return_unused_preposition", "stable_prediction_replacement");
        }
      }
    }

    const auto policy_candidate = selectRecoveryPolicyCandidate(*this);
    if (!policy_candidate.tool_id.empty()) {
      return selectTool(
        policy_candidate.tool_id, "recover_left", policy_candidate.basis);
    }

    return BT::NodeStatus::FAILURE;
  }

private:
  BT::NodeStatus selectTool(
    const std::string & tool_id, const std::string & policy_transition,
    const std::string & policy_basis)
  {
    std::string home_location_id;
    std::string home_location_type;
    readBlackboard(*this, makeToolKey(tool_id, "home_location"), home_location_id);
    readBlackboard(*this, makeToolKey(tool_id, "home_type"), home_location_type);
    if (policy_transition == "return_unused_preposition") {
      std::string origin_location_id;
      std::string origin_location_type;
      readBlackboard(
        *this, makeToolKey(tool_id, "preposition_origin_location"),
        origin_location_id);
      readBlackboard(
        *this, makeToolKey(tool_id, "preposition_origin_type"),
        origin_location_type);
      if (!origin_location_id.empty()) {
        home_location_id = origin_location_id;
      }
      if (!origin_location_type.empty()) {
        home_location_type = origin_location_type;
      }
    }
    writeBlackboard(*this, "selected.tool", tool_id);
    writeBlackboard(*this, "selected.policy_transition", policy_transition);
    writeBlackboard(*this, "selected.policy_basis", policy_basis);
    writeBlackboard(*this, "bt.target_location_id", home_location_id);
    writeBlackboard(*this, "bt.target_location_type", home_location_type);
    if (policy_transition == "return_unused_preposition") {
      auto cooldown_tool = toolTypeId(*this, tool_id);
      if (cooldown_tool.empty()) {
        cooldown_tool = tool_id;
      }
      writeBlackboard(
        *this, "policy.preposition_cooldown_tool", cooldown_tool);
      writeBlackboard(
        *this, "policy.preposition_cooldown_clear_since_sec", 0.0);
    }
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
    bool uncertain = true;
    bool implicit_visible = false;
    std::string robot_state;
    std::string active_task_id;
    std::string explicit_request;
    std::string surgeon_request;
    std::string surgeon_intent;
    std::string implicit_tool;
    std::string implicit_hand_pose;
    std::string predicted_tool;
    std::string prepositioned_tool;
    std::string owner;
    const auto lifecycle = toolLifecycle(*this, selected_tool);
    bool contaminated = false;
    bool cleaner_busy = false;
    bool ready_for_handover = false;
    bool voice_backed = false;
    double implicit_confidence = 0.0;
    double implicit_stability_sec = 0.0;
    double prediction_confidence = 0.0;
    double prediction_stability_sec = 0.0;
    readBlackboard(*this, "phase.uncertain", uncertain);
    readBlackboard(*this, "robot.state", robot_state);
    readBlackboard(*this, "robot.active_task_id", active_task_id);
    readBlackboard(*this, "request.explicit_tool", explicit_request);
    readBlackboard(*this, "request.surgeon_tool", surgeon_request);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    readBlackboard(*this, "surgeon.ready_handover", ready_for_handover);
    readBlackboard(*this, "request.voice_backed", voice_backed);
    readBlackboard(*this, "request.implicit_visible", implicit_visible);
    readBlackboard(*this, "request.implicit_tool", implicit_tool);
    readBlackboard(*this, "request.implicit_hand_pose", implicit_hand_pose);
    readBlackboard(*this, "request.implicit_confidence", implicit_confidence);
    readBlackboard(*this, "request.implicit_stability_sec", implicit_stability_sec);
    readBlackboard(*this, "prediction.tool", predicted_tool);
    readBlackboard(*this, "prediction.confidence", prediction_confidence);
    readBlackboard(*this, "prediction.stability_sec", prediction_stability_sec);
    readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
    readBlackboard(*this, "cleaner.busy", cleaner_busy);
    readBlackboard(*this, makeToolKey(selected_tool, "contaminated"), contaminated);
    readBlackboard(*this, makeToolKey(selected_tool, "owner"), owner);
    const auto selected_tool_type = toolTypeId(*this, selected_tool);
    const bool explicit_request_selected =
      (!selected_tool.empty()) &&
      (
        (!explicit_request.empty() && selected_tool_type == explicit_request) ||
        (isExplicitSurgeonIntent(surgeon_intent) &&
        selected_tool_type == surgeon_request)
      );
    const auto implicit_target =
      !implicit_tool.empty() ? implicit_tool :
      (!prepositioned_tool.empty() ? prepositioned_tool : predicted_tool);
    const bool implicit_candidate_supported =
      !implicit_tool.empty() ||
      (!prepositioned_tool.empty() && implicit_target == prepositioned_tool) ||
      (
        prediction_confidence >= kPreparationMinConfidence &&
        prediction_stability_sec >= kPreparationMinStabilitySec
      );
    const bool implicit_request_selected =
      implicit_visible && implicit_hand_pose == "open_receive" &&
      implicit_confidence >= kImplicitGestureMinConfidence &&
      implicit_stability_sec >= kImplicitGestureMinStabilitySec &&
      implicit_candidate_supported &&
      !implicit_target.empty() && selected_tool_type == implicit_target &&
      (implicit_tool.empty() || predicted_tool.empty() || implicit_tool == predicted_tool);
    const bool voice_backed_explicit_request =
      explicit_request_selected && voice_backed;
    const bool active_tool = toolIsActive(*this, selected_tool);
    const bool prepositioned_right = lifecycle == "prepositioned_right";
    const bool holder_available = owner.empty() || owner == "none" || (prepositioned_right && owner == "robot_right_hand");
    const bool usable_lifecycle = lifecycle == "home_rack" || lifecycle == "returned_home" || prepositioned_right;
    const bool surgeon_owned = lifecycle == "surgeon_owned";
    const bool on_mayo = lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery";
    const bool blocked_by_safety =
      hasBlockingSafetyFlag(*this, voice_backed_explicit_request);
    const bool surgeon_hand_has_capacity =
      surgeonHeldToolCount(*this, selected_tool) < kMaxSurgeonHeldTools;
    const bool request_ready =
      (explicit_request_selected && ready_for_handover) ||
      implicit_request_selected ||
      (!explicit_request_selected && !implicit_request_selected);
    const bool mayo_handover_allowed =
      on_mayo && holder_available && active_task_id.empty() && !cleaner_busy &&
      (!uncertain || voice_backed_explicit_request) && robot_state != "fault" &&
      surgeon_hand_has_capacity && request_ready;

    const bool allowed =
      active_tool &&
      !blocked_by_safety &&
      (
        surgeon_owned ||
        mayo_handover_allowed ||
        (
          usable_lifecycle && holder_available && !contaminated && active_task_id.empty() &&
          !cleaner_busy && (!uncertain || voice_backed_explicit_request) &&
          robot_state != "fault" && surgeon_hand_has_capacity &&
          request_ready
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
    auto next_required_transition = toolNextRequiredTransition(*this, selected_tool);
    std::string policy_basis;
    readBlackboard(*this, "selected.policy_basis", policy_basis);
    if (next_required_transition.empty()) {
      readBlackboard(
        *this, "selected.policy_transition", next_required_transition);
    }

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
    std::string right_hand_instance;
    readBlackboard(
      *this, "robot.right_hand_instance", right_hand_instance);

    if (mode == "explicit_request" || mode == "implicit_request") {
      if (lifecycle == "surgeon_owned") {
        writeBlackboard(*this, "bt.action", std::string{});
        writeBlackboard(*this, "bt.decision_reason", std::string("requested tool already surgeon-side"));
        writeBlackboard(*this, "bt.rationale", std::string("requested tool already surgeon-side"));
        clearCommandFields(*this, false);
        writeBlackboard(
          *this, "bt.mode",
          mode == "implicit_request" ?
          std::string("implicit_fulfilled") : std::string("explicit_fulfilled"));
        return BT::NodeStatus::SUCCESS;
      }
      const bool on_mayo =
        lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery";
      if (contaminated && !on_mayo) {
        return BT::NodeStatus::FAILURE;
      }
      if (
        right_hand_instance == selected_tool ||
        lifecycle == "prepositioned_right")
      {
        writeBlackboard(*this, "bt.action", std::string("direct_handover"));
        writeBlackboard(*this, "bt.source_location_id", std::string("robot_right_hand"));
        writeBlackboard(*this, "bt.source_location_type", std::string("robot_right_hand"));
      } else if (!right_hand_instance.empty()) {
        writeBlackboard(*this, "bt.action", std::string("put_down_and_handover"));
        writeBlackboard(
          *this, "bt.source_location_id",
          tool_location.empty() ? home_location_id : tool_location);
        writeBlackboard(
          *this, "bt.source_location_type",
          tool_location_type.empty() ? home_location_type : tool_location_type);
        writeBlackboard(
          *this, "bt.decision_reason",
          std::string("right hand occupied; return held tool before requested handover"));
        writeBlackboard(
          *this, "bt.rationale",
          std::string("right hand occupied; return held tool before requested handover"));
      } else if (on_mayo) {
        writeBlackboard(
          *this, "bt.action", std::string("pick_up_from_mayo_and_handover"));
        writeBlackboard(
          *this, "bt.source_location_id",
          tool_location.empty() ? std::string("mayo_stand") : tool_location);
        writeBlackboard(
          *this, "bt.source_location_type",
          tool_location_type.empty() ? std::string("mayo_stand") : tool_location_type);
        writeBlackboard(
          *this, "bt.decision_reason",
          std::string("requested tool is on Mayo; pick up and hand over"));
        writeBlackboard(
          *this, "bt.rationale",
          std::string("requested tool is on Mayo; pick up and hand over"));
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
      if (!toolIsAnticipatoryCandidate(*this, selected_tool)) {
        return BT::NodeStatus::FAILURE;
      }
      const bool from_mayo_reuse = lifecycle == "mayo_reuse";
      const auto prepare_source_location =
        !tool_location.empty() ? tool_location :
        (from_mayo_reuse ? std::string("mayo_stand") : home_location_id);
      const auto prepare_source_type =
        !tool_location_type.empty() ? tool_location_type :
        (from_mayo_reuse ? std::string("mayo_stand") : home_location_type);
      writeBlackboard(*this, "bt.action", std::string("predict_tool"));
      writeBlackboard(*this, "bt.arm", std::string("right"));
      writeBlackboard(*this, "bt.source_location_id", prepare_source_location);
      writeBlackboard(*this, "bt.source_location_type", prepare_source_type);
      writeBlackboard(*this, "bt.target_location_id", std::string("robot_right_hand"));
      writeBlackboard(*this, "bt.target_location_type", std::string("robot_right_hand"));
      writeBlackboard(*this, "bt.target_owner", std::string("robot_right_hand"));
      writeBlackboard(*this, "bt.cleaning_required", false);
      if (from_mayo_reuse) {
        writeBlackboard(
          *this, "bt.decision_reason",
          std::string("stable next-tool prediction selected a Mayo reuse tool for robot hold"));
        writeBlackboard(
          *this, "bt.rationale",
          std::string("prepare the stable predicted tool from Mayo and hold it on the robot"));
      }
      return BT::NodeStatus::SUCCESS;
    }

    if (mode == "recovery") {
      if (next_required_transition == "return_unused_preposition") {
        if (lifecycle != "prepositioned_right") {
          return BT::NodeStatus::FAILURE;
        }
        writeBlackboard(*this, "bt.action", std::string("return_unused_preposition"));
        writeBlackboard(*this, "bt.arm", std::string("right"));
        writeBlackboard(*this, "bt.source_location_id", std::string("robot_right_hand"));
        writeBlackboard(*this, "bt.source_location_type", std::string("robot_right_hand"));
        std::string return_location_id;
        std::string return_location_type;
        readBlackboard(
          *this, makeToolKey(selected_tool, "preposition_origin_location"),
          return_location_id);
        readBlackboard(
          *this, makeToolKey(selected_tool, "preposition_origin_type"),
          return_location_type);
        writeBlackboard(
          *this, "bt.target_location_id",
          return_location_id.empty() ? home_location_id : return_location_id);
        writeBlackboard(
          *this, "bt.target_location_type",
          return_location_type.empty() ? home_location_type : return_location_type);
        writeBlackboard(*this, "bt.target_owner", std::string("none"));
        writeBlackboard(*this, "bt.cleaning_required", false);
        const auto return_reason =
          policy_basis == "stable_prediction_replacement" ?
          std::string("stable replacement prediction frees the right-hand preparation slot") :
          policy_basis == "prediction_evidence_expired" ?
          std::string("prediction evidence expired; release the reversible preparation slot") :
          policy_basis == "preposition_dwell_expired" ?
          std::string("speculative preparation dwell expired; release the right-hand slot") :
          std::string("unused prepositioned tool must return to its source");
        writeBlackboard(*this, "bt.decision_reason", return_reason);
        writeBlackboard(*this, "bt.rationale", return_reason);
        return BT::NodeStatus::SUCCESS;
      }
      if (
        (lifecycle != "mayo_recovery" && lifecycle != "mayo_reuse") ||
        next_required_transition != "recover_left")
      {
        return BT::NodeStatus::FAILURE;
      }
      const auto recovery_source_location = std::string("mayo_stand");
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
      const auto recovery_reason = policy_basis.empty() ?
        std::string("mayo stand tool requires retrieve action") :
        std::string("BT recovery policy: ") + policy_basis;
      writeBlackboard(*this, "bt.decision_reason", recovery_reason);
      writeBlackboard(*this, "bt.rationale", recovery_reason);
      return BT::NodeStatus::SUCCESS;
    }

    return BT::NodeStatus::FAILURE;
  }
};

class ShouldDispatchDecision : public BT::SyncActionNode
{
public:
  explicit ShouldDispatchDecision(const std::string & name, const BT::NodeConfig & config, RosContext context)
  : BT::SyncActionNode(name, config)
  {
    (void)context;
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
    std::string right_hand_instance;
    std::string left_hand_instance;
    int64_t bundle_generation = 0;
    int64_t request_generation = 0;
    int64_t implicit_request_generation = 0;
    readBlackboard(*this, "selected.tool", selected_tool);
    readBlackboard(*this, "robot.right_hand_instance", right_hand_instance);
    readBlackboard(*this, "robot.left_hand_instance", left_hand_instance);
    readBlackboard(*this, "bundle.generation", bundle_generation);
    readBlackboard(*this, "request.generation", request_generation);
    readBlackboard(
      *this, "request.implicit_generation", implicit_request_generation);

    if (hasActiveRobotTask(*this)) {
      return BT::NodeStatus::FAILURE;
    }

    const auto signature = makeSignature(
      decision, action, rationale, selected_tool, selected_tool_lifecycle, next_required_transition,
      target_location_id, target_location_type, mode, arm, right_hand_instance,
      left_hand_instance, bundle_generation, request_generation,
      implicit_request_generation);

    std::string last_signature;
    readBlackboard(*this, "dispatch.last_signature", last_signature);

    // A physical command is emitted at most once for an unchanged decision and
    // observed world context. A retry requires a new request or a relevant
    // state transition, rather than the passage of wall-clock time.
    if (signature == last_signature) {
      return BT::NodeStatus::FAILURE;
    }

    writeBlackboard(*this, "dispatch.last_signature", signature);
    return BT::NodeStatus::SUCCESS;
  }

private:
  static std::string makeSignature(
    const std::string & decision, const std::string & action, const std::string & rationale,
    const std::string & selected_tool, const std::string & selected_tool_lifecycle,
    const std::string & next_required_transition, const std::string & target_location_id,
    const std::string & target_location_type, const std::string & mode, const std::string & arm,
    const std::string & right_hand_instance,
    const std::string & left_hand_instance, const int64_t bundle_generation,
    const int64_t request_generation, const int64_t implicit_request_generation)
  {
    std::ostringstream stream;
    stream << decision << "|" << action << "|" << rationale << "|" << selected_tool << "|" <<
      selected_tool_lifecycle << "|" << next_required_transition << "|" << target_location_id << "|" <<
      target_location_type << "|" << mode << "|" << arm << "|" << right_hand_instance << "|" <<
      left_hand_instance << "|" << bundle_generation << "|" <<
      request_generation << "|" << implicit_request_generation;
    return stream.str();
  }
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
    readBlackboard(
      *this, "selected.tool", msg.selected_tool_instance_id);
    msg.selected_tool = toolTypeId(*this, msg.selected_tool_instance_id);
    int64_t request_generation = 0;
    readBlackboard(*this, "request.generation", request_generation);
    msg.request_generation = static_cast<uint64_t>(std::max<int64_t>(0, request_generation));
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
    readBlackboard(
      *this, "selected.tool", msg.instrument_instance_id);
    msg.instrument_id = toolTypeId(*this, msg.instrument_instance_id);
    int64_t request_generation = 0;
    readBlackboard(*this, "request.generation", request_generation);
    msg.request_generation = static_cast<uint64_t>(std::max<int64_t>(0, request_generation));
    readBlackboard(*this, "bt.cleaning_required", msg.cleaning_required);
    const auto sequence = skill_command_sequence.fetch_add(1, std::memory_order_relaxed) + 1;
    msg.command_id =
      "skill-" + std::to_string(context_.getCurrentTime().nanoseconds()) + "-" +
      std::to_string(sequence);
    return !msg.action.empty();
  }
};

}  // namespace taskplanner_bt_nodes

AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::LoadWorldState)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::IsProcedureActive)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::IsPhaseCertain)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::HasExplicitRequest)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::HasImplicitRequest)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::NeedsRecovery)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::IsToolAvailable)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::CanHandover)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::CanPreposition)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::SelectExplicitTool)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::SelectImplicitTool)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::SelectExpectedTool)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::SelectRecoveryTool)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::SetIdleDecision)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::ApplyActionGuard)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::ConfigureHumanoidCommand)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::ShouldDispatchDecision)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::EmitBTDecision)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::PublishSkillCommand)
