# One public, ordered surgical-state event. detail text and internal reasoning are
# intentionally not carried by this interface.
builtin_interfaces/Time stamp
uint64 sequence

# Immutable publication/run identity captured atomically when the gateway
# accepts this event. Consumers must group events by
# (gateway_instance_id, procedure_run_id), never by sequence alone.
string schema_version
string catalog_version
string gateway_instance_id
string procedure_run_id
string procedure_type

string event_type
string subject_type
string subject_id
string phase
string location_type
string location_id
string state
string correlation_id
float32 confidence
string evidence_status
