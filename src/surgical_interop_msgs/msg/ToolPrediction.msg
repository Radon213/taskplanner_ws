# One ranked, advisory next-instrument prediction. It is model-derived evidence,
# never a robot command or authorization to perform a handover.
builtin_interfaces/Time stamp

# One-based rank within ToolPredictionArray.predictions.
uint32 rank

# Procedure-catalog identifier and, when known, a concrete inventory instance.
string instrument_id
string instance_id

# Confidence is finite and clamped to [0.0, 1.0]. Stability is seconds for which
# this same prediction has remained selected. Zero is valid, not an unknown
# sentinel.
float32 confidence
float32 stability_sec

# Stable producer/category identifier and evidence authority.
string source
string evidence_status
