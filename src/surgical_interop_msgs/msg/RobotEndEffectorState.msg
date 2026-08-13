# Public semantic possession state for one robot end effector. This deliberately
# contains no joint, pose, force, trajectory, or medical-device control values.
string STATE_UNKNOWN="unknown"
string STATE_EMPTY="empty"
string STATE_HOLDING="holding"

builtin_interfaces/Time stamp
string robot_id
string end_effector_id

# unknown: possession could not be established; empty: known to hold no tool;
# holding: instrument_id identifies the held tool.
string state
string instrument_id
string instance_id

# Confidence is finite and clamped to [0.0, 1.0].
float32 confidence
string evidence_status
