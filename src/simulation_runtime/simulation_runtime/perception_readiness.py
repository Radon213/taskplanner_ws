"""Typed perception admission helpers, independent of ROS node lifecycle.

The Production planner receives external RF-DETR facts through ROS topics.
This module owns the bounded message/provenance checks so launch selection,
preflight state management and VLM prompting do not grow another copy.
"""

from __future__ import annotations

import math

from procedure_spec import (
    ScenarioRuntimeRequirements,
    get_default_spec_dir,
    load_bundle,
)


RFDETR_TOOL_OBSERVATION_MAX_IMAGE_DIMENSION = 16_384
RFDETR_TOOL_OBSERVATION_MAX_DEPTH_M = 10.0
RFDETR_VIEWS = frozenset({"cam_3", "cam_4"})
RFDETR_VLM_ALIGNMENT_STATUSES = frozenset(
    {
        "aligned",
        "misaligned",
        "missing",
        "not_compared_no_flir_reference",
        "not_compared_missing_source_timestamp",
        "not_compared_no_fresh_detector",
        "not_compared_stale_detector",
        "omitted_receive_stale",
        "omitted_no_flir_reference",
        "omitted_missing_source_timestamp",
        "omitted_source_timestamp_misaligned",
    }
)


def requires_rfdetr_tool_location_context(scenario: object) -> bool:
    """Compatibility query backed by the central scenario resolver.

    New code should pass ``ScenarioRuntimeRequirements`` directly. Bundle-name
    callers remain supported without keeping a second detector deny/allowlist.
    """

    if isinstance(scenario, ScenarioRuntimeRequirements):
        runtime = scenario
    else:
        bundle_name = str(scenario or "").strip().casefold()
        if not bundle_name:
            return False
        try:
            runtime = load_bundle(
                get_default_spec_dir().parent / bundle_name
            ).get_scenario_runtime_requirements()
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
    return bool(runtime.rfdetr_tool_observations_required)


def nonempty_message_text(value: object) -> str:
    """Normalize generated string fields without retaining payload text."""

    return str(value or "").strip()


def _strict_uint(value: object, *, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    try:
        if float(value) != float(numeric):
            return None
    except (TypeError, ValueError):
        return None
    return numeric if 0 <= numeric <= maximum else None


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _valid_bbox(
    value: object,
    *,
    image_width: int,
    image_height: int,
) -> bool:
    if isinstance(value, (str, bytes)):
        return False
    try:
        raw_values = tuple(value)
    except TypeError:
        return False
    if len(raw_values) != 4:
        return False
    coordinates = tuple(_finite_float(item) for item in raw_values)
    if any(item is None for item in coordinates):
        return False
    x0, y0, x1, y1 = (float(item) for item in coordinates)
    x0 = min(max(x0, 0.0), float(image_width))
    x1 = min(max(x1, 0.0), float(image_width))
    y0 = min(max(y0, 0.0), float(image_height))
    y1 = min(max(y1, 0.0), float(image_height))
    return x1 > x0 and y1 > y0


def rfdetr_tool_observation_source_stamp(
    message: object,
    *,
    expected_view: str,
    expected_model_version: str = "",
) -> tuple[float | None, str]:
    """Validate one bounded RF-DETR frame and return its source timestamp.

    Fresh empty arrays are valid executed no-detection frames. A malformed row
    invalidates the whole frame; no masks, image bytes or detector free text
    cross this admission boundary.
    """

    view = str(expected_view).strip()
    if view not in RFDETR_VIEWS:
        return None, "rfdetr_tool_observations_expected_view_invalid"
    if nonempty_message_text(getattr(message, "view", "")) != view:
        return None, "rfdetr_tool_observations_view_mismatch"

    schema_version = nonempty_message_text(
        getattr(message, "schema_version", "")
    )
    observation_id = nonempty_message_text(
        getattr(message, "observation_id", "")
    )
    model_version = nonempty_message_text(
        getattr(message, "model_version", "")
    )
    ontology_version = nonempty_message_text(
        getattr(message, "ontology_version", "")
    )
    if (
        not schema_version
        or not observation_id
        or not model_version
        or not ontology_version
    ):
        return None, "rfdetr_tool_observations_provenance_invalid"
    pinned_model_version = nonempty_message_text(expected_model_version)
    if pinned_model_version and model_version != pinned_model_version:
        return None, "rfdetr_tool_observations_model_version_mismatch"
    # ``model_version`` is mandatory provenance and remains visible to
    # operators, but its spelling is producer-owned.  An empty expected value
    # deliberately means "observe, do not pin" so a reviewed provider can
    # roll checkpoints without taking the planner offline.  Exact matching is
    # retained as an explicit opt-in for experiments or incident isolation.

    sequence = _strict_uint(
        getattr(message, "sequence", None),
        maximum=2**63 - 1,
    )
    width = _strict_uint(
        getattr(message, "image_width", None),
        maximum=RFDETR_TOOL_OBSERVATION_MAX_IMAGE_DIMENSION,
    )
    height = _strict_uint(
        getattr(message, "image_height", None),
        maximum=RFDETR_TOOL_OBSERVATION_MAX_IMAGE_DIMENSION,
    )
    if sequence is None or not width or not height:
        return None, "rfdetr_tool_observations_geometry_invalid"

    header = getattr(message, "header", None)
    stamp = getattr(header, "stamp", None)
    seconds = _strict_uint(getattr(stamp, "sec", None), maximum=2**31 - 1)
    nanoseconds = _strict_uint(
        getattr(stamp, "nanosec", None),
        maximum=999_999_999,
    )
    if seconds is None or nanoseconds is None:
        return None, "rfdetr_tool_observations_source_stamp_invalid"
    source_stamp_sec = float(seconds) + float(nanoseconds) / 1_000_000_000.0
    if not math.isfinite(source_stamp_sec) or source_stamp_sec <= 0.0:
        return None, "rfdetr_tool_observations_source_stamp_invalid"

    raw_instances = getattr(message, "instances", None)
    if not isinstance(raw_instances, (list, tuple)):
        return None, "rfdetr_tool_observations_instances_invalid"
    for instance in raw_instances:
        class_name = nonempty_message_text(
            getattr(instance, "class_name", "")
        )
        confidence = _finite_float(
            getattr(instance, "class_confidence", None)
        )
        canonical_class_id = _strict_uint(
            getattr(instance, "canonical_class_id", None), maximum=65_535
        )
        model_class_index = _strict_uint(
            getattr(instance, "model_class_index", None), maximum=65_535
        )
        frame_local_instance_id = _strict_uint(
            getattr(instance, "frame_local_instance_id", None),
            maximum=2**32 - 1,
        )
        if (
            not class_name
            or confidence is None
            or not 0.0 < confidence <= 1.0
            or canonical_class_id is None
            or model_class_index is None
            or frame_local_instance_id is None
            or not _valid_bbox(
                getattr(instance, "bbox_xyxy_px", None),
                image_width=width,
                image_height=height,
            )
        ):
            return None, "rfdetr_tool_observations_instance_invalid"
        if bool(getattr(instance, "observation_point_depth_valid", False)):
            depth_m = _finite_float(
                getattr(instance, "observation_point_depth_m", None)
            )
            if (
                depth_m is None
                or not 0.0 < depth_m <= RFDETR_TOOL_OBSERVATION_MAX_DEPTH_M
            ):
                return None, "rfdetr_tool_observations_depth_invalid"

    return source_stamp_sec, ""
