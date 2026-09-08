"""Small, reusable ownership primitives for procedure scenario selection.

This module deliberately has no ROS dependency.  It owns the local, atomic
``bundle -> parsed spec -> revision`` swap used by the ScenarioStore process,
while ROS nodes only adapt that state to the existing ``std_msgs/String``
snapshot topic.  Keeping the file and revision work here lets a future owner
restart without importing the simulation lifecycle, ODT, or BT code.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading

from procedure_spec import (
    compute_bundle_config_revision,
    load_bundle,
    scenario_config_payload,
)


_BUNDLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_BUNDLE_REVISION_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
_SELECTION_STATE_SCHEMA = "taskplanner.scenario_selection.v1"
_SELECTION_STATE_MAX_BYTES = 16_384
_SCENARIO_MUTATION_EXECUTION_STATES = frozenset(
    {"idle", "halted", "completed", "stopped", "terminated"}
)


@dataclass(frozen=True, slots=True)
class ScenarioSnapshot:
    """One parsed, researcher-authored scenario revision.

    ``spec`` intentionally remains opaque to the store.  The procedure-spec
    package validates it before this immutable value is committed, so readers
    always receive the last successfully loaded bundle.
    """

    bundle_name: str
    spec_dir: Path
    revision: str
    spec: object


class ScenarioStore:
    """Thread-safe last-known-good source of truth for one scenario revision."""

    def __init__(self, snapshot: ScenarioSnapshot) -> None:
        self._lock = threading.RLock()
        self._snapshot = _validated_snapshot(snapshot)

    def snapshot(self) -> ScenarioSnapshot:
        """Return the current last-known-good snapshot atomically."""

        with self._lock:
            return self._snapshot

    def replace(self, snapshot: ScenarioSnapshot) -> ScenarioSnapshot:
        """Commit a parsed candidate and return the snapshot it replaced.

        Loading and validation happen before this method is called.  Therefore
        a malformed file or in-progress editor save never replaces the current
        published revision.
        """

        candidate = _validated_snapshot(snapshot)
        with self._lock:
            previous = self._snapshot
            self._snapshot = candidate
            return previous


def validate_bundle_name(bundle_name: str) -> str:
    """Return one path-safe bundle identifier or raise ``ValueError``."""

    value = str(bundle_name or "").strip()
    if not _BUNDLE_NAME_PATTERN.fullmatch(value) or ".." in value:
        raise ValueError(
            "bundle name must be a simple identifier using only "
            "letters, digits, '_', '-', or '.'; path traversal is not allowed"
        )
    return value


def load_scenario_snapshot(
    spec_root: str | Path,
    bundle_name: str,
    *,
    attempts: int = 3,
) -> ScenarioSnapshot:
    """Load one revision-stable bundle below ``spec_root``.

    A normal editor save may briefly expose two YAML revisions.  Re-checking
    the hash around ``load_bundle`` is sufficient here: rejected candidates
    leave the caller's existing ScenarioStore snapshot untouched.
    """

    bundle_id = validate_bundle_name(bundle_name)
    root = Path(spec_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"procedure spec root does not exist: {root}")
    bundle_dir = (root / bundle_id).resolve()
    try:
        bundle_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"bundle '{bundle_id}' resolves outside the configured spec root"
        ) from exc
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"bundle '{bundle_id}' not found under {root}")

    for _attempt in range(max(1, int(attempts))):
        before_revision = compute_bundle_config_revision(bundle_dir)
        spec = load_bundle(bundle_dir)
        after_revision = compute_bundle_config_revision(bundle_dir)
        if before_revision != after_revision:
            continue
        procedure_id = str(getattr(spec, "procedure_id", "") or "").strip()
        if procedure_id != bundle_id:
            raise ValueError(
                f"bundle directory '{bundle_id}' declares procedure_id "
                f"'{procedure_id or '<empty>'}'"
            )
        return ScenarioSnapshot(
            bundle_name=bundle_id,
            spec_dir=bundle_dir,
            revision=after_revision,
            spec=spec,
        )
    raise RuntimeError(
        f"bundle '{bundle_id}' changed while it was being loaded; retry preview or reload"
    )


def scenario_config_json(snapshot: ScenarioSnapshot) -> str:
    """Serialize one public, read-only configuration snapshot deterministically."""

    candidate = _validated_snapshot(snapshot)
    payload = scenario_config_payload(
        bundle_name=candidate.bundle_name,
        spec_dir=str(candidate.spec_dir),
        revision=candidate.revision,
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def load_selected_bundle_state(path: str | Path) -> str | None:
    """Return the last selected bundle from a small owner-local state file.

    This is deliberately *not* a Twin/world checkpoint.  It gives an
    independently restarted ScenarioStore the same selected bundle that it
    last published, while resident owners still decide when a new revision may
    affect their locally paused or stopped state.
    """

    state_path = Path(path)
    try:
        raw = state_path.read_bytes()
    except FileNotFoundError:
        return None
    if len(raw) > _SELECTION_STATE_MAX_BYTES:
        raise ValueError("scenario selection state is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("scenario selection state is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _SELECTION_STATE_SCHEMA:
        raise ValueError("scenario selection state has an unsupported schema")
    return validate_bundle_name(str(payload.get("bundle_name", "")))


def persist_selected_bundle_state(
    path: str | Path,
    snapshot: ScenarioSnapshot,
) -> None:
    """Atomically persist one bundle selection for ScenarioStore restart.

    A write failure does not roll back a successful in-memory configuration
    swap: the active ROS snapshot remains authoritative for this process and a
    later owner restart can fall back to its launch default.  The caller may
    surface the diagnostic without turning an ordinary scenario edit into a
    global readiness failure.  Runtime owners stay resident and consume the
    published scenario snapshot themselves, so this state is intentionally
    only a small selected-bundle preference rather than a restart plan.
    """

    candidate = _validated_snapshot(snapshot)
    state_path = Path(path)
    parent = state_path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("scenario selection state parent must be a real directory")
    payload = {
        "schema": _SELECTION_STATE_SCHEMA,
        "bundle_name": candidate.bundle_name,
        "revision": candidate.revision,
    }
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=".selected_bundle.",
        dir=str(parent),
        text=True,
    )
    temporary = Path(temporary_raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, state_path)
    finally:
        temporary.unlink(missing_ok=True)


def scenario_change_is_allowed(
    *,
    running: object,
    execution_state: object,
) -> tuple[bool, str]:
    """Return whether a bundle selection can be applied at an intervention boundary.

    Runtime owners remain alive across selections and converge from the
    retained scenario snapshot.  A paused procedure is therefore a valid,
    explicit intervention boundary; no owner restart or topology fan-out is
    required.  Endpoint availability, perception health, and controller
    admission remain outside ScenarioStore.
    """

    state = str(execution_state or "").strip().casefold()
    if state == "paused":
        return True, "simulation is paused"
    if not bool(running) and state in _SCENARIO_MUTATION_EXECUTION_STATES:
        return True, "simulation is stopped"
    return (
        False,
        "scenario selection requires the simulation to be paused or stopped; "
        f"current state is {state or 'unknown'}",
    )


def materialize_bundle_snapshot(
    bundle_dir: str | Path,
    config_revision: str,
    snapshot_root: str | Path,
) -> Path:
    """Create an immutable legacy snapshot for one exact YAML revision.

    The normal research path uses :func:`load_scenario_snapshot` directly.
    This helper is retained as a pure migration utility only; it no longer
    drives the active runtime's scenario reload fan-out.
    """

    match = _BUNDLE_REVISION_PATTERN.fullmatch(str(config_revision).strip())
    if match is None:
        raise ValueError("bundle config revision must be one sha256 digest")
    source_bundle = Path(bundle_dir).resolve()
    bundle_name = validate_bundle_name(source_bundle.name)
    if compute_bundle_config_revision(source_bundle) != config_revision:
        raise RuntimeError("bundle changed before its snapshot was materialized")

    root = Path(snapshot_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("procedure snapshot root must be a real directory")
    root.chmod(0o700)
    bundle_snapshot_root = root / bundle_name
    bundle_snapshot_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if bundle_snapshot_root.is_symlink() or not bundle_snapshot_root.is_dir():
        raise ValueError("procedure bundle snapshot root must be a real directory")
    bundle_snapshot_root.chmod(0o700)
    revision_root = bundle_snapshot_root / match.group(1)
    destination_bundle = revision_root / "specs" / bundle_name

    def verify_snapshot() -> None:
        expected_directories = (
            revision_root,
            revision_root / "specs",
            destination_bundle,
        )
        if any(path.is_symlink() or not path.is_dir() for path in expected_directories):
            raise RuntimeError(
                "procedure snapshot destination is not a regular directory tree"
            )
        for path in revision_root.rglob("*"):
            if path.is_symlink():
                raise RuntimeError("procedure snapshot destination contains a symlink")
        if compute_bundle_config_revision(destination_bundle) != config_revision:
            raise RuntimeError(
                "existing procedure snapshot does not match its content digest"
            )

    def seal_snapshot() -> None:
        for path in revision_root.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        for path in sorted(
            (entry for entry in revision_root.rglob("*") if entry.is_dir()),
            key=lambda entry: len(entry.parts),
            reverse=True,
        ):
            path.chmod(0o555)
        revision_root.chmod(0o555)

    if destination_bundle.is_dir():
        verify_snapshot()
        seal_snapshot()
        verify_snapshot()
        return destination_bundle

    temporary_root = Path(
        tempfile.mkdtemp(
            prefix=f".{match.group(1)}.",
            dir=str(bundle_snapshot_root),
        )
    )
    temporary_specs = temporary_root / "specs"
    temporary_bundle = temporary_specs / bundle_name
    temporary_bundle.mkdir(parents=True)

    def copy_yaml(source: Path, destination: Path) -> None:
        if source.is_symlink():
            raise ValueError(
                f"procedure snapshots do not follow YAML symlinks: {source}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    try:
        shared_catalog = source_bundle.parent / "display_catalog.yaml"
        if shared_catalog.is_file():
            copy_yaml(shared_catalog, temporary_specs / "display_catalog.yaml")
        for source in sorted(source_bundle.rglob("*")):
            if not source.is_file() or source.suffix.casefold() not in {
                ".yaml",
                ".yml",
            }:
                continue
            try:
                relative = source.relative_to(source_bundle)
            except ValueError as exc:
                raise ValueError(
                    f"procedure YAML resolves outside its bundle: {source}"
                ) from exc
            copy_yaml(source, temporary_bundle / relative)
        if compute_bundle_config_revision(temporary_bundle) != config_revision:
            raise RuntimeError("materialized procedure snapshot digest mismatch")
        try:
            temporary_root.rename(revision_root)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            try:
                verify_snapshot()
            except Exception as verify_exc:
                raise RuntimeError(
                    "procedure snapshot destination appeared with different content"
                ) from verify_exc
        verify_snapshot()
        seal_snapshot()
        verify_snapshot()
        return destination_bundle
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)


def _validated_snapshot(snapshot: ScenarioSnapshot) -> ScenarioSnapshot:
    if not isinstance(snapshot, ScenarioSnapshot):
        raise TypeError("scenario snapshot must be a ScenarioSnapshot")
    bundle_name = validate_bundle_name(snapshot.bundle_name)
    spec_dir = Path(snapshot.spec_dir)
    revision = str(snapshot.revision or "").strip()
    if not revision or len(revision) > 1024 or any(ch in revision for ch in "\r\n\x00"):
        raise ValueError("scenario revision is invalid")
    if snapshot.spec is None:
        raise ValueError("scenario snapshot spec is required")
    return ScenarioSnapshot(
        bundle_name=bundle_name,
        spec_dir=spec_dir,
        revision=revision,
        spec=snapshot.spec,
    )


__all__ = [
    "ScenarioSnapshot",
    "ScenarioStore",
    "compute_bundle_config_revision",
    "load_selected_bundle_state",
    "load_scenario_snapshot",
    "materialize_bundle_snapshot",
    "persist_selected_bundle_state",
    "scenario_change_is_allowed",
    "scenario_config_json",
    "validate_bundle_name",
]
