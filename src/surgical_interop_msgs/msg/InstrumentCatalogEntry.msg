# Stable display metadata for one procedure-scoped instrument type.
string instrument_id
string display_name
string display_name_ko
string[] aliases
string category

# Configured procedure inventory, not a live availability count. Live instance
# state and availability come from /surgery/instruments.
uint32 inventory_count
bool requestable
string role
