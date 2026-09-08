"""Small capability model for the Debug runtime.

The Debug workspace deliberately has several optional integrations (USB ASR,
external record upload, and DDS network mutation).  They
must not turn a read-only observer into an all-or-nothing process.  This module
keeps that lifecycle decision data-only so launch files can start a tiny
observer today and add a focused sidecar later without changing the public
status schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


CAPABILITY_NAMES = frozenset(
    {
        "observer",
        "control",
        "asr",
        "record",
        "network",
    }
)

_PROFILES: dict[str, frozenset[str]] = {
    "full": frozenset(CAPABILITY_NAMES),
    "observer": frozenset({"observer"}),
    "control": frozenset({"observer", "control"}),
    "asr": frozenset({"observer", "asr"}),
    "record": frozenset({"observer", "record"}),
    "network": frozenset({"observer", "network"}),
}

_DESCRIPTIONS = {
    "observer": "ROS topic/status observation only",
    "control": "typed Topic/Service/Action dispatch",
    "asr": "USB microphone and reviewed ASR route",
    "record": "surgery-record input/API client",
    "network": "DDS network inspection/settings",
}


@dataclass(frozen=True, slots=True)
class DebugCapabilities:
    """The enabled runtime capabilities for one Debug process.

    ``observer`` is always present.  It is intentionally not a dependency of
    the other capabilities: a deployment can run a status-only observer with
    no PipeWire socket, no API-key mount, and no PNU worker.
    """

    enabled_names: frozenset[str]

    @classmethod
    def parse(cls, raw: object) -> "DebugCapabilities":
        if raw is None:
            raw = "full"
        if isinstance(raw, str):
            values = [item.strip().casefold() for item in raw.split(",")]
        elif isinstance(raw, Iterable):
            values = [str(item).strip().casefold() for item in raw]
        else:
            raise ValueError("debug capabilities must be a CSV string or string list")
        values = [item for item in values if item]
        if not values:
            raise ValueError("debug capabilities must not be empty")

        names: set[str] = set()
        for value in values:
            if value in _PROFILES:
                names.update(_PROFILES[value])
            elif value in CAPABILITY_NAMES:
                names.add(value)
            else:
                supported = ", ".join(sorted((*_PROFILES, *CAPABILITY_NAMES)))
                raise ValueError(
                    f"unsupported debug capability/profile {value!r}; supported: {supported}"
                )
        names.add("observer")
        return cls(frozenset(names))

    def enabled(self, name: str) -> bool:
        return str(name).strip().casefold() in self.enabled_names

    @property
    def observer_only(self) -> bool:
        return self.enabled_names == frozenset({"observer"})

    def status_rows(self) -> list[dict[str, object]]:
        """Return bounded, browser-readable capability status rows."""

        return [
            {
                "name": name,
                "enabled": name in self.enabled_names,
                "state": "enabled" if name in self.enabled_names else "not_started",
                "description": _DESCRIPTIONS[name],
                "restart_scope": "debug-observer" if name == "observer" else f"debug-{name}",
            }
            for name in sorted(CAPABILITY_NAMES)
        ]


def capability_for_operation(operation: str) -> str:
    """Return the focused owner capability for one Debug command."""

    normalized = str(operation).strip().casefold()
    if normalized.startswith("asr_"):
        return "asr"
    if normalized.startswith("record_"):
        return "record"
    if normalized in {"apply_network_settings", "ping_host"}:
        return "network"
    # Arm/disarm, endpoint selection, manual topic output, and generic typed
    # dispatch all belong to the one explicit write capability.
    return "control"


__all__ = [
    "CAPABILITY_NAMES",
    "DebugCapabilities",
    "capability_for_operation",
]
