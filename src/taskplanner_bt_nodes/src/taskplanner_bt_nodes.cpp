#include <algorithm>
#include <atomic>
#include <chrono>
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

std::atomic<uint64_t> skill_command_sequence{0};

// CAM4 admission is owned by the Digital Twin's continuous hand gate.  The
// BT consumes only its normalized visible/open_receive state; duplicating
// dwell or confidence thresholds here would make a valid hand signal wait a
// second time after the reducer already admitted it.
// Active/paused WorldState is emitted at 0.5 s cadence. A cached sample older
// than this cannot safely authorize Mayo motion if the Digital Twin process
// has stopped publishing.
constexpr int64_t kWorldStateMaxReceiptAgeNs = 1500000000LL;

int64_t steadyNowNs()
{
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
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

bool directHandSignalActive(const BT::TreeNode & node)
{
  bool visible = false;
  std::string execution_state;
  std::string implicit_tool;
  std::string hand_pose;
  readBlackboard(node, "runtime.execution_state", execution_state);
  readBlackboard(node, "request.implicit_visible", visible);
  readBlackboard(node, "request.implicit_tool", implicit_tool);
  readBlackboard(node, "request.implicit_hand_pose", hand_pose);
  return execution_state == "running" && visible && implicit_tool.empty() &&
         hand_pose == "open_receive";
}

bool mayoWorkspaceOccupied(const BT::TreeNode & node)
{
  bool present = true;
  readBlackboard(node, "perception.cam4_mayo_hand_present", present);
  return present;
}

bool isMayoAnchor(const std::string & value)
{
  static const std::unordered_set<std::string> mayo_anchors = {
    "mayo", "mayo_stand", "mayo_reuse_zone", "mayo_recovery_zone"};
  return mayo_anchors.count(value) > 0;
}

bool commandTouchesMayoWorkspace(
  const std::string & action, const std::string & selected_lifecycle,
  const std::string & source_location_id,
  const std::string & source_location_type,
  const std::string & target_location_id,
  const std::string & target_location_type)
{
  return
    action == "retrieve_from_mayo" ||
    action == "return_unused_preposition" ||
    selected_lifecycle == "mayo_reuse" ||
    selected_lifecycle == "mayo_recovery" ||
    isMayoAnchor(source_location_id) ||
    isMayoAnchor(source_location_type) ||
    isMayoAnchor(target_location_id) ||
    isMayoAnchor(target_location_type);
}

bool isExactRightHandPreposition(
  const BT::TreeNode & node, const std::string & selected_instance = {})
{
  std::string prepositioned_tool;
  std::string prepositioned_instance;
  std::string right_hand_instance;
  readBlackboard(node, "robot.prepositioned_tool", prepositioned_tool);
  readBlackboard(node, "robot.prepositioned_instance", prepositioned_instance);
  readBlackboard(node, "robot.right_hand_instance", right_hand_instance);
  if (
    prepositioned_tool.empty() || prepositioned_instance.empty() ||
    right_hand_instance.empty() ||
    prepositioned_instance != right_hand_instance ||
    (!selected_instance.empty() && selected_instance != prepositioned_instance) ||
    !toolIsActive(node, prepositioned_instance) ||
    !toolMatchesType(node, prepositioned_instance, prepositioned_tool) ||
    toolLifecycle(node, prepositioned_instance) != "prepositioned_right")
  {
    return false;
  }
  std::string owner;
  std::string location_id;
  std::string location_type;
  readBlackboard(node, makeToolKey(prepositioned_instance, "owner"), owner);
  readBlackboard(
    node, makeToolKey(prepositioned_instance, "location"), location_id);
  readBlackboard(
    node, makeToolKey(prepositioned_instance, "location_type"), location_type);
  // The Digital Twin's canonical event projection uses the generic `robot`
  // anchor for a tool that is physically held in the robot's right hand. The
  // endpoint action contract still names `robot_right_hand`; accepting either
  // representation here is safe only because the exact instance, lifecycle,
  // and right-hand owner checks above have already established laterality.
  const bool canonical_right_hand_location =
    (location_id == "robot_right_hand" &&
     location_type == "robot_right_hand") ||
    (location_id == "robot" && location_type == "robot");
  return owner == "robot_right_hand" && canonical_right_hand_location;
}

std::string findActiveInstanceForType(
  const BT::TreeNode & node, const std::string & instrument_type,
  const std::unordered_set<std::string> & allowed_lifecycles = {},
  bool prefer_mayo = false, bool allow_occupied_mayo = false)
{
  std::string first_eligible;
  const bool mayo_occupied = mayoWorkspaceOccupied(node);
  for (const auto & tool_id : allTools(node)) {
    if (!toolMatchesType(node, tool_id, instrument_type) || !toolIsActive(node, tool_id)) {
      continue;
    }
    if (
      allowed_lifecycles.empty() ||
      allowed_lifecycles.count(toolLifecycle(node, tool_id)) > 0)
    {
      const auto lifecycle = toolLifecycle(node, tool_id);
      const bool on_mayo =
        lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery";
      if (mayo_occupied && on_mayo && !allow_occupied_mayo) {
        continue;
      }
      if (!prefer_mayo) {
        return tool_id;
      }
      if (on_mayo) {
        return tool_id;
      }
      if (first_eligible.empty()) {
        first_eligible = tool_id;
      }
    }
  }
  return first_eligible;
}

bool operatorRequestedMayoPickup(
  const BT::TreeNode & node, const std::string & selected_tool)
{
  if (!toolIsActive(node, selected_tool)) {
    return false;
  }
  const auto lifecycle = toolLifecycle(node, selected_tool);
  const bool on_mayo =
    lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery";
  if (!on_mayo) {
    return false;
  }

  std::string location_id;
  std::string location_type;
  std::string owner;
  std::string active_task_id;
  std::string robot_state;
  bool cleaner_busy = false;
  readBlackboard(node, makeToolKey(selected_tool, "location"), location_id);
  readBlackboard(node, makeToolKey(selected_tool, "location_type"), location_type);
  readBlackboard(node, makeToolKey(selected_tool, "owner"), owner);
  readBlackboard(node, "robot.active_task_id", active_task_id);
  readBlackboard(node, "robot.state", robot_state);
  readBlackboard(node, "cleaner.busy", cleaner_busy);
  if (
    (!isMayoAnchor(location_id) && !isMayoAnchor(location_type)) ||
    (!owner.empty() && owner != "none") || !active_task_id.empty() ||
    cleaner_busy || robot_state == "fault")
  {
    return false;
  }

  std::string explicit_request;
  std::string surgeon_request;
  std::string surgeon_instance;
  std::string surgeon_intent;
  bool voice_backed = false;
  bool ready_for_handover = false;
  bool handover_hint = false;
  readBlackboard(node, "request.explicit_tool", explicit_request);
  readBlackboard(node, "request.surgeon_tool", surgeon_request);
  readBlackboard(node, "request.surgeon_instance", surgeon_instance);
  readBlackboard(node, "surgeon.intent", surgeon_intent);
  readBlackboard(node, "request.voice_backed", voice_backed);
  readBlackboard(node, "surgeon.ready_handover", ready_for_handover);
  readBlackboard(node, "state.handover_hint", handover_hint);
  const auto selected_tool_type = toolTypeId(node, selected_tool);
  const bool voice_explicit_selection =
    voice_backed && ready_for_handover && handover_hint &&
    surgeon_intent == "voice_request" &&
    !surgeon_instance.empty() && surgeon_instance == selected_tool &&
    (
      (!explicit_request.empty() && explicit_request == selected_tool_type) ||
      (!surgeon_request.empty() && surgeon_request == selected_tool_type)
    );

  return voice_explicit_selection;
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

  if (contaminated && lifecycle != "mayo_reuse") {
    return false;
  }
  if (!next_required_transition.empty()) {
    return false;
  }
  if (lifecycle == "mayo_reuse") {
    return true;
  }
  return lifecycle == "home_rack" || lifecycle == "returned_home";
}

std::string findAnticipatoryInstanceForType(
  const BT::TreeNode & node, const std::string & instrument_type)
{
  std::string first_eligible;
  const bool mayo_occupied = mayoWorkspaceOccupied(node);
  for (const auto & tool_id : allTools(node)) {
    if (
      toolMatchesType(node, tool_id, instrument_type) &&
      toolIsAnticipatoryCandidate(node, tool_id))
    {
      // A reusable Mayo instance avoids an unnecessary tray/rack round trip.
      // Eligibility is still decided above, so a Mayo recovery candidate is
      // never promoted into anticipatory preparation by this preference.
      if (toolLifecycle(node, tool_id) == "mayo_reuse") {
        if (mayo_occupied) {
          continue;
        }
        return tool_id;
      }
      if (first_eligible.empty()) {
        first_eligible = tool_id;
      }
    }
  }
  return first_eligible;
}

bool explicitRequestReplacesPreposition(const BT::TreeNode & node)
{
  std::string surgeon_intent;
  std::string requested_tool;
  std::string requested_instance;
  std::string prepositioned_tool;
  std::string prepositioned_instance;
  int64_t request_generation = 0;
  readBlackboard(node, "surgeon.intent", surgeon_intent);
  readBlackboard(node, "request.surgeon_tool", requested_tool);
  readBlackboard(node, "request.surgeon_instance", requested_instance);
  readBlackboard(node, "request.generation", request_generation);
  readBlackboard(node, "robot.prepositioned_tool", prepositioned_tool);
  readBlackboard(node, "robot.prepositioned_instance", prepositioned_instance);
  if (
    request_generation <= 0 || !isExplicitSurgeonIntent(surgeon_intent) ||
    prepositioned_tool.empty() || prepositioned_instance.empty())
  {
    return false;
  }
  if (!requested_instance.empty()) {
    return requested_instance != prepositioned_instance;
  }
  return !requested_tool.empty() && requested_tool != prepositioned_tool;
}

bool systemTopPredictionReplacesPreposition(const BT::TreeNode & node)
{
  std::string predicted_tool;
  std::string prepositioned_tool;
  std::string prepositioned_instance;
  std::string preposition_reservation;
  bool autonomous_ready = false;
  readBlackboard(node, "prediction.tool", predicted_tool);
  readBlackboard(node, "robot.prepositioned_tool", prepositioned_tool);
  readBlackboard(node, "robot.prepositioned_instance", prepositioned_instance);
  readBlackboard(node, "prediction.autonomous_ready", autonomous_ready);
  if (!prepositioned_instance.empty()) {
    readBlackboard(
      node, makeToolKey(prepositioned_instance, "reserved_for"),
      preposition_reservation);
  }
  const auto replacement_instance = findAnticipatoryInstanceForType(
    node, predicted_tool);
  const bool replacement_available = !replacement_instance.empty();
  return
    !predicted_tool.empty() && !prepositioned_tool.empty() &&
    predicted_tool != prepositioned_tool &&
    replacement_available &&
    autonomous_ready &&
    preposition_reservation != "voice_prepared";
}

bool isVoicePreparedPreposition(const BT::TreeNode & node, const std::string & tool_id)
{
  std::string reservation;
  readBlackboard(node, makeToolKey(tool_id, "reserved_for"), reservation);
  return reservation == "voice_prepared";
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
  if (explicitRequestReplacesPreposition(node)) {
    return true;
  }
  if (systemTopPredictionReplacesPreposition(node)) {
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
          next_required_transition == "return_home")
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
  return explicit_recovery;
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
    const auto now_ns = steadyNowNs();
    if (last_msg_ptr) {
      last_world_state_ = last_msg_ptr;
      last_world_state_receipt_ns_ = now_ns;
    }
    const bool receipt_fresh =
      last_world_state_receipt_ns_ >= 0 &&
      now_ns >= last_world_state_receipt_ns_ &&
      now_ns - last_world_state_receipt_ns_ <= kWorldStateMaxReceiptAgeNs;
    if (!last_world_state_ || !receipt_fresh) {
      writeBlackboard(
        *this, "perception.cam4_mayo_hand_present", true);
      writeBlackboard(*this, "bt.action", std::string{});
      writeBlackboard(
        *this, "bt.blocking_guard", std::string("world_state_stale"));
      clearCommandFields(*this, true);
      // RosSubscriberNode can return only a completed status.  The tree's
      // dedicated bounded input-yield wrapper converts this into the next
      // executor tick without spinning or treating it as an Action failure.
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
    writeBlackboard(*this, "runtime.procedure_run_id", msg.procedure_run_id);
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
    writeBlackboard(
      *this, "perception.cam4_mayo_hand_present",
      static_cast<bool>(msg.cam4_mayo_hand_present));
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
    writeBlackboard(
      *this, "prediction.autonomous_ready",
      static_cast<bool>(msg.autonomous_preparation_ready));
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
      writeBlackboard(*this, makeToolKey(instance_id, "reserved_for"), instrument.reserved_for);
      writeBlackboard(*this, makeToolKey(instance_id, "available"), isAvailableStatus(instrument.status));
      writeBlackboard(*this, makeToolKey(instance_id, "contaminated"), static_cast<bool>(instrument.contaminated));
      writeBlackboard(*this, makeToolKey(instance_id, "cleanliness"), instrument.cleanliness_state);
      writeBlackboard(*this, makeToolKey(instance_id, "lifecycle"), instrument.lifecycle_stage);
      writeBlackboard(
        *this, makeToolKey(instance_id, "next_required_transition"),
        instrument.next_required_transition);
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
  int64_t last_world_state_receipt_ns_{-1};
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
    if (hasActiveRobotTask(*this) || !directHandSignalActive(*this)) {
      return BT::NodeStatus::FAILURE;
    }
    // An implicit open-hand signal is delivery-only: it may hand over the
    // exact instance already prepared in the robot's right hand, but must
    // never turn the current rank-1 prediction into a new pickup command.
    return isExactRightHandPreposition(*this) ?
      BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
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
    return hasRecoveryContext(*this) ?
      BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
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
    const bool on_mayo = lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery";
    if (on_mayo) {
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
    std::string surgeon_intent;
    bool voice_backed = false;
    readBlackboard(*this, "selected.tool", selected_tool);
    readBlackboard(*this, "request.explicit_tool", explicit_request);
    readBlackboard(*this, "request.surgeon_tool", surgeon_request);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    readBlackboard(*this, "request.voice_backed", voice_backed);
    const auto selected_tool_type = toolTypeId(*this, selected_tool);
    const bool voice_backed_selected =
      voice_backed && !selected_tool.empty() &&
      (selected_tool_type == explicit_request ||
      (isExplicitSurgeonIntent(surgeon_intent) && selected_tool_type == surgeon_request));
    const bool direct_hand_preposition_selected =
      directHandSignalActive(*this) &&
      isExactRightHandPreposition(*this, selected_tool);
    // The visual signal only releases a tool already prepared in the right
    // hand. It is not an authorization to pick up the rank-1 prediction.
    if (hasBlockingSafetyFlag(
        *this,
        voice_backed_selected || direct_hand_preposition_selected))
    {
      return BT::NodeStatus::FAILURE;
    }
    if (hasActiveRobotTask(*this)) {
      return BT::NodeStatus::FAILURE;
    }
    if (!toolIsActive(*this, selected_tool)) {
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
      // The n-gram readiness bit is owned by the Digital Twin and does not
      // consume VLM output.  Keep a VLM outage observable, but do not make a
      // slow first current-epoch inference delay this deterministic,
      // right-hand-only preparation.  All other blocking flags still apply;
      // recovery keeps its separate VLM-health gate.
      hasBlockingSafetyFlag(*this, true))
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
      isExactRightHandPreposition(*this, right_hand_instance) &&
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
    // Digital Twin is the sole owner of supply selection for an explicit
    // request.  In particular, do not select an arbitrary active instance by
    // type here: that could turn a surgeon-owned or unknown instance into a
    // robot source after the Twin has rejected it.
    if (
      !surgeon_instance.empty() &&
      toolIsActive(*this, surgeon_instance) &&
      toolMatchesType(*this, surgeon_instance, requested_tool_type))
    {
      writeBlackboard(*this, "selected.tool", surgeon_instance);
      writeBlackboard(*this, "selected.policy_transition", std::string{});
      writeBlackboard(*this, "selected.policy_basis", std::string("explicit_request"));
      return BT::NodeStatus::SUCCESS;
    }
    if (tool_id.empty()) {
      return BT::NodeStatus::FAILURE;
    }
    return BT::NodeStatus::FAILURE;
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
    std::string policy_basis = "hand_handover_signal";
    std::string prepositioned_tool;
    std::string prepositioned_instance;
    readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
    readBlackboard(*this, "robot.prepositioned_instance", prepositioned_instance);
    if (isExactRightHandPreposition(*this)) {
      tool_type = prepositioned_tool;
      selected_instance = prepositioned_instance;
      policy_basis = "hand_signal_preposition_match";
    } else {
      return BT::NodeStatus::FAILURE;
    }
    if (tool_type.empty() || selected_instance.empty()) {
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
    bool autonomous_preparation_ready = false;
    readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
    readBlackboard(*this, "prediction.tool", predicted_tool);
    readBlackboard(
      *this, "prediction.autonomous_ready", autonomous_preparation_ready);
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

    if (!predicted_tool.empty() && autonomous_preparation_ready)
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
    if (mayoWorkspaceOccupied(*this)) {
      writeBlackboard(
        *this, "bt.blocking_guard", std::string("cam4_mayo_hand_present"));
      return BT::NodeStatus::FAILURE;
    }
    if (
      (execution_state != "running" && execution_state != "finishing") ||
      hasActiveRobotTask(*this) || !left_hand_tool.empty() || cleaner_busy ||
      hasBlockingSafetyFlag(*this))
    {
      return BT::NodeStatus::FAILURE;
    }

    // Spoken completion can leave a tool prepared in the right hand. Return
    // that held tool before any Mayo-to-tray cleanup: otherwise the left arm
    // can start recovering a Mayo tool while the right hand still carries the
    // prepared item that completion is meant to clear first.
    if (execution_state == "finishing") {
      for (const auto & tool_id : allTools(*this)) {
        const auto lifecycle = toolLifecycle(*this, tool_id);
        const auto transition = toolNextRequiredTransition(*this, tool_id);
        if (lifecycle != "prepositioned_right") {
          continue;
        }
        if (transition == "return_unused_preposition") {
          return selectTool(
            tool_id, "return_unused_preposition", "completion_release_excluded_preposition");
        }
        if (transition == "return_preposition_to_tray") {
          return selectTool(
            tool_id, "return_preposition_to_tray", "completion_return_preposition_to_tray");
        }
      }
    }

    // Retrieval uses the left hand, but the Mayo lane is single-capacity:
    // never start a second transfer while the right hand still holds a
    // prepositioned tool.  Completion cleanup above is intentionally allowed
    // to return that right-hand tool first; the next BT tick can then retry the
    // queued Mayo recovery with an empty right hand.
    std::string right_hand_tool;
    std::string right_hand_instance;
    readBlackboard(*this, "robot.right_hand_tool", right_hand_tool);
    readBlackboard(*this, "robot.right_hand_instance", right_hand_instance);
    if (!right_hand_tool.empty() || !right_hand_instance.empty()) {
      writeBlackboard(
        *this, "bt.blocking_guard",
        std::string("retrieve_blocked_right_hand_preposition"));
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

    // Spoken completion freezes the eligible Mayo *instances* in the DT.
    // Those targets keep their evidence-derived mayo_reuse label, while their
    // next transition carries the terminal recovery authorization.  Select
    // exactly that transition so a later generic Mayo observation cannot add
    // a new cleanup item.
    if (execution_state == "finishing") {
      for (const auto & tool_id : allTools(*this)) {
        const auto lifecycle = toolLifecycle(*this, tool_id);
        if (
          (lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery") &&
          toolNextRequiredTransition(*this, tool_id) == "recover_left")
        {
          return selectTool(
            tool_id, "recover_left", "completion_frozen_mayo_target");
        }
      }
    }

    for (const auto & tool_id : allTools(*this)) {
      const auto lifecycle = toolLifecycle(*this, tool_id);
      if (lifecycle == "mayo_recovery") {
        return selectTool(
          tool_id, "recover_left", "observed_mayo_recovery_state");
      }
    }

    if (explicitRequestReplacesPreposition(*this)) {
      for (const auto & tool_id : allTools(*this)) {
        const auto lifecycle = toolLifecycle(*this, tool_id);
        if (
          toolNextRequiredTransition(*this, tool_id) == "return_unused_preposition" &&
          lifecycle == "prepositioned_right")
        {
          return selectTool(
            tool_id, "return_unused_preposition", "explicit_request_replacement");
        }
      }
    }

    if (systemTopPredictionReplacesPreposition(*this)) {
      std::string prepositioned_tool;
      readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
      for (const auto & tool_id : allTools(*this)) {
        if (
          toolLifecycle(*this, tool_id) == "prepositioned_right" &&
          toolMatchesType(*this, tool_id, prepositioned_tool) &&
          !isVoicePreparedPreposition(*this, tool_id))
        {
          return selectTool(
            tool_id, "return_unused_preposition",
            "dt_authorized_prediction_replacement");
        }
      }
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
      home_location_id = "mayo_stand";
      home_location_type = "mayo_stand";
    }
    writeBlackboard(*this, "selected.tool", tool_id);
    writeBlackboard(*this, "selected.policy_transition", policy_transition);
    writeBlackboard(*this, "selected.policy_basis", policy_basis);
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
    std::string robot_state;
    std::string active_task_id;
    std::string explicit_request;
    std::string surgeon_request;
    std::string surgeon_intent;
    std::string prepositioned_tool;
    std::string owner;
    const auto lifecycle = toolLifecycle(*this, selected_tool);
    bool contaminated = false;
    bool cleaner_busy = false;
    bool ready_for_handover = false;
    bool voice_backed = false;
    bool handover_hint = false;
    readBlackboard(*this, "robot.state", robot_state);
    readBlackboard(*this, "robot.active_task_id", active_task_id);
    readBlackboard(*this, "request.explicit_tool", explicit_request);
    readBlackboard(*this, "request.surgeon_tool", surgeon_request);
    readBlackboard(*this, "surgeon.intent", surgeon_intent);
    readBlackboard(*this, "surgeon.ready_handover", ready_for_handover);
    readBlackboard(*this, "request.voice_backed", voice_backed);
    readBlackboard(*this, "robot.prepositioned_tool", prepositioned_tool);
    readBlackboard(*this, "cleaner.busy", cleaner_busy);
    readBlackboard(*this, "state.handover_hint", handover_hint);
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
    const bool exact_right_hand_preposition =
      isExactRightHandPreposition(*this, selected_tool);
    const bool implicit_request_selected =
      directHandSignalActive(*this) &&
      exact_right_hand_preposition &&
      selected_tool_type == prepositioned_tool;
    const bool voice_backed_explicit_request =
      explicit_request_selected && voice_backed;
    const bool active_tool = toolIsActive(*this, selected_tool);
    const bool prepositioned_right = lifecycle == "prepositioned_right";
    const bool holder_available = prepositioned_right ?
      exact_right_hand_preposition : (owner.empty() || owner == "none");
    const bool usable_lifecycle = lifecycle == "home_rack" || lifecycle == "returned_home" || prepositioned_right;
    const bool on_mayo = lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery";
    const bool direct_hand_preposition_selected =
      implicit_request_selected && exact_right_hand_preposition;
    const bool operator_requested_mayo_handover =
      on_mayo && operatorRequestedMayoPickup(*this, selected_tool);
    // A direct hand signal may only release the exact tool already prepared
    // in the right hand. It never authorizes a new rank-1 pickup.
    const bool blocked_by_safety = hasBlockingSafetyFlag(
      *this,
      voice_backed_explicit_request || direct_hand_preposition_selected);
    const bool request_ready =
      (explicit_request_selected && ready_for_handover) ||
      implicit_request_selected ||
      (!explicit_request_selected && !implicit_request_selected);
    const bool robot_task_slot_available = active_task_id.empty();
    // `handover_hint` is not advisory.  It is the Digital Twin's authoritative
    // supply admission for the current explicit request.  BT only consumes
    // that decision; it must not re-select a surgeon-owned or unknown source.
    const bool source_admitted = !explicit_request_selected || handover_hint;
    const bool mayo_handover_allowed =
      on_mayo &&
      (!mayoWorkspaceOccupied(*this) || operator_requested_mayo_handover) &&
      holder_available &&
      robot_task_slot_available && !cleaner_busy &&
      robot_state != "fault" && request_ready;

    const bool allowed =
      active_tool &&
      source_admitted &&
      !blocked_by_safety &&
      (
        mayo_handover_allowed ||
        (
          usable_lifecycle && holder_available && !contaminated && robot_task_slot_available &&
          !cleaner_busy && robot_state != "fault" && request_ready
        )
      );

    if (explicit_request_selected && !source_admitted) {
      writeBlackboard(
        *this, "bt.blocking_guard", std::string("requested_tool_not_robot_reachable"));
    }
    if (
      on_mayo && mayoWorkspaceOccupied(*this) &&
      !operator_requested_mayo_handover)
    {
      writeBlackboard(
        *this, "bt.blocking_guard", std::string("cam4_mayo_hand_present"));
    }
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
    const bool voice_prepared_preposition =
      isVoicePreparedPreposition(*this, selected_tool);
    std::string policy_basis;
    readBlackboard(*this, "selected.policy_basis", policy_basis);
    if (next_required_transition.empty()) {
      readBlackboard(
        *this, "selected.policy_transition", next_required_transition);
    }
    const bool operator_requested_mayo_handover =
      (mode == "explicit_request" || mode == "implicit_request") &&
      operatorRequestedMayoPickup(*this, selected_tool);

    if (
      mayoWorkspaceOccupied(*this) &&
      !operator_requested_mayo_handover &&
      (
        lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery" ||
        next_required_transition == "return_unused_preposition"
      ))
    {
      writeBlackboard(*this, "bt.action", std::string{});
      clearCommandFields(*this, false);
      writeBlackboard(
        *this, "bt.blocking_guard", std::string("cam4_mayo_hand_present"));
      return BT::NodeStatus::FAILURE;
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
    bool voice_backed = false;
    readBlackboard(
      *this, "robot.right_hand_instance", right_hand_instance);
    readBlackboard(*this, "request.voice_backed", voice_backed);
    if (mode == "explicit_request" || mode == "implicit_request") {
      if (lifecycle == "surgeon_owned") {
        writeBlackboard(*this, "bt.action", std::string{});
        writeBlackboard(
          *this, "bt.blocking_guard", std::string("requested_tool_not_robot_reachable"));
        writeBlackboard(
          *this, "bt.decision_reason", std::string("requested tool is surgeon-owned"));
        writeBlackboard(
          *this, "bt.rationale", std::string("requested tool is surgeon-owned"));
        return BT::NodeStatus::FAILURE;
      }
      const bool on_mayo =
        lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery";
      if (contaminated && !on_mayo) {
        return BT::NodeStatus::FAILURE;
      }
      const bool exact_direct_hand_preposition =
        mode == "implicit_request" &&
        directHandSignalActive(*this) &&
        isExactRightHandPreposition(*this, selected_tool);
      if (mode == "implicit_request" && !exact_direct_hand_preposition) {
        writeBlackboard(*this, "bt.action", std::string{});
        writeBlackboard(
          *this, "bt.blocking_guard",
          std::string("implicit_request_requires_prepositioned_right_tool"));
        return BT::NodeStatus::FAILURE;
      }
      const bool explicit_right_hand_selection =
        mode == "explicit_request" &&
        right_hand_instance == selected_tool &&
        isExactRightHandPreposition(*this, selected_tool);
      const bool voice_explicit_request =
        mode == "explicit_request" && voice_backed;
      if (exact_direct_hand_preposition || explicit_right_hand_selection)
      {
        writeBlackboard(*this, "bt.action", std::string("direct_handover"));
        writeBlackboard(*this, "bt.source_location_id", std::string("robot_right_hand"));
        writeBlackboard(*this, "bt.source_location_type", std::string("robot_right_hand"));
      } else if (!right_hand_instance.empty()) {
        if (
          mode != "explicit_request" ||
          !explicitRequestReplacesPreposition(*this))
        {
          // A direct hand signal is not an explicit request for a different
          // tool. Let the DT-authorized replacement path settle the existing
          // preposition before an autonomous replacement is attempted.
          writeBlackboard(
            *this, "bt.blocking_guard",
            std::string("non_explicit_request_waiting_for_system_top_replacement"));
          return BT::NodeStatus::FAILURE;
        }
        if (hasActiveRobotTask(*this)) {
          writeBlackboard(
            *this, "bt.blocking_guard",
            std::string("active_tool_action"));
          return BT::NodeStatus::FAILURE;
        }
        if (mayoWorkspaceOccupied(*this)) {
          writeBlackboard(
            *this, "bt.blocking_guard", std::string("cam4_mayo_hand_present"));
          return BT::NodeStatus::FAILURE;
        }
        const auto held_lifecycle = toolLifecycle(*this, right_hand_instance);
        if (
          !toolIsActive(*this, right_hand_instance) ||
          held_lifecycle != "prepositioned_right")
        {
          writeBlackboard(*this, "bt.blocking_guard", std::string("right_hand_occupied"));
          return BT::NodeStatus::FAILURE;
        }
        // The public interface has no atomic "put down then hand over" action.
        // Park the currently prepared instance on Mayo first and leave the
        // explicit or direct hand signal untouched. This is the intentionally
        // short robot-to-Mayo leg; the next WorldState tick can select and hand
        // over the requested instance without a rack/home round trip.
        writeBlackboard(*this, "selected.tool", right_hand_instance);
        writeBlackboard(*this, "bt.selected_tool_lifecycle", held_lifecycle);
        writeBlackboard(
          *this, "bt.next_required_transition", std::string("return_unused_preposition"));
        writeBlackboard(*this, "bt.action", std::string("return_unused_preposition"));
        writeBlackboard(*this, "bt.arm", std::string("right"));
        writeBlackboard(*this, "bt.source_location_id", std::string("robot_right_hand"));
        writeBlackboard(*this, "bt.source_location_type", std::string("robot_right_hand"));
        writeBlackboard(
          *this, "bt.target_location_id", std::string("mayo_stand"));
        writeBlackboard(
          *this, "bt.target_location_type", std::string("mayo_stand"));
        writeBlackboard(*this, "bt.target_owner", std::string("none"));
        writeBlackboard(*this, "bt.cleaning_required", false);
        writeBlackboard(
          *this, "bt.decision_reason",
          std::string("right hand occupied; park held tool on Mayo before requested handover"));
        writeBlackboard(
          *this, "bt.rationale",
          std::string("right hand occupied; park held tool on Mayo before requested handover"));
        return BT::NodeStatus::SUCCESS;
      } else if (on_mayo) {
        // The public interface supports Mayo -> robot preparation and robot ->
        // surgeon handover as two audited transitions, not an atomic composite.
        // A voice request completes at the controller-confirmed preparation
        // transition; an implicit hand signal may later request handover.
        if (tool_location.empty() || tool_location_type.empty()) {
          writeBlackboard(
            *this, "bt.blocking_guard", std::string("requested_tool_location_unconfirmed"));
          return BT::NodeStatus::FAILURE;
        }
        writeBlackboard(*this, "bt.action", std::string("prepare_tool"));
        writeBlackboard(*this, "bt.source_location_id", tool_location);
        writeBlackboard(*this, "bt.source_location_type", tool_location_type);
        writeBlackboard(*this, "bt.arm", std::string("right"));
        writeBlackboard(*this, "bt.target_location_id", std::string("robot_right_hand"));
        writeBlackboard(*this, "bt.target_location_type", std::string("robot_right_hand"));
        writeBlackboard(*this, "bt.target_owner", std::string("robot_right_hand"));
        writeBlackboard(*this, "bt.cleaning_required", false);
        writeBlackboard(
          *this, "bt.decision_reason",
          std::string("requested tool is on Mayo; prepare it before handover"));
        writeBlackboard(
          *this, "bt.rationale",
          std::string("requested tool is on Mayo; prepare it before handover"));
        return BT::NodeStatus::SUCCESS;
      } else {
        if (
          (lifecycle != "home_rack" && lifecycle != "returned_home") ||
          tool_location.empty() || tool_location_type.empty())
        {
          writeBlackboard(
            *this, "bt.blocking_guard", std::string("requested_tool_not_robot_reachable"));
          return BT::NodeStatus::FAILURE;
        }
        if (voice_explicit_request) {
          writeBlackboard(*this, "bt.action", std::string("prepare_tool"));
          writeBlackboard(*this, "bt.source_location_id", tool_location);
          writeBlackboard(*this, "bt.source_location_type", tool_location_type);
          writeBlackboard(*this, "bt.arm", std::string("right"));
          writeBlackboard(
            *this, "bt.target_location_id", std::string("robot_right_hand"));
          writeBlackboard(
            *this, "bt.target_location_type", std::string("robot_right_hand"));
          writeBlackboard(
            *this, "bt.target_owner", std::string("robot_right_hand"));
          writeBlackboard(*this, "bt.cleaning_required", false);
          writeBlackboard(
            *this, "bt.decision_reason",
            std::string("voice-requested tool is prepared for a later hand signal"));
          writeBlackboard(
            *this, "bt.rationale",
            std::string("voice-requested tool is prepared for a later hand signal"));
          return BT::NodeStatus::SUCCESS;
        }
        writeBlackboard(*this, "bt.action", std::string("pick_up_and_handover"));
        writeBlackboard(*this, "bt.source_location_id", tool_location);
        writeBlackboard(*this, "bt.source_location_type", tool_location_type);
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
      if (
        next_required_transition == "return_unused_preposition" ||
        next_required_transition == "return_preposition_to_tray")
      {
        if (
          next_required_transition == "return_unused_preposition" &&
          voice_prepared_preposition)
        {
          writeBlackboard(
            *this, "bt.blocking_guard",
            std::string("voice_prepared_waiting_for_hand"));
          writeBlackboard(*this, "bt.action", std::string{});
          return BT::NodeStatus::FAILURE;
        }
        if (lifecycle != "prepositioned_right") {
          return BT::NodeStatus::FAILURE;
        }
        const bool return_to_mayo =
          next_required_transition == "return_unused_preposition";
        writeBlackboard(
          *this, "bt.action",
          return_to_mayo ? std::string("return_unused_preposition") :
          std::string("return_preposition_to_tray"));
        writeBlackboard(*this, "bt.arm", std::string("right"));
        writeBlackboard(*this, "bt.source_location_id", std::string("robot_right_hand"));
        writeBlackboard(*this, "bt.source_location_type", std::string("robot_right_hand"));
        writeBlackboard(
          *this, "bt.target_location_id",
          return_to_mayo ? std::string("mayo_stand") : home_location_id);
        writeBlackboard(
          *this, "bt.target_location_type",
          return_to_mayo ? std::string("mayo_stand") : home_location_type);
        writeBlackboard(*this, "bt.target_owner", std::string("none"));
        writeBlackboard(*this, "bt.cleaning_required", false);
        const auto return_reason =
          !return_to_mayo ?
          std::string("completion cleanup returns prepared recovery tool directly to tray") :
          policy_basis == "dt_authorized_prediction_replacement" ?
          std::string("DT-authorized n-gram prediction changed; park current preparation on Mayo") :
          policy_basis == "explicit_request_replacement" ?
          std::string("explicit request changed tools; park current preparation on Mayo") :
          std::string("unused prepositioned tool must be parked on Mayo to free the right hand");
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
    std::string source_location_id;
    std::string source_location_type;
    readBlackboard(*this, "bt.source_location_id", source_location_id);
    readBlackboard(*this, "bt.source_location_type", source_location_type);
    const auto mode = firstInputOrBlackboard(*this, "mode", "bt.mode");
    const auto arm = firstInputOrBlackboard(*this, "arm", "bt.arm");
    const auto selected_tool_lifecycle =
      firstInputOrBlackboard(*this, "selected_tool_lifecycle", "bt.selected_tool_lifecycle");
    const auto next_required_transition =
      firstInputOrBlackboard(*this, "next_required_transition", "bt.next_required_transition");

    std::string selected_tool;
    std::string right_hand_instance;
    std::string left_hand_instance;
    std::string procedure_run_id;
    int64_t bundle_generation = 0;
    int64_t request_generation = 0;
    int64_t implicit_request_generation = 0;
    readBlackboard(*this, "selected.tool", selected_tool);
    readBlackboard(*this, "robot.right_hand_instance", right_hand_instance);
    readBlackboard(*this, "robot.left_hand_instance", left_hand_instance);
    readBlackboard(*this, "runtime.procedure_run_id", procedure_run_id);
    readBlackboard(*this, "bundle.generation", bundle_generation);
    readBlackboard(*this, "request.generation", request_generation);
    readBlackboard(
      *this, "request.implicit_generation", implicit_request_generation);

    // Defense in depth: even if a stale recovery decision reaches dispatch
    // after the Twin has reserved a voice-prepared tool, never park it until
    // the surgeon signals or explicitly requests a replacement.
    if (
      decision == "recovery" &&
      action == "return_unused_preposition" &&
      isVoicePreparedPreposition(*this, selected_tool))
    {
      writeBlackboard(
        *this, "bt.blocking_guard",
        std::string("voice_prepared_waiting_for_hand"));
      return BT::NodeStatus::FAILURE;
    }

    const bool operator_requested_mayo_handover =
      (decision == "explicit_request" || decision == "implicit_request") &&
      action == "prepare_tool" &&
      (selected_tool_lifecycle == "mayo_reuse" ||
      selected_tool_lifecycle == "mayo_recovery") &&
      (isMayoAnchor(source_location_id) || isMayoAnchor(source_location_type)) &&
      target_location_id == "robot_right_hand" &&
      target_location_type == "robot_right_hand" &&
      operatorRequestedMayoPickup(*this, selected_tool);

    if (
      mayoWorkspaceOccupied(*this) &&
      !operator_requested_mayo_handover &&
      commandTouchesMayoWorkspace(
        action, selected_tool_lifecycle, source_location_id,
        source_location_type, target_location_id, target_location_type))
    {
      writeBlackboard(
        *this, "bt.blocking_guard", std::string("cam4_mayo_hand_present"));
      return BT::NodeStatus::FAILURE;
    }

    // No request provenance can preempt or overlap a tracked external tool
    // Action. Any later retry is a new admission decision after the active
    // Action's terminal result has been projected into WorldState.
    if (hasActiveRobotTask(*this)) {
      return BT::NodeStatus::FAILURE;
    }

    const bool implicit_handover_action =
      action == "direct_handover" || action == "pick_up_and_handover";
    std::string implicit_episode_signature;
    if (
      decision == "implicit_request" &&
      (procedure_run_id.empty() || implicit_request_generation <= 0))
    {
      return BT::NodeStatus::FAILURE;
    }
    if (decision == "implicit_request" && implicit_handover_action) {
      // One continuous direct hand-signal episode authorizes at most one handover.
      // Inventory/lifecycle changes after a successful handover must not turn
      // the same hand-signal episode into a request for a second instance or a new rank-1
      // prediction.  The reducer re-arms this only after its fresh-negative
      // release debounce creates a new episode.
      implicit_episode_signature =
        procedure_run_id + "|" +
        std::to_string(implicit_request_generation);
      std::string last_implicit_episode;
      readBlackboard(*this, "dispatch.last_implicit_episode", last_implicit_episode);
      if (implicit_episode_signature == last_implicit_episode) {
        return BT::NodeStatus::FAILURE;
      }
    }

    const auto signature = makeSignature(
      decision, action, rationale, selected_tool, selected_tool_lifecycle, next_required_transition,
      target_location_id, target_location_type, mode, arm, right_hand_instance,
      left_hand_instance, procedure_run_id, bundle_generation, request_generation,
      implicit_request_generation);

    std::string last_signature;
    readBlackboard(*this, "dispatch.last_signature", last_signature);

    // A physical command is emitted at most once for an unchanged decision and
    // observed world context. A retry requires a new request or a relevant
    // state transition, rather than the passage of wall-clock time.
    if (signature == last_signature) {
      return BT::NodeStatus::FAILURE;
    }

    if (!implicit_episode_signature.empty()) {
      writeBlackboard(
        *this, "dispatch.last_implicit_episode", implicit_episode_signature);
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
    const std::string & left_hand_instance, const std::string & procedure_run_id,
    const int64_t bundle_generation,
    const int64_t request_generation, const int64_t implicit_request_generation)
  {
    std::ostringstream stream;
    stream << decision << "|" << action << "|" << rationale << "|" << selected_tool << "|" <<
      selected_tool_lifecycle << "|" << next_required_transition << "|" << target_location_id << "|" <<
      target_location_type << "|" << mode << "|" << arm << "|" << right_hand_instance << "|" <<
      left_hand_instance << "|" << procedure_run_id << "|" << bundle_generation << "|" <<
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
    readBlackboard(*this, "runtime.procedure_run_id", msg.procedure_run_id);
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
    bool request_voice_backed = false;
    readBlackboard(*this, "request.voice_backed", request_voice_backed);
    // Voice provenance belongs only to the explicit-request command that was
    // selected from that request.  A stale flag must never promote an
    // implicit or anticipatory command to the bridge's preemptive class.
    msg.voice_backed = request_voice_backed && msg.mode == "explicit_request";
    int64_t request_generation = 0;
    readBlackboard(*this, "request.generation", request_generation);
    msg.request_generation = static_cast<uint64_t>(std::max<int64_t>(0, request_generation));
    // Every emitted execution command is bound to the exact accepted
    // procedure interval.  This is not limited to direct-hand commands:
    // a late explicit/prediction callback must never be admissible in the
    // next run either.
    readBlackboard(*this, "runtime.procedure_run_id", msg.procedure_run_id);
    if (msg.procedure_run_id.empty()) {
      return false;
    }
    msg.implicit_request_generation = 0;
    readBlackboard(*this, "bt.cleaning_required", msg.cleaning_required);
    if (msg.mode == "implicit_request") {
      int64_t implicit_request_generation = 0;
      readBlackboard(
        *this, "request.implicit_generation", implicit_request_generation);
      if (implicit_request_generation <= 0) {
        return false;
      }
      msg.implicit_request_generation = static_cast<uint64_t>(
        implicit_request_generation);
      // A stable ID lets the bridge/controller recognize a replay after a BT
      // restart. The durable bridge ledger separately fences all handover
      // variants to one handover per run/episode.
      msg.command_id =
        "skill-hand-" + msg.procedure_run_id + "-" +
        std::to_string(implicit_request_generation) + "-" + msg.action;
    } else {
      const auto sequence =
        skill_command_sequence.fetch_add(1, std::memory_order_relaxed) + 1;
      msg.command_id =
        "skill-" + std::to_string(context_.getCurrentTime().nanoseconds()) + "-" +
        std::to_string(sequence);
    }
    return !msg.action.empty();
  }
};

}  // namespace taskplanner_bt_nodes

AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::LoadWorldState)
AUTO_APMS_BEHAVIOR_TREE_REGISTER_NODE(taskplanner_bt_nodes::IsProcedureActive)
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
