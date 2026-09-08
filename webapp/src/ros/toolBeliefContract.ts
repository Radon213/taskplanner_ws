/** Lightweight transport contract shared by planning, subscription, and UI code. */
export const TOOL_BELIEF_TOPIC = "/surgery/perception/tool_beliefs";
export const TOOL_BELIEF_MESSAGE_TYPE =
  "surgical_perception_msgs/msg/TrackedToolBeliefArray";
export const TOOL_BELIEF_ENABLED_TOPIC =
  "/surgery/perception/tool_beliefs/enabled";
export const TOOL_BELIEF_ENABLED_MESSAGE_TYPE = "std_msgs/msg/Bool";
export const SET_TOOL_BELIEF_ENABLED_SERVICE =
  "/surgery/perception/tool_beliefs/set_enabled";
export const SET_TOOL_BELIEF_ENABLED_SERVICE_TYPE = "std_srvs/srv/SetBool";
