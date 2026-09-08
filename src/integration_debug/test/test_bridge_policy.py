"""Research Debug bridge policy: broad read visibility, narrow write paths."""

from integration_debug.bridge_policy import (
    DEBUG_ACTIONS_ALLOWLIST,
    DEBUG_CAPABILITY_CLASS_NAMES,
    DEBUG_ROSAPI_SERVICES_ALLOWLIST,
    DEBUG_SERVICES_ALLOWLIST,
    DEBUG_TOPICS_PUBLISH_ALLOWLIST,
    DEBUG_TOPICS_SUBSCRIBE_ALLOWLIST,
    MULTICAM_OBSERVER_ACTIONS_ALLOWLIST,
    MULTICAM_OBSERVER_CAPABILITY_CLASS_NAMES,
    MULTICAM_OBSERVER_SERVICES_ALLOWLIST,
    MULTICAM_OBSERVER_TOPICS_PUBLISH_ALLOWLIST,
    MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST,
    OPERATIONAL_DEBUG_SERVICES_ALLOWLIST,
    restrict_debug_rosbridge_protocol,
    restrict_multicam_observer_rosbridge_protocol,
    restrict_operational_debug_rosbridge_protocol,
)


def test_debug_observer_can_read_any_ros_topic_without_write_widening() -> None:
    restricted = restrict_debug_rosbridge_protocol(
        {
            "topics_pub_glob": ["*"],
            "topics_sub_glob": ["/arbitrary/topic"],
            "services_glob": ["*"],
            "actions_glob": ["*"],
        }
    )

    assert DEBUG_TOPICS_SUBSCRIBE_ALLOWLIST == ("*",)
    assert restricted["topics_sub_glob"] == ["*"]
    assert restricted["topics_glob"] == ["*", "/integration/debug/heartbeat"]
    assert restricted["topics_pub_glob"] == list(
        DEBUG_TOPICS_PUBLISH_ALLOWLIST
    ) == ["/integration/debug/heartbeat"]
    assert restricted["services_glob"] == list(DEBUG_SERVICES_ALLOWLIST)
    assert restricted["actions_glob"] == list(DEBUG_ACTIONS_ALLOWLIST) == []


def test_debug_mutable_services_stay_at_the_interlocked_gateway() -> None:
    restricted = restrict_operational_debug_rosbridge_protocol(
        {"services_glob": ["*", "/world_anchor_node/*"]}
    )

    assert restricted["services_glob"] == list(
        OPERATIONAL_DEBUG_SERVICES_ALLOWLIST
    )
    assert "/integration/debug/command" in restricted["services_glob"]
    assert set(DEBUG_ROSAPI_SERVICES_ALLOWLIST).issubset(
        restricted["services_glob"]
    )
    assert "*" not in restricted["services_glob"]
    assert "/simulation/check_transition_ready" not in restricted["services_glob"]


def test_multicam_observer_is_graph_wide_but_strictly_read_only() -> None:
    restricted = restrict_multicam_observer_rosbridge_protocol(
        {
            "topics_glob": ["/arbitrary/topic"],
            "topics_pub_glob": ["*"],
            "topics_sub_glob": ["/arbitrary/topic"],
            "services_glob": ["*"],
            "actions_glob": ["*"],
        }
    )

    assert MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST == ("*",)
    assert restricted["topics_glob"] == ["*"]
    assert restricted["topics_sub_glob"] == ["*"]
    assert restricted["topics_pub_glob"] == list(
        MULTICAM_OBSERVER_TOPICS_PUBLISH_ALLOWLIST
    ) == []
    assert restricted["services_glob"] == list(
        MULTICAM_OBSERVER_SERVICES_ALLOWLIST
    ) == ["/multicam_observer/rosapi/topics"]
    assert restricted["actions_glob"] == list(
        MULTICAM_OBSERVER_ACTIONS_ALLOWLIST
    ) == []


def test_bridge_capabilities_do_not_expose_browser_action_protocols() -> None:
    forbidden = {
        "AdvertiseService",
        "ServiceResponse",
        "UnadvertiseService",
        "AdvertiseAction",
        "ActionFeedback",
        "ActionResult",
        "SendActionGoal",
        "UnadvertiseAction",
    }
    assert forbidden.isdisjoint(DEBUG_CAPABILITY_CLASS_NAMES)
    assert MULTICAM_OBSERVER_CAPABILITY_CLASS_NAMES == (
        "Subscribe",
        "Defragment",
        "CallService",
    )
