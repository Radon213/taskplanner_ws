"""Small, transport-neutral policy for declared typed ROS dispatch.

The Debug gateway deliberately does not own a second command-admission or
receipt system.  This module only answers a narrower question: given a
``kind_endpoint_type_payload_timeout`` request, is it one of the explicitly
configured endpoint/type combinations and what is its resolved endpoint for
the selected robot source?  A ROS owner supplies the actual client, payload
codec, cancellation, and controller-facing behaviour.

Keeping this policy free of :mod:`rclpy` makes the same interface usable by
any ingress owner without sharing a ROS node or a browser-specific operation
switch.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Any, Mapping


DISPATCH_KINDS = frozenset({"topic", "service", "action"})
__all__ = [
    "DISPATCH_KINDS",
    "DispatchEndpoint",
    "DispatchPolicy",
    "ResolvedDispatch",
]


@dataclass(frozen=True, slots=True)
class DispatchEndpoint:
    """One allowed typed ROS endpoint.

    ``name`` is a stable logical endpoint alias.  It is intentionally not an
    arbitrary ROS name supplied by a browser/client: the route can change
    between external and virtual robots while the allowed type remains fixed.
    """

    name: str
    kind: str
    ros_type: str
    endpoints: tuple[tuple[str, str], ...]
    # ``payload_codec`` is a compatibility adapter for the two existing
    # clinical endpoints.  New installed ROS interfaces use
    # ``payload_contract`` plus rosidl_runtime_py field mapping and therefore
    # do not require a Python codec change.
    payload_codec: str
    physical: bool
    single_flight: bool
    timeout_sec: float
    response_semantics: str = "action"
    payload_contract: Mapping[str, Any] = field(default_factory=dict)
    fixed_payload: Mapping[str, Any] = field(default_factory=dict)
    command_id_field: str = ""

    def endpoint_for(self, source: str) -> str:
        values = dict(self.endpoints)
        selected = str(source).strip().lower()
        endpoint = values.get(selected) or values.get("default")
        if not endpoint:
            raise ValueError(
                f"dispatch endpoint {self.name} has no route for source {selected}"
            )
        return endpoint

    def with_endpoint(self, source: str, endpoint: str) -> "DispatchEndpoint":
        """Return a copy with one source route overridden by runtime params."""

        source = str(source).strip().lower()
        endpoint = str(endpoint).strip()
        if not source or not endpoint.startswith("/"):
            raise ValueError("dispatch endpoint override must be an absolute ROS name")
        values = dict(self.endpoints)
        values[source] = endpoint
        return replace(self, endpoints=tuple(sorted(values.items())))


@dataclass(frozen=True, slots=True)
class ResolvedDispatch:
    """Resolved request passed from an ingress to a typed ROS adapter."""

    endpoint: DispatchEndpoint
    ros_endpoint: str
    payload: dict[str, Any]
    timeout_sec: float

    @property
    def kind(self) -> str:
        return self.endpoint.kind

    @property
    def ros_type(self) -> str:
        return self.endpoint.ros_type

    @property
    def physical(self) -> bool:
        return self.endpoint.physical

    @property
    def single_flight(self) -> bool:
        return self.endpoint.single_flight


class DispatchPolicy:
    """Validate and resolve the generic typed-dispatch request shape.

    Public interface for another owner::

        policy = DispatchPolicy.from_dispatch_config(dispatch_config)
        request = policy.resolve(payload, endpoint_source="external")
        # request.kind, request.ros_endpoint, request.ros_type,
        # request.payload, request.timeout_sec

    The class contains no ROS client and never creates a receipt, dedupe entry,
    state-machine admission, or route decision beyond the configured alias.
    """

    def __init__(
        self,
        endpoints: Mapping[str, DispatchEndpoint],
        legacy_operations: Mapping[str, str] | None = None,
    ) -> None:
        self._endpoints = dict(endpoints)
        self._legacy_operations = {
            str(key).strip().lower(): str(value).strip()
            for key, value in (legacy_operations or {}).items()
            if str(key).strip() and str(value).strip()
        }

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "DispatchPolicy":
        if not isinstance(config, Mapping):
            raise ValueError("config must be a mapping")
        raw_dispatch = config.get("dispatch", {})
        return cls.from_dispatch_config(raw_dispatch)

    @classmethod
    def from_dispatch_config(
        cls, dispatch_config: Mapping[str, Any]
    ) -> "DispatchPolicy":
        """Build from the dispatch section without a package-specific config shape."""

        raw_dispatch = dispatch_config
        if not isinstance(raw_dispatch, Mapping):
            raise ValueError("dispatch must be a mapping")
        raw_endpoints = raw_dispatch.get("endpoints", [])
        if not isinstance(raw_endpoints, list) or not raw_endpoints:
            raise ValueError("dispatch.endpoints must be a non-empty list")

        endpoints: dict[str, DispatchEndpoint] = {}
        for raw in raw_endpoints:
            endpoint = _parse_endpoint(raw)
            if endpoint.name in endpoints:
                raise ValueError(f"duplicate dispatch endpoint: {endpoint.name}")
            endpoints[endpoint.name] = endpoint

        raw_operations = raw_dispatch.get("legacy_operations", {})
        if raw_operations is None:
            raw_operations = {}
        if not isinstance(raw_operations, Mapping):
            raise ValueError("dispatch.legacy_operations must be a mapping")
        for operation, endpoint_name in raw_operations.items():
            if str(endpoint_name).strip() not in endpoints:
                raise ValueError(
                    f"dispatch legacy operation {operation!s} references an unknown endpoint"
                )
        return cls(endpoints, raw_operations)

    def with_endpoint_overrides(
        self, overrides: Mapping[tuple[str, str], str]
    ) -> "DispatchPolicy":
        """Apply runtime endpoint parameters without widening the allow policy."""

        endpoints = dict(self._endpoints)
        for (name, source), endpoint in overrides.items():
            if name not in endpoints:
                raise ValueError(f"cannot override unknown dispatch endpoint: {name}")
            if str(source).strip().lower() not in dict(endpoints[name].endpoints):
                raise ValueError(
                    "cannot override an undeclared dispatch endpoint source"
                )
            endpoints[name] = endpoints[name].with_endpoint(source, endpoint)
        return DispatchPolicy(endpoints, self._legacy_operations)

    def endpoint(self, name: str) -> DispatchEndpoint:
        try:
            return self._endpoints[str(name).strip()]
        except KeyError as exc:
            raise ValueError("unsupported typed dispatch endpoint") from exc

    def endpoint_for(self, name: str, endpoint_source: str) -> str:
        return self.endpoint(name).endpoint_for(endpoint_source)

    def resolve(
        self, payload: Mapping[str, Any], *, endpoint_source: str
    ) -> ResolvedDispatch:
        """Validate a ``kind_endpoint_type_payload_timeout`` request.

        ``endpoint`` may be the stable configured alias or the exact resolved
        ROS name.  Accepting the latter keeps command logs readable while still
        rejecting arbitrary endpoint names and type substitutions.
        """

        if not isinstance(payload, Mapping):
            raise ValueError("typed dispatch payload must be a JSON object")
        kind = str(payload.get("kind", "")).strip().lower()
        name_or_endpoint = str(payload.get("endpoint", "")).strip()
        ros_type = str(payload.get("type", "")).strip()
        raw_payload = payload.get("payload")
        if kind not in DISPATCH_KINDS:
            raise ValueError("dispatch kind must be topic, service, or action")
        if not name_or_endpoint:
            raise ValueError("dispatch endpoint is required")
        if not ros_type:
            raise ValueError("dispatch type is required")
        if not isinstance(raw_payload, Mapping):
            raise ValueError("dispatch payload must be a JSON object")

        endpoint = self._find_endpoint(
            name_or_endpoint,
            endpoint_source=endpoint_source,
        )
        if endpoint.kind != kind:
            raise ValueError("dispatch kind does not match configured endpoint")
        if endpoint.ros_type != ros_type:
            raise ValueError("dispatch type does not match configured endpoint")
        timeout_sec = _timeout_value(
            payload.get("timeout_sec", endpoint.timeout_sec),
            default=endpoint.timeout_sec,
            kind=endpoint.kind,
        )
        if timeout_sec > endpoint.timeout_sec:
            raise ValueError("dispatch timeout_sec exceeds configured endpoint limit")
        return ResolvedDispatch(
            endpoint=endpoint,
            ros_endpoint=endpoint.endpoint_for(endpoint_source),
            payload=dict(raw_payload),
            timeout_sec=timeout_sec,
        )

    def resolve_legacy_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        endpoint_source: str,
    ) -> ResolvedDispatch:
        """Convert one temporary operation alias into the generic shape.

        This is deliberately a migration adapter, not another dispatch path.
        New callers must use :meth:`resolve` with ``operation=dispatch``.
        """

        name = self._legacy_operations.get(str(operation).strip().lower())
        if not name:
            raise ValueError("unsupported legacy dispatch operation")
        endpoint = self.endpoint(name)
        return self.resolve(
            {
                "kind": endpoint.kind,
                "endpoint": endpoint.name,
                "type": endpoint.ros_type,
                "payload": dict(payload),
                "timeout_sec": endpoint.timeout_sec,
            },
            endpoint_source=endpoint_source,
        )

    def public_policy(self, endpoint_source: str) -> list[dict[str, Any]]:
        """Return safe, browser-readable endpoint/type policy rows."""

        return [
            {
                "name": endpoint.name,
                "kind": endpoint.kind,
                "endpoint": endpoint.endpoint_for(endpoint_source),
                "type": endpoint.ros_type,
                "physical": endpoint.physical,
                "single_flight": endpoint.single_flight,
                "timeout_sec": endpoint.timeout_sec,
                "response_semantics": endpoint.response_semantics,
                "payload_contract": dict(endpoint.payload_contract),
                "command_id_field": endpoint.command_id_field,
            }
            for endpoint in sorted(self._endpoints.values(), key=lambda value: value.name)
        ]

    def _find_endpoint(self, name_or_endpoint: str, *, endpoint_source: str) -> DispatchEndpoint:
        direct = self._endpoints.get(name_or_endpoint)
        if direct is not None:
            return direct
        matches = [
            endpoint
            for endpoint in self._endpoints.values()
            if endpoint.endpoint_for(endpoint_source) == name_or_endpoint
        ]
        if len(matches) != 1:
            raise ValueError("dispatch endpoint is not in the configured policy")
        return matches[0]


def _parse_endpoint(raw: object) -> DispatchEndpoint:
    if not isinstance(raw, Mapping):
        raise ValueError("each dispatch endpoint must be a mapping")
    name = str(raw.get("name", "")).strip()
    kind = str(raw.get("kind", "")).strip().lower()
    ros_type = str(raw.get("type", "")).strip()
    codec = str(raw.get("payload_codec", "")).strip()
    if not name or not kind or not ros_type:
        raise ValueError("dispatch endpoint requires name, kind, and type")
    if kind not in DISPATCH_KINDS:
        raise ValueError(f"unsupported dispatch kind: {kind}")
    if "/" not in ros_type:
        raise ValueError("dispatch endpoint type must be a ROS package/type name")
    raw_endpoints = raw.get("endpoints")
    if isinstance(raw_endpoints, str):
        raw_endpoints = {"default": raw_endpoints}
    if not isinstance(raw_endpoints, Mapping) or not raw_endpoints:
        raise ValueError("dispatch endpoint requires endpoints mapping")
    endpoints: dict[str, str] = {}
    for source, endpoint in raw_endpoints.items():
        source_name = str(source).strip().lower()
        endpoint_name = str(endpoint).strip()
        if not source_name or not endpoint_name.startswith("/"):
            raise ValueError("dispatch endpoints must be absolute ROS names")
        endpoints[source_name] = endpoint_name
    timeout_sec = _timeout_value(raw.get("timeout_sec", 0.0), default=0.0, kind=kind)
    if kind == "topic" and timeout_sec != 0.0:
        raise ValueError("topic dispatch timeout_sec must be 0")
    response_semantics = str(raw.get("response_semantics", "action")).strip()
    if response_semantics not in {"action", "admission", "none"}:
        raise ValueError("dispatch response_semantics must be action, admission, or none")
    physical = bool(raw.get("physical", False))
    single_flight = bool(raw.get("single_flight", False))
    if physical and kind == "topic":
        raise ValueError("physical dispatch endpoints must use action or service")
    if physical and not single_flight:
        raise ValueError("physical dispatch endpoints must enable single_flight")
    raw_contract = raw.get("payload_contract")
    if raw_contract is None:
        raw_contract = {}
    if not isinstance(raw_contract, Mapping):
        raise ValueError("dispatch payload_contract must be a mapping")
    if not codec and "payload_contract" not in raw:
        raise ValueError(
            "generic dispatch endpoint requires payload_contract; use payload_codec only for a legacy adapter"
        )
    contract: dict[str, Any] = {}
    for field_name, field_rule in raw_contract.items():
        field_key = str(field_name).strip()
        if not field_key or not isinstance(field_rule, Mapping):
            raise ValueError("dispatch payload_contract fields must have mapping rules")
        contract[field_key] = dict(field_rule)
    raw_fixed_payload = raw.get("fixed_payload", {})
    if raw_fixed_payload is None:
        raw_fixed_payload = {}
    if not isinstance(raw_fixed_payload, Mapping):
        raise ValueError("dispatch fixed_payload must be a mapping")
    fixed_payload = {str(key): value for key, value in raw_fixed_payload.items()}
    if set(contract).intersection(fixed_payload):
        raise ValueError("dispatch payload_contract and fixed_payload must not share fields")
    command_id_field = str(
        raw.get("command_id_field", "command_id" if physical else "")
    ).strip()
    if physical and not command_id_field:
        raise ValueError("physical dispatch endpoint requires command_id_field")
    return DispatchEndpoint(
        name=name,
        kind=kind,
        ros_type=ros_type,
        endpoints=tuple(sorted(endpoints.items())),
        payload_codec=codec,
        physical=physical,
        single_flight=single_flight,
        timeout_sec=timeout_sec,
        response_semantics=response_semantics,
        payload_contract=contract,
        fixed_payload=fixed_payload,
        command_id_field=command_id_field,
    )


def _timeout_value(raw: object, *, default: float, kind: str) -> float:
    if raw is None:
        raw = default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("dispatch timeout_sec must be numeric") from exc
    if not math.isfinite(value) or value < 0.0:
        raise ValueError("dispatch timeout_sec must be finite and non-negative")
    if kind in {"service", "action"} and value <= 0.0:
        raise ValueError("service/action dispatch timeout_sec must be greater than 0")
    return value
