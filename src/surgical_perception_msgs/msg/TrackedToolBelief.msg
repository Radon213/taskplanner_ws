# One scenario-bounded tracker slot. In strict mode it denotes an authored
# instance and existence_probability is 1.0. In exchangeable mode it is a
# logical capacity slot, not a stable physical identity; existence_probability
# is the probability that the slot is active/present. Interpret status_flags
# before treating instance_id as an identity.
string track_id
string instrument_id
string instance_id
string display_name
float32 existence_probability

# This producer currently emits confirmed, probable, or uncertain.
string status
string committed_location_id
float32 committed_location_probability
string most_likely_location_id
float32 most_likely_probability
ToolLocationProbability[] locations

# A zero stamp and negative age mean no positive camera evidence has arrived.
builtin_interfaces/Time last_positive_stamp
float32 last_positive_age_sec
string[] evidence_sources

# Advisory provenance only. This message cannot admit or execute robot motion.
string motion_mode
string active_command_id
string model_version
string ontology_version
string calibration_version
string[] status_flags
