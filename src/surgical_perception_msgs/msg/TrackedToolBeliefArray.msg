# Observation-only, scenario-bounded real-to-sim belief snapshot. Individual
# rows declare strict instance versus exchangeable capacity semantics through
# status_flags; neither mode grants control authority.
std_msgs/Header header
string schema_version
string procedure_id
string procedure_run_id
string tracker_revision
bool observation_only
bool robot_motion_active
float32 robot_motion_negative_scale
uint64 ignored_out_of_inventory_count
string[] ignored_class_names
uint64 ignored_ambiguous_command_count
TrackedToolBelief[] tools
