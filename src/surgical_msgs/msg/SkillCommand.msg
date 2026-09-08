builtin_interfaces/Time stamp
string command_id
string action
string instrument_id
string instrument_instance_id
uint64 request_generation
# Immutable execution interval for every command. Consumers must reject a
# command whose run is not the currently active procedure run.
string procedure_run_id
# Direct-hand replay fence. This field stays zero for every non-direct command
# and identifies one hand-signal episode within the run above across BT and
# execution-bridge process restarts.
uint64 implicit_request_generation
# True only for a validated typed voice request. This provenance permits the
# execution bridge to preempt a lower-priority humanoid tool-transfer Action;
# implicit/prediction commands must leave it false.
bool voice_backed
string target_location_id
string target_location_type
string rationale
string arm
string source_location_id
string source_location_type
string target_owner
bool cleaning_required
string mode
