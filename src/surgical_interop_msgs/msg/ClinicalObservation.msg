# Public observation from a clinical model. It is not automatically a confirmed
# clinical fact; evidence_status declares its authority.
builtin_interfaces/Time stamp
uint64 sequence
string source
string summary
string[] phase_ids
float32[] phase_confidences
string[] observed_tool_ids
string[] observed_location_types
string[] observed_location_ids
float32[] observed_confidences
string gesture_event_type
string gesture_requested_tool
string gesture_hand_pose
float32 gesture_confidence
float32 uncertainty
string evidence_status
