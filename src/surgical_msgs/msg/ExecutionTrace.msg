# Producer-only execution-observability event.  This message never requests
# motion or changes admission state.  It deliberately excludes VLM proposals,
# planner rationale, and free text.  The bounded retraction fields below are a
# read-only copy of an already-dispatched Service request so an operator can
# audit what was sent without parsing reason strings.
builtin_interfaces/Time stamp
uint64 sequence
string command_id
# Immutable execution interval that owns this trace row. This is required for
# observers because an Action/Service callback can arrive after a new run starts.
string procedure_run_id
string route
string transport
string endpoint
# Selected endpoint family at dispatch time. This lets observers distinguish
# an isolated virtual Action/Service from the reviewed external route without
# inferring it from endpoint text.
string endpoint_source
string stage
# True only after the local Action/Service client call returned.  It proves
# neither remote receipt nor physical completion.
bool dispatch_submitted
bool terminal
# For a Service response, `service_admission_only` is explicitly not physical
# execution evidence.
string evidence
string reason_code
# Retraction Service payload copied at dispatch time.  All three fields remain
# zero for non-retraction traces.  For a retraction trace, command follows
# ExecuteRetractionCommand.COMMAND_*, target side follows TARGET_*, and distance
# is the exact controller-facing value in metres.
uint8 retraction_command
uint8 retraction_target_side
float64 retraction_distance_m
