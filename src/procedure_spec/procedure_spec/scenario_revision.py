"""Revision-stable loading for ScenarioStore bundle consumers.

ScenarioStore is the sole owner of the selected bundle.  A retained
``scenario_config`` message nevertheless names a mutable, researcher-authored
directory, so every consumer must verify that the bytes it is about to load
still match the revision published by ScenarioStore.  Keeping the digest and
before/after load check here prevents each owner from drifting into a slightly
different interpretation of the same selection.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

from .loader import load_bundle


_BUNDLE_CONFIG_REVISION_SCHEMA = b"taskplanner.procedure_bundle.revision.v1\0"
_BUNDLE_CONFIG_SUFFIXES = frozenset({".yaml", ".yml"})
_BUNDLE_REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def compute_bundle_config_revision(bundle_dir: str | Path) -> str:
    """Return the deterministic ScenarioStore digest for one bundle.

    The digest deliberately includes every bundle-local YAML file and the
    shared display catalog which ``load_bundle`` reads.  It ignores mtimes and
    absolute paths, so independent owners loading the same authored revision
    produce the same value.
    """

    bundle_path = Path(bundle_dir)
    if not bundle_path.is_dir():
        raise FileNotFoundError(
            f"procedure bundle directory does not exist: {bundle_path}"
        )

    inputs: dict[str, Path] = {}
    shared_catalog = bundle_path.parent / "display_catalog.yaml"
    if shared_catalog.is_file():
        inputs["../display_catalog.yaml"] = shared_catalog
    for path in bundle_path.rglob("*"):
        if path.is_file() and path.suffix.casefold() in _BUNDLE_CONFIG_SUFFIXES:
            inputs[path.relative_to(bundle_path).as_posix()] = path

    digest = hashlib.sha256(_BUNDLE_CONFIG_REVISION_SCHEMA)
    for logical_path, path in sorted(inputs.items()):
        logical_bytes = logical_path.encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(logical_bytes).to_bytes(8, "big"))
        digest.update(logical_bytes)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return f"sha256:{digest.hexdigest()}"


def load_bundle_at_revision(
    bundle_dir: str | Path,
    revision: object,
    *,
    attempts: int = 3,
) -> Any:
    """Load a bundle only when it remains at the published revision.

    An editor save can briefly expose an incomplete or newer YAML tree.  The
    pre/post digest comparison rejects a stale retained message and retries a
    concurrent write without ever returning a mixed bundle.  It is validation
    only: callers decide when their local state is quiescent enough to adopt
    the returned spec.
    """

    expected_revision = str(revision or "").strip()
    if not _BUNDLE_REVISION_PATTERN.fullmatch(expected_revision):
        raise ValueError("scenario config revision must be one sha256 digest")

    bundle_path = Path(bundle_dir)
    for _ in range(max(1, int(attempts))):
        before_revision = compute_bundle_config_revision(bundle_path)
        if before_revision != expected_revision:
            raise ValueError(
                "scenario config revision does not match the authored bundle"
            )
        spec = load_bundle(bundle_path)
        after_revision = compute_bundle_config_revision(bundle_path)
        if after_revision == expected_revision:
            return spec
    raise RuntimeError(
        "scenario config bundle changed while its revision was being loaded"
    )


__all__ = [
    "compute_bundle_config_revision",
    "load_bundle_at_revision",
]
