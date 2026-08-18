from integration_debug.bridge_policy import (
    DEBUG_ACTIONS_ALLOWLIST,
    DEBUG_CAPABILITY_CLASS_NAMES,
    DEBUG_MULTICAM_SERVICES_ALLOWLIST,
    DEBUG_MULTICAM_SUBSCRIBE_ALLOWLIST,
    DEBUG_ROSAPI_SERVICES_ALLOWLIST,
    DEBUG_ROSAPI_TOPICS_GLOB,
    DEBUG_SERVICES_ALLOWLIST,
    DEBUG_TOPICS_ALLOWLIST,
    DEBUG_TOPICS_PUBLISH_ALLOWLIST,
    DEBUG_TOPICS_SUBSCRIBE_ALLOWLIST,
    MULTICAM_OBSERVER_ACTIONS_ALLOWLIST,
    MULTICAM_OBSERVER_CAPABILITY_CLASS_NAMES,
    MULTICAM_OBSERVER_ROSAPI_TOPICS_GLOB,
    MULTICAM_OBSERVER_SERVICES_ALLOWLIST,
    MULTICAM_OBSERVER_TOPICS_PUBLISH_ALLOWLIST,
    MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST,
    OPERATIONAL_DEBUG_SERVICES_ALLOWLIST,
    restrict_debug_rosbridge_protocol,
    restrict_multicam_observer_rosbridge_protocol,
    restrict_operational_debug_rosbridge_protocol,
)


def test_debug_rosbridge_policy_is_exact_and_has_only_readonly_rosapi_topics() -> None:
    restricted = restrict_debug_rosbridge_protocol(
        {
            "topics_pub_glob": None,
            "topics_sub_glob": None,
            "services_glob": ["*"],
            "actions_glob": ["*"],
            "max_message_size": 1_000_000,
        }
    )

    assert restricted["topics_pub_glob"] == list(
        DEBUG_TOPICS_PUBLISH_ALLOWLIST
    ) == ["/integration/debug/heartbeat"]
    assert restricted["topics_sub_glob"] == list(DEBUG_TOPICS_SUBSCRIBE_ALLOWLIST)
    assert set(DEBUG_MULTICAM_SUBSCRIBE_ALLOWLIST).issubset(restricted["topics_sub_glob"])
    assert restricted["topics_glob"] == list(DEBUG_TOPICS_ALLOWLIST)
    assert restricted["services_glob"] == list(DEBUG_SERVICES_ALLOWLIST)
    assert set(DEBUG_MULTICAM_SERVICES_ALLOWLIST).issubset(restricted["services_glob"])
    assert set(DEBUG_ROSAPI_SERVICES_ALLOWLIST).issubset(restricted["services_glob"])
    assert restricted["actions_glob"] == list(DEBUG_ACTIONS_ALLOWLIST) == []
    assert "/rosapi/*" not in restricted["services_glob"]
    assert {
        pattern for pattern in restricted["services_glob"] if pattern.startswith("/rosapi")
    } == {"/rosapi/topics"}
    assert restricted["max_message_size"] == 1_000_000


def test_debug_rosbridge_policy_cannot_be_widened_by_input_parameters() -> None:
    restricted = restrict_debug_rosbridge_protocol(
        {
            "topics_pub_glob": ["*"],
            "topics_sub_glob": ["*"],
            "services_glob": ["*", "/rosapi/*"],
            "actions_glob": ["*"],
        }
    )

    assert "*" not in restricted["topics_pub_glob"]
    assert "*" not in restricted["topics_sub_glob"]
    assert "*" not in restricted["topics_glob"]
    assert "*" not in restricted["services_glob"]
    assert restricted["actions_glob"] == []


def test_debug_rosbridge_policy_excludes_live_runtime_endpoints() -> None:
    restricted = restrict_debug_rosbridge_protocol({})
    denied_topics = {
        "/simulation/control_state",
        "/sensors/surgeon/sentence",
        "/surgery/context",
    }
    assert denied_topics.isdisjoint(restricted["topics_glob"])
    assert "/integration/check_readiness" not in restricted["services_glob"]


def test_operational_debug_policy_denies_world_anchor_mutations() -> None:
    restricted = restrict_operational_debug_rosbridge_protocol(
        {"services_glob": ["*", "/world_anchor_node/*"]}
    )
    assert restricted["services_glob"] == list(
        OPERATIONAL_DEBUG_SERVICES_ALLOWLIST
    ) == ["/integration/debug/command", "/rosapi/topics"]
    assert set(DEBUG_MULTICAM_SERVICES_ALLOWLIST).isdisjoint(
        restricted["services_glob"]
    )
    assert "*" not in restricted["services_glob"]


def test_browser_bridge_policies_deny_runtime_transition_interlocks() -> None:
    transition_services = {
        "/simulation/check_transition_ready",
        "/simulation/reserve_transition",
    }
    for restricted in (
        restrict_debug_rosbridge_protocol({}),
        restrict_operational_debug_rosbridge_protocol({}),
        restrict_multicam_observer_rosbridge_protocol({}),
    ):
        assert transition_services.isdisjoint(restricted["services_glob"])


def test_multicam_rosapi_uses_the_same_bounded_topic_patterns() -> None:
    assert DEBUG_ROSAPI_TOPICS_GLOB.startswith("[")
    assert DEBUG_ROSAPI_TOPICS_GLOB.endswith("]")
    for pattern in DEBUG_TOPICS_SUBSCRIBE_ALLOWLIST:
        assert pattern in DEBUG_ROSAPI_TOPICS_GLOB


def test_debug_rosbridge_has_only_browser_required_capabilities() -> None:
    assert DEBUG_CAPABILITY_CLASS_NAMES == (
        "Advertise",
        "Publish",
        "Subscribe",
        "Defragment",
        "CallService",
    )
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


def test_multicam_observer_policy_is_strictly_read_only() -> None:
    restricted = restrict_multicam_observer_rosbridge_protocol(
        {
            "topics_glob": ["*"],
            "topics_pub_glob": ["*"],
            "topics_sub_glob": ["*"],
            "services_glob": ["*", "/rosapi/*"],
            "actions_glob": ["*"],
            "max_message_size": 1_000_000,
        }
    )

    assert restricted["topics_glob"] == list(
        MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST
    )
    assert restricted["topics_sub_glob"] == list(
        MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST
    )
    assert restricted["topics_pub_glob"] == list(
        MULTICAM_OBSERVER_TOPICS_PUBLISH_ALLOWLIST
    ) == []
    assert restricted["services_glob"] == list(
        MULTICAM_OBSERVER_SERVICES_ALLOWLIST
    ) == ["/multicam_observer/rosapi/topics"]
    assert restricted["actions_glob"] == list(
        MULTICAM_OBSERVER_ACTIONS_ALLOWLIST
    ) == []
    assert restricted["max_message_size"] == 1_000_000


def test_multicam_observer_denies_every_mutating_endpoint() -> None:
    restricted = restrict_multicam_observer_rosbridge_protocol({})
    denied_services = {
        "/integration/debug/command",
        "/world_anchor_node/begin",
        "/world_anchor_node/stop",
        "/world_anchor_node/solve",
        "/world_anchor_node/publish",
        "/rosapi/topics",
        "/rosapi/*",
    }
    assert denied_services.isdisjoint(restricted["services_glob"])
    assert "/integration/debug/heartbeat" not in restricted["topics_glob"]
    assert restricted["topics_pub_glob"] == []
    assert restricted["actions_glob"] == []


def test_multicam_observer_exposes_only_subscription_capabilities() -> None:
    assert MULTICAM_OBSERVER_CAPABILITY_CLASS_NAMES == (
        "Subscribe",
        "Defragment",
        "CallService",
    )
    forbidden = {
        "Advertise",
        "Publish",
        "AdvertiseService",
        "ServiceResponse",
        "UnadvertiseService",
        "AdvertiseAction",
        "ActionFeedback",
        "ActionResult",
        "SendActionGoal",
        "UnadvertiseAction",
    }
    assert forbidden.isdisjoint(MULTICAM_OBSERVER_CAPABILITY_CLASS_NAMES)


def test_multicam_observer_rosapi_is_namespaced_and_topic_filtered() -> None:
    assert MULTICAM_OBSERVER_ROSAPI_TOPICS_GLOB.startswith("[")
    assert MULTICAM_OBSERVER_ROSAPI_TOPICS_GLOB.endswith("]")
    for pattern in MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST:
        assert pattern in MULTICAM_OBSERVER_ROSAPI_TOPICS_GLOB
    assert "/multicam_node/*" in MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST
