string GROUP_RETRACTION="retraction"

# The legacy envelope remains while the public bridge maps compatible commands
# to the single ExecuteRetractionCommand Service.
string OPERATION_RETRACTION_ADJUSTMENT="retraction"
string OPERATION_TOOL_CHANGE="change_end_effector"

string ADJUSTMENT_SINGLE="single"
string ADJUSTMENT_MULTI="multi"

string TARGET_LEFT_MALLEABLE="left_malleable"
string TARGET_RIGHT_MALLEABLE="right_malleable"
string TARGET_BOTH_MALLEABLE="both_malleable"

string DIRECTION_FRAME_SURGEON_VIEW="surgeon_view"

string DIRECTION_UP="up"
string DIRECTION_DOWN="down"
string DIRECTION_LEFT="left"
string DIRECTION_RIGHT="right"
string DIRECTION_NONE="none"

string AXIS_LEFT_RIGHT="left_right"
string AXIS_UP_DOWN="up_down"
string AXIS_NONE="none"

string ARM_1="arm_1"
string ARM_2="arm_2"

string TOOL_THYROID_RETRACTOR="thyroid_retractor"
string TOOL_ARMY_NAVY_RETRACTOR="army_navy_retractor"

string DISTANCE_EXPLICIT_WITH_UNIT="explicit_with_unit"
string DISTANCE_EXPLICIT_UNIT_INFERRED="explicit_unit_inferred"
string DISTANCE_QUALITATIVE_INFERRED="qualitative_inferred"
string DISTANCE_DEFAULTED="defaulted"

builtin_interfaces/Time stamp
string request_id
string command_id
string group_id
string operation
string arm_id
string target_tool_id
string adjustment_mode
string target_retractor_id
string direction_frame
string direction
string axis
float32 distance_mm
string distance_origin
string raw_distance_text
string end_effector_profile
string rationale
float32 confidence
