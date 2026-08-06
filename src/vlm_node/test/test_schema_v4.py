from __future__ import annotations

import copy

import pytest

from vlm_node.schema import SchemaValidationError, compact_vlm_json_schema, validate_payload


def _base_v4() -> dict:
    return {
        "v": "4",
        "phase": [["P04", 0.91]],
        "tool": [["T05", 0.84]],
        "intent": ["", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.09,
        "sum": "retraction request observed",
        "bed_robot_arm_group": None,
    }


def _retraction_proposal(**overrides) -> dict:
    proposal = {
        "request_id": "req-123",
        "group_id": "retraction",
        "operation": "retraction",
        "direction": "LEFT_RIGHT",
        "distance_mm": 10.0,
        "distance_origin": "qualitative_inferred",
        "raw_distance_text": "조금",
        "end_effector_profile": "army_navy",
        "rationale": "bilateral exposure is needed",
        "confidence": 0.91,
    }
    proposal.update(overrides)
    return proposal


def test_v4_accepts_null_optional_group_proposal() -> None:
    normalized = validate_payload(_base_v4())
    assert normalized["v"] == "4"
    assert normalized["gesture"] == ["", "", "", 0.0]
    assert normalized["bed_robot_arm_group"] is None


def test_v4_preserves_visual_only_open_palm_evidence() -> None:
    payload = _base_v4()
    payload["gesture"] = ["request_tool", "T05", "open_receive", 0.86]

    normalized = validate_payload(payload)

    assert normalized["gesture"] == [
        "request_tool",
        "T05",
        "open_receive",
        0.86,
    ]


def test_v4_allows_visual_request_before_tool_identity_is_resolved() -> None:
    payload = _base_v4()
    payload["gesture"] = ["request_tool", "", "open_receive", 0.79]

    normalized = validate_payload(payload)

    assert normalized["gesture"] == [
        "request_tool",
        "",
        "open_receive",
        0.79,
    ]


def test_v4_deterministically_restores_flat_single_candidate_pairs() -> None:
    payload = _base_v4()
    payload["phase"] = ["P04", 0.91]
    payload["tool"] = ["T05", 0.84]

    normalized = validate_payload(payload)

    assert normalized["phase"] == [["P04", 0.91]]
    assert normalized["tool"] == [["T05", 0.84]]


def test_v4_still_rejects_ambiguous_candidate_rows() -> None:
    payload = _base_v4()
    payload["tool"] = ["T05", "T02"]

    with pytest.raises(SchemaValidationError, match="each tool item"):
        validate_payload(payload)


def test_v4_tolerates_omitted_proposal_and_normalizes_it_to_null() -> None:
    payload = _base_v4()
    payload.pop("bed_robot_arm_group")
    assert validate_payload(payload)["bed_robot_arm_group"] is None


@pytest.mark.parametrize(
    ("raw_text", "distance_mm", "origin"),
    [
        ("1 cm", 10.0, "explicit_with_unit"),
        ("10", 10.0, "explicit_unit_inferred"),
        ("5 cm", 50.0, "explicit_with_unit"),
        ("조금", 10.0, "qualitative_inferred"),
        ("", 10.0, "defaulted"),
    ],
)
def test_v4_parses_and_rechecks_retraction_distance(
    raw_text: str,
    distance_mm: float,
    origin: str,
) -> None:
    payload = _base_v4()
    payload["bed_robot_arm_group"] = _retraction_proposal(
        raw_distance_text=raw_text,
        distance_mm=distance_mm,
        distance_origin=origin,
    )
    normalized = validate_payload(payload)["bed_robot_arm_group"]
    assert normalized["distance_mm"] == distance_mm
    assert normalized["distance_origin"] == origin
    assert normalized["raw_distance_text"] == raw_text


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("direction", "FORWARD", "direction"),
        ("group_id", "suction", "group_id"),
        ("operation", "suction_start", "operation"),
        ("distance_mm", 31, "distance mismatch"),
    ],
)
def test_v4_rejects_invalid_group_proposals(field: str, value: object, message: str) -> None:
    payload = _base_v4()
    proposal = _retraction_proposal()
    if field == "distance_mm":
        proposal["raw_distance_text"] = "조금"
    proposal[field] = value
    payload["bed_robot_arm_group"] = proposal
    with pytest.raises(SchemaValidationError, match=message):
        validate_payload(payload)


def test_v4_rejects_qualitative_distance_outside_one_through_thirty() -> None:
    payload = _base_v4()
    payload["bed_robot_arm_group"] = _retraction_proposal(
        raw_distance_text="중간 정도",
        distance_mm=31,
    )
    with pytest.raises(SchemaValidationError, match="between 1 and 30"):
        validate_payload(payload)


@pytest.mark.parametrize("raw_text", ["", "당겨줘"])
def test_v4_rejects_invented_qualitative_distance_without_intensity(
    raw_text: str,
) -> None:
    payload = _base_v4()
    payload["bed_robot_arm_group"] = _retraction_proposal(
        raw_distance_text=raw_text,
        distance_mm=15,
    )
    with pytest.raises(SchemaValidationError, match="intensity expression"):
        validate_payload(payload)


def test_v1_through_v3_remain_supported() -> None:
    v3 = _base_v4()
    v3["v"] = "3"
    v3.pop("bed_robot_arm_group")
    assert validate_payload(copy.deepcopy(v3))["v"] == "3"

    assert validate_payload(
        {
            "v": "2",
            "phase": ["P01", 0.8],
            "tool": ["T01", 0.8],
            "intent": ["", "", 0.0],
            "mayo": [],
            "mayo_retrieve": ["", 0.0],
            "u": 0.2,
            "sum": "ok",
        }
    )["v"] == "2"

    assert validate_payload(
        {
            "v": "1",
            "ph": [["P01", 0.8]],
            "to": [],
            "sg": ["", "", "", 0.0],
            "u": 0.2,
            "sum": "ok",
        }
    )["v"] == "1"


def test_v4_json_schema_has_nullable_single_group_proposal_and_six_directions() -> None:
    schema = compact_vlm_json_schema("4")
    assert "bed_robot_arm_group" in schema["required"]
    proposal_schema = schema["properties"]["bed_robot_arm_group"]["anyOf"][1]
    assert proposal_schema["properties"]["direction"]["enum"] == [
        "UP",
        "DOWN",
        "LEFT",
        "RIGHT",
        "LEFT_RIGHT",
        "UP_DOWN",
    ]
    assert proposal_schema["properties"]["distance_mm"]["exclusiveMinimum"] == 0.0


def test_v4_non_numeric_uncertainty_fails_closed() -> None:
    payload = _base_v4()
    payload["u"] = "Insufficient visual evidence; explanation belongs in sum."

    normalized = validate_payload(payload)

    assert normalized["u"] == 1.0


def test_mayo_retrieve_is_derived_from_highest_recover_vote() -> None:
    payload = _base_v4()
    payload["mayo"] = [
        ["T02", "recover", 0.61],
        ["T03", "recover", 0.82],
        ["T04", "reuse", 0.93],
    ]
    payload["mayo_retrieve"] = ["T04", 0.99]

    normalized = validate_payload(payload)

    assert normalized["mayo_retrieve"] == ["T03", 0.82]


def test_mayo_reuse_cannot_also_be_retrieved() -> None:
    payload = _base_v4()
    payload["mayo"] = [["T02", "reuse", 0.95]]
    payload["mayo_retrieve"] = ["T02", 0.95]

    normalized = validate_payload(payload)

    assert normalized["mayo"] == [["T02", "reuse", 0.95]]
    assert normalized["mayo_retrieve"] == ["", 0.0]


def test_conflicting_duplicate_mayo_vote_fails_closed_on_equal_confidence() -> None:
    payload = _base_v4()
    payload["mayo"] = [
        ["T02", "recover", 0.8],
        ["T02", "reuse", 0.8],
    ]
    payload["mayo_retrieve"] = ["T02", 0.8]

    normalized = validate_payload(payload)

    assert normalized["mayo"] == [["T02", "reuse", 0.8]]
    assert normalized["mayo_retrieve"] == ["", 0.0]


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("inf")])
def test_mayo_confidence_must_be_bounded(confidence: float) -> None:
    payload = _base_v4()
    payload["mayo"] = [["T02", "reuse", confidence]]

    with pytest.raises(SchemaValidationError, match="between 0 and 1"):
        validate_payload(payload)
