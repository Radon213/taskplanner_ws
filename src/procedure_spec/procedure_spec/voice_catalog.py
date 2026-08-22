"""Canonical active-procedure binding for spoken tool requests.

Spoken aliases are procedure-local: a token such as ``보비`` must never be
globally mapped to a fixed ``Txx`` ID.  This module is intentionally owned by
``procedure_spec`` so the voice producer, Digital Twin, and BT policy can
derive the same binding without depending on each other's implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping, Sequence
import unicodedata

from .loader import load_bundle


@dataclass(frozen=True)
class VoiceCommandCatalog:
    """Unambiguous aliases for requestable instruments in one bundle."""

    procedure_id: str
    catalog_id: str
    tool_aliases: dict[str, tuple[str, ...]]
    ambiguous_aliases: dict[str, tuple[str, ...]]
    bundle_path: str


def normalize_voice_alias(value: object) -> str:
    """Canonical matching form; does not replace the original transcript."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = re.sub(r"[^0-9a-z가-힣]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def voice_catalog_id_for(
    procedure_id: str,
    tool_aliases: Mapping[str, Sequence[str]],
) -> str:
    """Return a stable, full SHA-256 procedure/alias binding ID.

    The exact preimage is UTF-8 JSON generated with ``ensure_ascii=False``,
    ``sort_keys=True``, and compact separators for a mapping containing the
    procedure ID and each sorted tool ID with sorted normalized aliases.
    """

    payload = {
        "procedure_id": str(procedure_id),
        "tools": [
            {
                "tool_id": str(tool_id),
                "aliases": sorted(
                    normalize_voice_alias(alias)
                    for alias in aliases
                    if normalize_voice_alias(alias)
                ),
            }
            for tool_id, aliases in sorted(tool_aliases.items())
        ],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def load_voice_command_catalog(bundle_dir: str | Path) -> VoiceCommandCatalog:
    """Load the active bundle and remove aliases ambiguous across tools.

    The function never guesses a winner for an ambiguous alias.  The remaining
    unambiguous aliases are what gets fingerprinted and emitted in a voice
    proposal; the dropped aliases remain visible in ``ambiguous_aliases`` for
    observability and test coverage.
    """

    bundle_path = Path(bundle_dir).expanduser().resolve()
    spec = load_bundle(bundle_path)
    aliases_by_tool: dict[str, list[str]] = {}
    alias_owners: dict[str, set[str]] = {}
    for instrument in spec.bundle.instruments:
        if not instrument.requestable:
            continue
        tool_id = str(instrument.id).strip()
        aliases = [
            tool_id,
            str(instrument.display_name),
            str(instrument.display_name_ko),
            *(str(alias) for alias in instrument.aliases),
        ]
        normalized_aliases = [normalize_voice_alias(alias) for alias in aliases]
        aliases_by_tool[tool_id] = list(
            dict.fromkeys(alias for alias in normalized_aliases if alias)
        )
        for alias in aliases_by_tool[tool_id]:
            alias_owners.setdefault(alias, set()).add(tool_id)

    ambiguous = {
        alias: tuple(sorted(owners))
        for alias, owners in alias_owners.items()
        if len(owners) > 1
    }
    safe_aliases = {
        tool_id: tuple(alias for alias in aliases if alias not in ambiguous)
        for tool_id, aliases in aliases_by_tool.items()
    }
    safe_aliases = {
        tool_id: aliases for tool_id, aliases in safe_aliases.items() if aliases
    }
    if not safe_aliases:
        raise ValueError(
            f"procedure bundle {bundle_path} has no unambiguous requestable tool aliases"
        )
    procedure_id = str(spec.bundle.procedure_id).strip()
    if not procedure_id:
        raise ValueError(f"procedure bundle {bundle_path} has an empty procedure_id")
    return VoiceCommandCatalog(
        procedure_id=procedure_id,
        catalog_id=voice_catalog_id_for(procedure_id, safe_aliases),
        tool_aliases=safe_aliases,
        ambiguous_aliases=ambiguous,
        bundle_path=str(bundle_path),
    )
