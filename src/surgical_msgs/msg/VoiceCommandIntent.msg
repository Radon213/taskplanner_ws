# Proposal emitted by the voice-intent resolver.  It is intentionally not a
# command and does not imply that a downstream policy accepted or executed it.
std_msgs/Header header
string utterance_id
# Exact public-gateway epoch/run scope. Empty on resolver-only proposals and
# populated only after the VLM function admission gate has joined the proposal
# to the active GatewayInfo lease. These fields are correlation evidence, not
# execution authority.
string gateway_instance_id
string procedure_run_id
# Stable VLM function identity when this proposal has passed the dialogue
# function admission gate. Empty on resolver-only proposals. Downstream
# consumers must preserve this value in their admission evidence; it is an
# idempotency/correlation key, not controller authority.
string function_request_id
# Immutable source metadata copied from the admitted SpeechUtterance.  These
# fields distinguish a live final ASR result from Debug/replay String
# compatibility input; they do not constitute execution authority.
string source
bool source_is_final
string source_speaker_role
bool source_has_confidence
float32 source_confidence
string raw_text
string normalized_text
string procedure_id
string catalog_id
string intent
string tool_id
string retractor_command
# Canonical retractor selector: none, left, right, or both.  `both` is valid
# only for an adjustment and means the same distance is applied independently
# to the left and right retractor.
string target_side
float64 distance_m
string urgency
string provenance
bool requires_confirmation
string disposition
string reason
string[] evidence_spans
