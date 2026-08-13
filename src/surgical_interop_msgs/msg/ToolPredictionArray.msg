# Latest advisory next-instrument predictions. An empty array means that no
# public prediction is currently available; consumers must inspect
# procedure_active and /surgery/health before interpreting why.
builtin_interfaces/Time stamp
uint64 revision
string schema_version
string catalog_version
string gateway_instance_id
string procedure_run_id
string procedure_type
bool procedure_active
surgical_interop_msgs/ToolPrediction[] predictions
