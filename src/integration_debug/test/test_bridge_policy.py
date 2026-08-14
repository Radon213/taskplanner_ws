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
    restrict_debug_rosbridge_protocol,
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
