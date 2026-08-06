# One public, ordered surgical-state event. detail text and internal reasoning are
# intentionally not carried by this interface.
builtin_interfaces/Time stamp
uint64 sequence
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
