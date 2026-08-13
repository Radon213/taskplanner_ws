# Stable display metadata for one authored procedure phase.
string KIND_NORMAL="normal"
string KIND_INTERRUPT="interrupt"

# One-based authored display order across this catalog.
uint32 ordinal
string phase_id
string display_name
string display_name_ko
string phase_kind

# Authored procedure metadata only. Empty means no transition/tool was declared;
# it does not imply that an unlisted clinical transition is impossible.
string[] possible_next_phase_ids
string[] expected_instrument_ids
