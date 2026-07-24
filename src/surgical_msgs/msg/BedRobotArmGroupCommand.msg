string GROUP_SUCTION="suction"
string GROUP_RETRACTION="retraction"

string OPERATION_SUCTION_START="suction_start"
string OPERATION_SUCTION_STOP="suction_stop"
string OPERATION_RETRACTION="retraction"
string OPERATION_RELEASE_RETRACTION="release_retraction"
string OPERATION_CHANGE_END_EFFECTOR="change_end_effector"

string DIRECTION_UP="UP"
string DIRECTION_DOWN="DOWN"
string DIRECTION_LEFT="LEFT"
string DIRECTION_RIGHT="RIGHT"
string DIRECTION_LEFT_RIGHT="LEFT_RIGHT"
string DIRECTION_UP_DOWN="UP_DOWN"

string DISTANCE_EXPLICIT_WITH_UNIT="explicit_with_unit"
string DISTANCE_EXPLICIT_UNIT_INFERRED="explicit_unit_inferred"
string DISTANCE_QUALITATIVE_INFERRED="qualitative_inferred"
string DISTANCE_DEFAULTED="defaulted"

builtin_interfaces/Time stamp
string request_id
string command_id
string group_id
string operation
string direction
float32 distance_mm
string distance_origin
string raw_distance_text
string end_effector_profile
string rationale
float32 confidence
