# Validated, one-shot humanoid speech proposal emitted from a VLM inference.
# Consumers must use reply_id (derived from gateway_instance_id +
# procedure_run_id + utterance_id) as the idempotency key; correlation_id
# identifies the producing inference only.
builtin_interfaces/Time stamp
string source
uint64 source_epoch
uint64 source_sequence
string correlation_id
string schema_version

string gateway_instance_id
string procedure_run_id
string utterance_id
string turn_id
string reply_id

string text
# Examples: acknowledgement, answer, clarification.
string kind
# immediate, on_function_accepted, or on_function_completed.
string timing
bool speak

# Optional correlation to a function call produced for the same turn.
string function_call_name
string function_arguments_json
string function_request_id

# Dialogue envelope/schema validity only. This is not execution admission,
# controller acceptance, or proof of physical completion.
bool valid
string validation_error
