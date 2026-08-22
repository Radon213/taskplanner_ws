"""Taskplanner's versioned PNU Hand/Tool/Blood inference worker."""

API_VERSION = "v1"
REQUEST_SCHEMA = "taskplanner.pnu_perception.request.v1"
RESPONSE_SCHEMA = "taskplanner.pnu_perception.response.v1"
HEALTH_SCHEMA = "taskplanner.pnu_perception.health.v1"
CAPABILITIES_SCHEMA = "taskplanner.pnu_perception.capabilities.v1"
UPSTREAM_COMMIT = "0f9e93115b8cc1d470398c92e010e3fc6ef1de5d"

__all__ = [
    "API_VERSION",
    "CAPABILITIES_SCHEMA",
    "HEALTH_SCHEMA",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "UPSTREAM_COMMIT",
]
