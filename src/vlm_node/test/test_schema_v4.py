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
        "adjustment_mode": "multi",
        "target_retractor_id": "both_malleable",
        "direction_frame": "surgeon_view",
        "direction": "none",
        "axis": "left_right",
        "distance_mm": 10.0,
        "distance_origin": "explicit_with_unit",
        "raw_distance_text": "10 mm",
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
    ("raw_text", "distance_mm"),
    [
        ("1 mm", 1.0),
        ("0.1 cm", 1.0),
        ("1 cm", 10.0),
        ("30 mm", 30.0),
        ("3 cm", 30.0),
    ],
)
def test_v4_accepts_explicit_numeric_retraction_distance_within_limit(
    raw_text: str,
    distance_mm: float,
) -> None:
    payload = _base_v4()
    payload["bed_robot_arm_group"] = _retraction_proposal(
        raw_distance_text=raw_text,
        distance_mm=distance_mm,
        distance_origin="explicit_with_unit",
    )
    normalized = validate_payload(payload)["bed_robot_arm_group"]
    assert normalized["distance_mm"] == distance_mm
    assert normalized["distance_origin"] == "explicit_with_unit"
    assert normalized["raw_distance_text"] == raw_text


def test_v4_accepts_single_execute_retraction_adjustment_fields() -> None:
    payload = _base_v4()
    payload["bed_robot_arm_group"] = _retraction_proposal(
        adjustment_mode="single",
        target_retractor_id="right_malleable",
        direction="up",
        axis="none",
    )

    normalized = validate_payload(payload)["bed_robot_arm_group"]

    assert normalized["adjustment_mode"] == "single"
    assert normalized["target_retractor_id"] == "right_malleable"
    assert normalized["direction_frame"] == "surgeon_view"
    assert normalized["direction"] == "up"
    assert normalized["axis"] == "none"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("direction", "forward", "multi adjustment"),
        ("target_retractor_id", "left_malleable", "multi adjustment"),
        ("axis", "none", "multi adjustment"),
        ("direction_frame", "robot_base", "direction_frame"),
        ("group_id", "legacy_lane", "group_id"),
        ("operation", "unsupported_operation", "operation"),
        ("distance_mm", 11, "distance mismatch"),
    ],
)
def test_v4_rejects_invalid_group_proposals(field: str, value: object, message: str) -> None:
    payload = _base_v4()
    proposal = _retraction_proposal()
    proposal[field] = value
    payload["bed_robot_arm_group"] = proposal
    with pytest.raises(SchemaValidationError, match=message):
        validate_payload(payload)


@pytest.mark.parametrize(
    ("raw_text", "distance_mm"),
    [
        ("31 mm", 31.0),
        ("3.1 cm", 31.0),
        ("5 cm", 50.0),
    ],
)
def test_v4_rejects_explicit_distance_above_thirty_mm(
    raw_text: str,
    distance_mm: float,
) -> None:
    payload = _base_v4()
    payload["bed_robot_arm_group"] = _retraction_proposal(
        raw_distance_text=raw_text,
        distance_mm=distance_mm,
    )
    with pytest.raises(SchemaValidationError, match="30 mm contract limit"):
        validate_payload(payload)


@pytest.mark.parametrize(
    ("raw_text", "distance_origin", "message"),
    [
        ("10", "explicit_with_unit", "explicit numeric mm/cm"),
        ("조금", "qualitative_inferred", "explicit_with_unit"),
        ("", "defaulted", "explicit_with_unit"),
    ],
)
def test_v4_rejects_unitless_qualitative_and_defaulted_distance(
    raw_text: str,
    distance_origin: str,
    message: str,
) -> None:
    payload = _base_v4()
    payload["bed_robot_arm_group"] = _retraction_proposal(
        raw_distance_text=raw_text,
        distance_mm=10.0,
        distance_origin=distance_origin,
    )
    with pytest.raises(SchemaValidationError, match=message):
        validate_payload(payload)


@pytest.mark.parametrize(
    ("raw_text", "distance_mm"),
    [("0 mm", 0.0), ("-1 mm", -1.0)],
)
def test_v4_rejects_non_positive_distance(raw_text: str, distance_mm: float) -> None:
    payload = _base_v4()
    payload["bed_robot_arm_group"] = _retraction_proposal(
        raw_distance_text=raw_text,
        distance_mm=distance_mm,
    )
    with pytest.raises(SchemaValidationError, match="positive finite number"):
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


def test_v4_json_schema_has_nullable_retraction_adjustment_contract() -> None:
    schema = compact_vlm_json_schema("4")
    assert "bed_robot_arm_group" in schema["required"]
    proposal_schema = schema["properties"]["bed_robot_arm_group"]["anyOf"][1]
    assert proposal_schema["properties"]["direction"]["enum"] == [
        "up",
        "down",
        "left",
        "right",
        "none",
    ]
    assert proposal_schema["properties"]["axis"]["enum"] == [
        "left_right",
        "up_down",
        "none",
    ]
    assert proposal_schema["properties"]["adjustment_mode"]["enum"] == [
        "single",
        "multi",
    ]
    assert proposal_schema["properties"]["direction_frame"]["enum"] == [
        "surgeon_view",
    ]
    assert proposal_schema["properties"]["distance_origin"]["enum"] == [
        "explicit_with_unit",
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
