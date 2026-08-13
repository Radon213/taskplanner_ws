# Latest robot end-effector possession snapshot. While a procedure is active,
# configured end effectors remain present and use state=empty or state=unknown
# when no tool can be named. The array is empty while idle or source-stale.
builtin_interfaces/Time stamp
uint64 revision
string schema_version
string catalog_version
string gateway_instance_id
string procedure_run_id
string procedure_type
bool procedure_active
surgical_interop_msgs/RobotEndEffectorState[] end_effectors
