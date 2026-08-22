"""Resolve perception provider placement before any ROS nodes are started."""

from __future__ import annotations

from launch.actions import SetLaunchConfiguration
from launch.substitutions import LaunchConfiguration

from simulation_runtime.cv_contract import (
    resolve_perception_selection,
    validate_perception_endpoint,
)


def _configuration(context, name: str) -> str:
    return LaunchConfiguration(name).perform(context).strip()


def resolve_launch_perception(context):
    """Normalize new axes and their legacy aliases, failing closed on gaps."""

    try:
        selection = resolve_perception_selection(
            provider=_configuration(context, "perception_provider"),
            location=_configuration(context, "perception_location"),
            legacy_backend=_configuration(context, "perception_backend"),
        )

        explicit_endpoint = _configuration(context, "perception_endpoint")
        legacy_endpoint = _configuration(context, "rfdetr_service_url")
        # Older direct unit/integration invocations predate the optional alias;
        # the provider-specific local default below remains authoritative.
        pnu_endpoint = str(
            context.launch_configurations.get("pnu_service_url", "")
        ).strip()
        # A disabled provider deliberately ignores the legacy RF-DETR default.
        # An explicitly supplied new endpoint is still rejected by the shared
        # validator, preventing stale connection settings from being mistaken
        # for a disabled, fail-closed deployment.
        if explicit_endpoint or selection.provider == "disabled":
            endpoint_candidate = explicit_endpoint
        elif selection.provider == "pnu_hand_blood":
            # The PNU worker has its own versioned API and port.  An explicit
            # PNU alias wins; an explicit local selection otherwise gets the
            # safe loopback default.  The legacy external mapping may still
            # consume RFDETR_SERVICE_URL so existing deployments can migrate
            # without silently routing the new provider through the old API.
            endpoint_candidate = pnu_endpoint
            if not endpoint_candidate and selection.source == "legacy_backend":
                endpoint_candidate = legacy_endpoint
            if not endpoint_candidate and selection.location == "local":
                endpoint_candidate = "http://127.0.0.1:8020"
        else:
            endpoint_candidate = legacy_endpoint
        endpoint = validate_perception_endpoint(endpoint_candidate, selection)
    except ValueError as exc:
        raise RuntimeError(f"invalid perception configuration: {exc}") from exc

    return [
        SetLaunchConfiguration("perception_provider", selection.provider),
        SetLaunchConfiguration("perception_location", selection.location),
        SetLaunchConfiguration("perception_backend", selection.legacy_backend),
        SetLaunchConfiguration("perception_endpoint", endpoint),
        # Keep the old launch argument as an exact alias so existing bridge code
        # and direct launch invocations receive the same resolved endpoint.
        SetLaunchConfiguration("rfdetr_service_url", endpoint),
        SetLaunchConfiguration("perception_selection_source", selection.source),
    ]
