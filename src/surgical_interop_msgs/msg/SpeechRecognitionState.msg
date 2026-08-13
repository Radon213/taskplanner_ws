# Latest public, finalized speech-recognition state. This is a snapshot for UI
# integration, not an audio stream and not proof that the planner accepted a
# spoken request.
string STATE_UNAVAILABLE="unavailable"
string STATE_IDLE="idle"
string STATE_LISTENING="listening"
string STATE_PROCESSING="processing"
string STATE_READY="ready"
string STATE_ERROR="error"

builtin_interfaces/Time stamp
uint64 revision
string schema_version
string catalog_version
string gateway_instance_id
string procedure_run_id
string procedure_type
bool procedure_active

# available means a usable ASR result source is fresh; connected describes the
# upstream ASR transport/runtime. Consumers must not infer either from text.
bool available
bool connected
string state

# Procedure-run-local finalized-utterance sequence. It resets when
# procedure_run_id or gateway_instance_id changes. utterance_stamp is the
# gateway receipt time for this plain-string compatibility input.
uint64 utterance_sequence
builtin_interfaces/Time utterance_stamp
# Empty by default even for a finalized utterance. Deployments must explicitly
# opt in before publishing upstream free text; the gateway does not de-identify
# opted-in text. A *_REDACTED evidence_status distinguishes policy suppression.
string text

# Latency is valid only when latency_available=true. latency_basis is a stable
# description such as api_round_trip; no timing basis is implied when empty.
bool latency_available
float32 response_latency_ms
string latency_basis

string source
string evidence_status
