# Procedure-scoped, non-PHI display catalog. Unlike dynamic state snapshots,
# phases and instruments remain populated while the Taskplanner is idle so a UI
# can render controls before a scenario starts.
builtin_interfaces/Time stamp
uint64 revision
string schema_version
string catalog_version
string gateway_instance_id
string procedure_run_id
string procedure_type
bool procedure_active

string procedure_display_name
string procedure_display_name_ko
string default_phase_id
surgical_interop_msgs/PhaseCatalogEntry[] phases
surgical_interop_msgs/InstrumentCatalogEntry[] instruments
