"""Small, shared helpers for ScenarioStore configuration consumers.

ScenarioStore alone selects and publishes a bundle revision.  Consumers use
these helpers only to verify that the retained notice still names a local,
researcher-authored sibling bundle and to decide whether their *own* local
state is quiescent enough to adopt it.  This module intentionally contains no
ROS, route, admission, preflight, or reset behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from .scenario_config import ScenarioConfigSnapshot, parse_scenario_config
from .scenario_revision import load_bundle_at_revision


QUIESCENT_EXECUTION_STATES = frozenset(
    {
        "idle",
        "stopped",
        "halted",
        "completed",
        "terminated",
        "error",
        "failed",
    }
)


@dataclass(frozen=True, slots=True)
class ScenarioConsumerBundle:
    """A locally parsed bundle that a consumer may adopt atomically."""

    spec_dir: str
    procedure_spec: Any


@dataclass(slots=True)
class ScenarioConfigConsumerBinding:
    """One consumer's small, local view of the retained selection notice.

    It intentionally owns only the validation and pending/committed revision
    bookkeeping common to observers.  Each ROS owner still decides when it is
    locally idle and how a verified bundle changes its own parameters or
    projection.
    """

    fixed_spec_root: Path
    revision: str = ""
    applied_spec_dir: str = ""
    pending: ScenarioConfigSnapshot | None = None
    _pending_bundle: ScenarioConsumerBundle | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @classmethod
    def from_spec_dir(cls, spec_dir: str | Path) -> "ScenarioConfigConsumerBinding":
        """Anchor a binding to the parent of its launch-time bundle."""

        return cls(fixed_spec_root=Path(spec_dir).resolve().parent)

    def stage(self, payload: object) -> bool:
        """Validate a topic payload and retain it when it changes this binding."""

        snapshot = parse_scenario_config(payload)
        bundle = load_scenario_consumer_bundle(
            snapshot,
            fixed_spec_root=self.fixed_spec_root,
        )
        with self._lock:
            if (
                snapshot.revision == self.revision
                and bundle.spec_dir == self.applied_spec_dir
            ):
                return False
            self.pending = snapshot
            self._pending_bundle = bundle
            return True

    def resolve_pending(
        self,
    ) -> tuple[ScenarioConfigSnapshot, ScenarioConsumerBundle] | None:
        """Return the already validated bundle staged for the local swap."""

        with self._lock:
            snapshot = self.pending
            bundle = self._pending_bundle
            if snapshot is None or bundle is None:
                return None
            return snapshot, bundle

    def revalidate_pending(
        self,
    ) -> tuple[ScenarioConfigSnapshot, ScenarioConsumerBundle] | None:
        """Reload the staged selection immediately before a local swap.

        A retained ScenarioStore notice names an authored directory, not an
        immutable copy.  ``stage`` deliberately parses it early so an invalid
        sample cannot displace a valid pending revision, but an author may save
        the directory while this owner waits for its own quiet boundary.  The
        consumer must therefore verify the same published digest again at the
        exact local-commit boundary.  The caller still owns the actual atomic
        parameter/projection change and must call :meth:`commit` afterwards.
        """

        with self._lock:
            snapshot = self.pending
        if snapshot is None:
            return None
        bundle = load_scenario_consumer_bundle(
            snapshot,
            fixed_spec_root=self.fixed_spec_root,
        )
        with self._lock:
            # A newer retained sample won while its YAML was being loaded.
            # Leave it pending; applying the older one would violate owner
            # ordering even if its revision happened to remain valid.
            if self.pending != snapshot:
                return None
        return snapshot, bundle

    def discard(self, snapshot: ScenarioConfigSnapshot) -> None:
        """Discard exactly one invalid staged revision without losing a newer one."""

        with self._lock:
            if self.pending == snapshot:
                self.pending = None
                self._pending_bundle = None

    def commit(
        self,
        snapshot: ScenarioConfigSnapshot,
        bundle: ScenarioConsumerBundle,
    ) -> bool:
        """Commit only if this is still the newest staged revision."""

        with self._lock:
            if self.pending != snapshot:
                return False
            self.revision = snapshot.revision
            self.applied_spec_dir = bundle.spec_dir
            self.pending = None
            self._pending_bundle = None
            return True

    def note_local_spec_dir(self, spec_dir: str | Path) -> None:
        """Invalidate a commit after an independent local ``spec_dir`` change.

        Consumers still support direct researcher-driven parameter edits.  A
        subsequent retained ScenarioStore notice must therefore not be skipped
        merely because it matched a revision applied before that local edit.
        A ScenarioStore-driven parameter swap calls this too; its following
        :meth:`commit` restores the authoritative revision atomically.
        """

        with self._lock:
            self.revision = ""
            self.applied_spec_dir = str(Path(spec_dir).resolve())

    def pending_snapshot(self) -> ScenarioConfigSnapshot | None:
        """Return the newest staged revision for owner diagnostics."""

        with self._lock:
            return self.pending

    def status(self) -> tuple[str, str, ScenarioConfigSnapshot | None]:
        """Return committed revision, applied path, and staged snapshot together."""

        with self._lock:
            return self.revision, self.applied_spec_dir, self.pending


def load_scenario_consumer_bundle(
    snapshot: ScenarioConfigSnapshot,
    *,
    fixed_spec_root: str | Path,
) -> ScenarioConsumerBundle:
    """Validate and parse one ScenarioStore notice beneath a fixed root.

    The topic never grants filesystem authority: a consumer accepts only a
    direct child of the root captured at process startup, matching the bundle
    identity supplied by ScenarioStore and the authored procedure identity.
    The parsed object is returned so the caller can make its local swap without
    parsing different YAML for a second interpretation.
    """

    root = Path(fixed_spec_root).resolve()
    revision = str(snapshot.revision or "").strip()
    if not revision:
        raise ValueError("scenario config revision is invalid")
    candidate = Path(snapshot.spec_dir).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("scenario config spec_dir is outside the fixed root") from exc
    if candidate.parent != root:
        raise ValueError(
            "scenario config spec_dir must name one bundle directly beneath "
            "the fixed root"
        )
    if candidate.name != snapshot.bundle_name:
        raise ValueError("scenario config bundle_name does not match spec_dir")
    spec = load_bundle_at_revision(candidate, revision)
    if str(spec.procedure_id).strip() != snapshot.bundle_name:
        raise ValueError(
            "scenario config bundle_name does not match the authored procedure"
        )
    return ScenarioConsumerBundle(spec_dir=str(candidate), procedure_spec=spec)


def scenario_config_apply_is_safe(
    *,
    state_received: bool,
    scenario_running: bool,
    execution_state: object,
    initial_idle: bool,
    local_busy: bool = False,
) -> bool:
    """Return whether one observer may replace its local scenario projection.

    ``local_busy`` is deliberately supplied by the owner rather than inferred
    here: an actor's active cue and an adapter's in-flight endpoint request are
    different resources.  A paused authoritative scenario is usable only when
    that owner is not busy; stopped/terminal states also require ``running`` to
    be false.  A focused owner restart can rehydrate the retained snapshot
    before it receives a state frame when it begins initially idle.
    """

    if local_busy:
        return False
    if not state_received:
        return bool(initial_idle)
    state = str(execution_state or "").strip().casefold()
    if state == "paused":
        return True
    return not bool(scenario_running) and state in QUIESCENT_EXECUTION_STATES


__all__ = [
    "QUIESCENT_EXECUTION_STATES",
    "ScenarioConfigConsumerBinding",
    "ScenarioConsumerBundle",
    "load_scenario_consumer_bundle",
    "scenario_config_apply_is_safe",
]
