"""Fail-closed path identity checks for immutable repository artifacts."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_repo_artifact_identity(
    declared_path: Any,
    *,
    expected_path: Path,
    repo_root: Path,
    expected_sha256: Any,
    label: str,
) -> Path:
    """Resolve one immutable artifact after a checkout-root relocation.

    The declared absolute path remains authoritative when it exists. A missing
    historical path may map to the same full repository-relative path in the
    current checkout, but only after confinement and SHA-256 verification.
    """

    if not isinstance(declared_path, str) or not declared_path.strip():
        raise ValueError(f"{label}: declared path is missing")
    declared = Path(declared_path)
    if not declared.is_absolute():
        raise ValueError(f"{label}: declared path must be absolute")
    if ".." in declared.parts:
        raise ValueError(f"{label}: declared path must not contain '..'")
    if not isinstance(expected_sha256, str) or _SHA256_PATTERN.fullmatch(
        expected_sha256
    ) is None:
        raise ValueError(f"{label}: expected SHA-256 is invalid")

    root = repo_root.resolve()
    expected = expected_path.resolve()
    try:
        relative = expected.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"{label}: expected path is outside the repository"
        ) from exc
    if not relative.parts or not expected.is_file():
        raise ValueError(f"{label}: expected repository artifact is missing")
    actual_sha256 = _sha256_file(expected)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"{label}: SHA-256 mismatch: "
            f"expected={expected_sha256}, actual={actual_sha256}"
        )

    if declared == expected:
        return expected
    if os.path.lexists(declared):
        raise ValueError(
            f"{label}: declared artifact exists at a conflicting path"
        )
    if len(declared.parts) <= len(relative.parts) or tuple(
        declared.parts[-len(relative.parts) :]
    ) != relative.parts:
        raise ValueError(
            f"{label}: declared path does not preserve the repository-relative "
            "artifact identity"
        )
    return expected
