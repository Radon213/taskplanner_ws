# Proposal emitted by the voice-intent resolver.  It is intentionally not a
# command and does not imply that a downstream policy accepted or executed it.
std_msgs/Header header
string utterance_id
string raw_text
string normalized_text
string procedure_id
string catalog_id
string intent
string tool_id
string retractor_command
string target_side
float64 distance_m
string urgency
string provenance
bool requires_confirmation
string disposition
string reason
string[] evidence_spans
