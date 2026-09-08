"""Small, owner-local helpers for ScenarioStore voice configuration updates.

The ScenarioStore owns the selected bundle and emits its latest revision on a
latched topic.  Voice consumers only use that notice to refresh their own
local catalog/binding; it is never an ASR, VLM, or execution admission gate.
"""

from __future__ import annotations

from pathlib import Path

from procedure_spec import ScenarioConfigSnapshot, load_bundle_at_revision


_STOPPED_EXECUTION_STATES = frozenset(
    {"idle", "halted", "completed", "terminated", "stopped"}
)


def scenario_config_apply_is_safe(
    *,
    running: object,
    execution_state: object,
) -> bool:
    """Return whether a local voice binding may be replaced now.

    A paused procedure remains logically ``running`` in SimulationState, but
    it is an explicit operator quiescence point.  Every other accepted state
    must be both terminal/inactive and report ``running == false``.  Unknown
    or transitioning states intentionally retain the previous local binding.
    """

    state = str(execution_state or "").strip().casefold()
    if state == "paused":
        return True
    return not bool(running) and state in _STOPPED_EXECUTION_STATES


def scenario_config_reload_is_authorized(
    *,
    state_received: object,
    running: object,
    execution_state: object,
) -> bool:
    """Require an observed authoritative state before a local swap.

    This is intentionally scoped to configuration replacement only.  It does
    not participate in ASR admission, VLM availability, or endpoint dispatch.
    """

    return bool(state_received) and scenario_config_apply_is_safe(
        running=running,
        execution_state=execution_state,
    )


def scenario_config_bundle_path(snapshot: ScenarioConfigSnapshot) -> str:
    """Validate the self-describing bundle path before an owner loads it.

    The ProcedureSpec loader remains the authority for the file contents.  A
    mismatched path/name notice is discarded before it can replace an active
    local catalog, leaving the last known-good binding intact.
    """

    candidate = Path(snapshot.spec_dir).resolve()
    if candidate.name != snapshot.bundle_name:
        raise ValueError("scenario config bundle_name does not match spec_dir")
    # ScenarioStore publishes a digest for an editable authored bundle.  Do
    # not let a stale retained snapshot refresh voice aliases or the default
    # start phase from newer bytes.
    load_bundle_at_revision(candidate, snapshot.revision)
    return str(candidate)


__all__ = [
    "scenario_config_apply_is_safe",
    "scenario_config_bundle_path",
    "scenario_config_reload_is_authorized",
]
