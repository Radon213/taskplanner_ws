# Run-scoped receipt for one VLM dialogue function proposal.
# This reports local gate persistence/admission only. It never proves that an
# Action/Service was accepted or that physical motion occurred.
builtin_interfaces/Time stamp

string gateway_instance_id
string procedure_run_id
string utterance_id
string reply_id
string function_request_id
string function_call_name

# admitted, reply_acked, intent_acked, rejected, stale, or completed.
string state
bool terminal
bool success
string reason_code
