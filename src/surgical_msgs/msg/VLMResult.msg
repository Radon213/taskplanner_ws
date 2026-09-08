builtin_interfaces/Time stamp
# Immutable procedure interval that was active when this inference request was
# accepted. Presentation consumers must reject a result from another run.
string procedure_run_id
string source
uint64 source_epoch
uint64 source_sequence
string correlation_id
string schema_version
string raw_json
string summary
string[] phase_ids
float32[] phase_confidences
string[] observed_tool_ids
string[] observed_location_ids
string[] observed_location_types
float32[] observed_confidences
float32 uncertainty
