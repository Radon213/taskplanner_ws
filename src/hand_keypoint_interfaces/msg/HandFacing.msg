# One depth-backed semantic palm-surface orientation observation.
# hand_index is frame-local and matches Hand/HandGesture only when the parent
# Header is identical. This is perception evidence, not robot authority.

int32 hand_index

bool has_handedness
string handedness_label
float32 handedness_score

# has_facing=false is the fail-closed UNKNOWN state. Valid labels are
# PALM_UP, PALM_DOWN, or EDGE.
bool has_facing
string facing_label
float32 palm_up_score
geometry_msgs/Vector3 palm_normal_cam
float32 palm_plane_residual_m
float32 support_height_m
uint8 valid_depth_points
string rejection_reason
