"""Read-only controller contract manifests for the public robot endpoints.

The ROS graph only tells a client that an endpoint with a compatible transport
shape is present.  It cannot prove that the endpoint is the reviewed partner
controller, that its generated Action ABI matches, or that its policy accepts
the active procedure inventory.  This module defines the small, deterministic
manifest that is published by an emulator now and must be published by an
external controller when controller-side compatibility diagnostics are desired.

The manifest is diagnostic telemetry, not an admission or authentication
mechanism.  Endpoint discovery and integration-readiness checks govern runtime
dispatch; network-level authentication remains a deployment responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from typing import Any, Iterable, Mapping


CONTROLLER_CONTRACT_SCHEMA = "taskplanner.robot_controller_contract.v1"
TOOL_HANDOVER_ACTION_TYPE = "surgical_interop_msgs/action/ExecuteToolHandover"
RETRACTION_SERVICE_TYPE = "surgical_interop_msgs/srv/ExecuteRetractionCommand"

# These identifiers deliberately distinguish a non-physical emulator from the
# reviewed physical partner.  A virtual endpoint can demonstrate the same
# capability policy but must never satisfy an external-real admission check.
EIR_NUC_CAPABILITY_POLICY_ID = "eir-nuc-tool-handover.v1"
GENERIC_EMULATOR_CAPABILITY_POLICY_ID = "taskplanner-generic-emulator.v1"
VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID = "taskplanner-virtual-full-inventory.v1"
EIR_NUC_EXTERNAL_CONTRACT_ID = "eir-nuc-tool-handover.real.v1"
EIR_NUC_VIRTUAL_CONTRACT_ID = "taskplanner-virtual-eir-nuc.v1"

REVIEWED_TOOL_TRANSITIONS = frozenset(
    {
        ("tray", "robot"),
        ("tray", "surgeon"),
        ("robot", "surgeon"),
        ("robot", "tray"),
        ("robot", "mayo"),
        ("mayo", "robot"),
        ("mayo", "tray"),
    }
)

# Keep this field-level fingerprint independent from comments and generated
# language bindings.  Both peers can compute it before any Action Goal is sent.
TOOL_HANDOVER_ACTION_ABI = {
    "goal": (
        "string command_id",
        "string instrument_id",
        "string instrument_instance_id",
        "string source_location",
        "string target_location",
    ),
    "result": (
        "bool success",
        "string final_state",
        "string reason_code",
        "string failure_detail",
    ),
    "feedback": (
        "string state",
        "float32 progress",
    ),
}

RETRACTION_SERVICE_V1_ABI = {
    "request": (
        "uint16 protocol_version",
        "string source_id",
        "string command_id",
        "uint8 command",
        "uint8 target_side",
        "float64 distance_m",
    ),
    "response": (
        "bool request_accepted",
        "uint16 result_code",
        "string command_id",
        "string message",
    ),
}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


TOOL_HANDOVER_ACTION_ABI_FINGERPRINT = _fingerprint(TOOL_HANDOVER_ACTION_ABI)
RETRACTION_SERVICE_V1_ABI_FINGERPRINT = _fingerprint(RETRACTION_SERVICE_V1_ABI)


def _normal_text(value: object) -> str:
    return str(value or "").strip()


def validate_source_stamp(
    payload: object,
    *,
    now_sec: float,
    max_age_sec: float,
    future_tolerance_sec: float,
    source_name: str,
    previous_stamp_sec: float | None = None,
) -> tuple[float | None, str]:
    """Validate a wall-clock source stamp and reject replayed lease payloads.

    A recently *received* contract can still be a replay.  Admission callers
    must require a finite, fresh source stamp and a strictly increasing value
    whenever they replace their current lease.
    """

    prefix = _normal_text(source_name).replace(" ", "_") or "source"
    if not isinstance(payload, Mapping):
        return None, f"{prefix}_missing"
    raw_stamp = payload.get("stamp_sec")
    if isinstance(raw_stamp, bool):
        return None, f"{prefix}_source_stamp_invalid"
    try:
        stamp_sec = float(raw_stamp)
        current_sec = float(now_sec)
        allowed_age_sec = float(max_age_sec)
        allowed_future_sec = float(future_tolerance_sec)
    except (TypeError, ValueError):
        return None, f"{prefix}_source_stamp_missing"
    if (
        not math.isfinite(stamp_sec)
        or stamp_sec <= 0.0
        or not math.isfinite(current_sec)
        or not math.isfinite(allowed_age_sec)
        or not math.isfinite(allowed_future_sec)
        or allowed_age_sec < 0.0
        or allowed_future_sec < 0.0
    ):
        return None, f"{prefix}_source_stamp_invalid"
    # Some ROS JSON contracts retain a whole-second ``stamp_sec`` plus a
    # nanosecond component.  Fold it in before monotonic comparison so two
    # healthy publications in the same wall-clock second are not mistaken for
    # a replay, while malformed nanoseconds still fail closed.
    if "stamp_nanosec" in payload:
        raw_nanosec = payload.get("stamp_nanosec")
        if isinstance(raw_nanosec, bool):
            return None, f"{prefix}_source_stamp_invalid"
        try:
            nanosec = int(raw_nanosec)
        except (TypeError, ValueError):
            return None, f"{prefix}_source_stamp_invalid"
        if nanosec < 0 or nanosec >= 1_000_000_000:
            return None, f"{prefix}_source_stamp_invalid"
        stamp_sec += nanosec / 1_000_000_000.0
    if stamp_sec > current_sec + allowed_future_sec:
        return stamp_sec, f"{prefix}_source_stamp_future"
    if current_sec - stamp_sec > allowed_age_sec:
        return stamp_sec, f"{prefix}_source_stamp_stale"
    if previous_stamp_sec is not None:
        try:
            previous = float(previous_stamp_sec)
        except (TypeError, ValueError):
            previous = 0.0
        if math.isfinite(previous) and previous > 0.0 and stamp_sec <= previous:
            return stamp_sec, f"{prefix}_source_stamp_not_monotonic"
    return stamp_sec, ""


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    """One reviewed handover policy.

    ``allowed_instrument_instances`` is ``None`` only for the generic,
    non-partner emulator policy.  It must never be selected for an external
    controller admission check.
    """

    policy_id: str
    allowed_instrument_instances: Mapping[str, frozenset[str]] | None
    allowed_transitions: frozenset[tuple[str, str]]

    def supports_tool(self, instrument_id: str, instrument_instance_id: str) -> bool:
        instrument = _normal_text(instrument_id)
        instance = _normal_text(instrument_instance_id)
        if not instrument or not instance:
            return False
        if self.allowed_instrument_instances is None:
            return True
        allowed = self.allowed_instrument_instances.get(instrument)
        return bool(allowed and instance in allowed)

    def manifest(self) -> dict[str, object]:
        instruments: list[dict[str, object]]
        if self.allowed_instrument_instances is None:
            instruments = []
            identity_mode = "any_nonempty_public_identity"
        else:
            instruments = [
                {
                    "instrument_id": instrument_id,
                    "instances": sorted(instances),
                }
                for instrument_id, instances in sorted(
                    self.allowed_instrument_instances.items()
                )
            ]
            identity_mode = "exact_allowlist"
        return {
            "policy_id": self.policy_id,
            "identity_mode": identity_mode,
            "allowed_instruments": instruments,
            "allowed_transitions": [
                f"{source}->{target}"
                for source, target in sorted(self.allowed_transitions)
            ],
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.manifest())


_EIR_NUC_POLICY = CapabilityPolicy(
    policy_id=EIR_NUC_CAPABILITY_POLICY_ID,
    allowed_instrument_instances={
        "Adson forceps": frozenset(
            {"Adson forceps", "Adson forceps#1", "Adson forceps#2"}
        ),
        "Bovie surgical cautery": frozenset(
            {"Bovie surgical cautery", "Bovie surgical cautery#1"}
        ),
        "Bipolar cautery": frozenset(
            {"Bipolar cautery", "Bipolar cautery#1"}
        ),
        "Mosquito forceps": frozenset(
            {"Mosquito forceps", "Mosquito forceps#1"}
        ),
    },
    allowed_transitions=REVIEWED_TOOL_TRANSITIONS,
)

_GENERIC_EMULATOR_POLICY = CapabilityPolicy(
    policy_id=GENERIC_EMULATOR_CAPABILITY_POLICY_ID,
    allowed_instrument_instances=None,
    allowed_transitions=REVIEWED_TOOL_TRANSITIONS,
)

# This policy is intentionally isolated to the ``/integration/virtual``
# endpoint namespace. It can exercise every nonempty public identity in a
# procedure bundle, whereas the physical EIR-NUC policy remains an explicit
# reviewed allowlist and is never widened by a virtual-run convenience.
_VIRTUAL_EMULATOR_POLICY = CapabilityPolicy(
    policy_id=VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID,
    allowed_instrument_instances=None,
    allowed_transitions=REVIEWED_TOOL_TRANSITIONS,
)

_POLICIES = {
    _EIR_NUC_POLICY.policy_id: _EIR_NUC_POLICY,
    _GENERIC_EMULATOR_POLICY.policy_id: _GENERIC_EMULATOR_POLICY,
    _VIRTUAL_EMULATOR_POLICY.policy_id: _VIRTUAL_EMULATOR_POLICY,
}


def get_capability_policy(policy_id: object) -> CapabilityPolicy | None:
    """Return the reviewed policy or ``None`` for an unknown policy ID."""

    return _POLICIES.get(_normal_text(policy_id))


def validate_tool_handover_fields(
    *,
    instrument_id: object,
    instrument_instance_id: object,
    source_location: object,
    target_location: object,
    capability_policy_id: object,
) -> str:
    """Return a stable rejection reason, or ``""`` when the Goal is allowed."""

    source = _normal_text(source_location).casefold()
    target = _normal_text(target_location).casefold()
    if (source, target) not in REVIEWED_TOOL_TRANSITIONS:
        return "invalid_tool_transition"
    instrument = _normal_text(instrument_id)
    instance = _normal_text(instrument_instance_id)
    if not instrument or not instance:
        return "missing_instrument_identity"
    policy = get_capability_policy(capability_policy_id)
    if policy is None:
        return "unknown_capability_policy"
    if not policy.supports_tool(instrument, instance):
        return "instrument_not_supported_by_capability_policy"
    return ""


def required_tool_instances_for_spec(spec: object) -> tuple[tuple[str, str], ...]:
    """Return every public, requestable inventory instance in a procedure spec.

    The public bridge formats a catalog instance as ``<display name>#<index>``.
    An external controller therefore must explicitly support each requestable
    instance before preflight admits the procedure.  A bundle can narrow this
    set only by marking an instrument non-requestable in the procedure spec.
    """

    bundle = getattr(spec, "bundle", None)
    instruments = getattr(bundle, "instruments", ())
    required: list[tuple[str, str]] = []
    for instrument in instruments:
        if not bool(getattr(instrument, "requestable", True)):
            continue
        display_name = _normal_text(getattr(instrument, "display_name", ""))
        try:
            count = int(getattr(instrument, "inventory_count", 0))
        except (TypeError, ValueError):
            count = 0
        if not display_name or count <= 0:
            continue
        required.extend(
            (display_name, f"{display_name}#{index}")
            for index in range(1, count + 1)
        )
    return tuple(required)


def build_controller_contract(
    *,
    contract_id: object,
    endpoint_source: object,
    execution_mode: object,
    tool_handover_endpoint: object,
    retraction_service_name: object,
    capability_policy_id: object,
    supports_retraction_profile_identity: bool = False,
    stamp_sec: float | None = None,
) -> dict[str, object]:
    """Build one exact, read-only controller manifest payload.

    The V1 retraction Service cannot carry an end-effector profile or arm ID,
    so callers must not claim profile-identity support while advertising its
    V1 ABI fingerprint.
    """

    policy = get_capability_policy(capability_policy_id)
    if policy is None:
        raise ValueError("controller contract uses an unknown capability policy")
    if supports_retraction_profile_identity:
        raise ValueError(
            "ExecuteRetractionCommand V1 cannot advertise profile identity support"
        )
    payload: dict[str, object] = {
        "schema": CONTROLLER_CONTRACT_SCHEMA,
        "contract_id": _normal_text(contract_id),
        "endpoint_source": _normal_text(endpoint_source).casefold(),
        "execution_mode": _normal_text(execution_mode).casefold(),
        "tool_handover": {
            "endpoint": _normal_text(tool_handover_endpoint),
            "type": TOOL_HANDOVER_ACTION_TYPE,
            "abi_fingerprint": TOOL_HANDOVER_ACTION_ABI_FINGERPRINT,
            "capability_policy_id": policy.policy_id,
            "capability_fingerprint": policy.fingerprint,
        },
        "retraction": {
            "endpoint": _normal_text(retraction_service_name),
            "type": RETRACTION_SERVICE_TYPE,
            "abi_fingerprint": RETRACTION_SERVICE_V1_ABI_FINGERPRINT,
            "protocol_version": 1,
            "supports_profile_identity": False,
            "supports_change_end_effector": False,
            # V1 only acknowledges Service receipt; it has no physical-stop
            # state or completion field. Never let an emulator imply one.
            "physical_stop_confirmation": "unknown",
        },
    }
    if stamp_sec is not None:
        payload["stamp_sec"] = float(stamp_sec)
    return payload


def controller_contract_mismatches(
    payload: object,
    *,
    expected_contract_id: object,
    expected_endpoint_source: object,
    expected_execution_mode: object,
    expected_tool_handover_endpoint: object,
    expected_retraction_service_name: object,
    expected_capability_policy_id: object,
    require_tool_handover: bool,
    require_retraction_service: bool,
    require_retraction_profile_identity: bool,
    require_physical_stop_confirmation: bool = False,
    required_tool_instances: Iterable[tuple[str, str]] = (),
) -> tuple[str, ...]:
    """Return deterministic mismatch codes for one observed contract payload."""

    if not isinstance(payload, Mapping):
        return ("controller_contract_missing",)
    mismatches: list[str] = []
    if payload.get("schema") != CONTROLLER_CONTRACT_SCHEMA:
        mismatches.append("controller_contract_schema_mismatch")
    if not _normal_text(expected_contract_id):
        mismatches.append("controller_contract_id_unconfigured")
    elif _normal_text(payload.get("contract_id")) != _normal_text(
        expected_contract_id
    ):
        mismatches.append("controller_contract_id_mismatch")
    if _normal_text(payload.get("endpoint_source")).casefold() != _normal_text(
        expected_endpoint_source
    ).casefold():
        mismatches.append("controller_contract_endpoint_source_mismatch")
    if _normal_text(payload.get("execution_mode")).casefold() != _normal_text(
        expected_execution_mode
    ).casefold():
        mismatches.append("controller_contract_execution_mode_mismatch")

    tool = payload.get("tool_handover")
    if require_tool_handover:
        if not isinstance(tool, Mapping):
            mismatches.append("tool_handover_contract_missing")
        else:
            if _normal_text(tool.get("endpoint")) != _normal_text(
                expected_tool_handover_endpoint
            ):
                mismatches.append("tool_handover_endpoint_mismatch")
            if tool.get("type") != TOOL_HANDOVER_ACTION_TYPE:
                mismatches.append("tool_handover_type_mismatch")
            if tool.get("abi_fingerprint") != TOOL_HANDOVER_ACTION_ABI_FINGERPRINT:
                mismatches.append("tool_handover_abi_mismatch")
            expected_policy = get_capability_policy(expected_capability_policy_id)
            if expected_policy is None:
                mismatches.append("tool_handover_capability_policy_unconfigured")
            elif _normal_text(tool.get("capability_policy_id")) != expected_policy.policy_id:
                mismatches.append("tool_handover_capability_policy_mismatch")
            elif tool.get("capability_fingerprint") != expected_policy.fingerprint:
                mismatches.append("tool_handover_capability_fingerprint_mismatch")
            else:
                unsupported = sorted(
                    f"{instrument_id}|{instance_id}"
                    for instrument_id, instance_id in required_tool_instances
                    if not expected_policy.supports_tool(instrument_id, instance_id)
                )
                if unsupported:
                    mismatches.append(
                        "tool_handover_capability_missing:" + ",".join(unsupported)
                    )

    retraction = payload.get("retraction")
    if require_retraction_service:
        if not isinstance(retraction, Mapping):
            mismatches.append("retraction_contract_missing")
        else:
            if _normal_text(retraction.get("endpoint")) != _normal_text(
                expected_retraction_service_name
            ):
                mismatches.append("retraction_endpoint_mismatch")
            if retraction.get("type") != RETRACTION_SERVICE_TYPE:
                mismatches.append("retraction_type_mismatch")
            if retraction.get("abi_fingerprint") != RETRACTION_SERVICE_V1_ABI_FINGERPRINT:
                mismatches.append("retraction_abi_mismatch")
            try:
                protocol_version = int(retraction.get("protocol_version", 0) or 0)
            except (TypeError, ValueError):
                protocol_version = 0
            if protocol_version != 1:
                mismatches.append("retraction_protocol_version_mismatch")
            if require_retraction_profile_identity and not bool(
                retraction.get("supports_profile_identity")
            ):
                mismatches.append("retraction_profile_identity_unsupported")
            # A V1 Service receipt is not evidence that physical motion has
            # stopped.  An external Live route must explicitly advertise a
            # separately confirmed stop contract before it can be admitted.
            if require_physical_stop_confirmation and (
                _normal_text(retraction.get("physical_stop_confirmation"))
                .casefold()
                != "confirmed"
            ):
                mismatches.append(
                    "retraction_physical_stop_confirmation_unavailable"
                )

    return tuple(mismatches)


__all__ = [
    "CONTROLLER_CONTRACT_SCHEMA",
    "EIR_NUC_CAPABILITY_POLICY_ID",
    "EIR_NUC_EXTERNAL_CONTRACT_ID",
    "EIR_NUC_VIRTUAL_CONTRACT_ID",
    "GENERIC_EMULATOR_CAPABILITY_POLICY_ID",
    "RETRACTION_SERVICE_TYPE",
    "RETRACTION_SERVICE_V1_ABI_FINGERPRINT",
    "REVIEWED_TOOL_TRANSITIONS",
    "TOOL_HANDOVER_ACTION_ABI_FINGERPRINT",
    "TOOL_HANDOVER_ACTION_TYPE",
    "CapabilityPolicy",
    "build_controller_contract",
    "controller_contract_mismatches",
    "get_capability_policy",
    "required_tool_instances_for_spec",
    "validate_source_stamp",
    "validate_tool_handover_fields",
]
