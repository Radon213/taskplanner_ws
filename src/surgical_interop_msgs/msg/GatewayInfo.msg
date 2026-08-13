# Liveness and identity of the public surgical-state gateway. This heartbeat is
# published even while no procedure is active so consumers can distinguish an
# idle Taskplanner from a stopped or unreachable gateway.
builtin_interfaces/Time stamp

# Publication-cycle counter. It is process-local and resets when
# gateway_instance_id changes; it is not a semantic state version.
uint64 revision

# Version of the public state schema and installed interface package.
string schema_version
string interface_version

# Deterministic digest of the currently published ProcedureCatalog content.
string catalog_version

# Opaque, non-PHI UUID generated once per gateway process.
string gateway_instance_id

# Opaque, non-PHI identifier for the active procedure run. Empty while idle.
string procedure_run_id
string procedure_type
bool procedure_active
