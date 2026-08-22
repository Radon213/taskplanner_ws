from __future__ import annotations

import pytest

from procedure_spec import (
    DEFAULT_ADJUSTMENT_DISTANCE_M,
    RetractionCommand,
    RetractionState,
    RetractionTargetSide,
    allowed_retractor_commands,
    apply_retractor_service_admission,
    normalize_retractor_adjustment_parameters,
    normalize_retractor_command,
)


@pytest.mark.parametrize(
    ("transcript", "state", "command"),
    [
        ("직접 교시 시작", RetractionState.IDLE, RetractionCommand.START_DIRECT_TEACH),
        (
            "다이렉트 티치 종료",
            RetractionState.DIRECT_TEACHING,
            RetractionCommand.FINISH_DIRECT_TEACH,
        ),
        (
            "리트렉션 시작",
            RetractionState.TAUGHT_READY,
            RetractionCommand.START_RETRACTION,
        ),
        (
            "tool chage",
            RetractionState.IDLE,
            RetractionCommand.CHANGE_TOOL,
        ),
        (
            "이제 툴 바꿔",
            RetractionState.IDLE,
            RetractionCommand.CHANGE_TOOL,
        ),
        (
            "stop retraction",
            RetractionState.RETRACTION_ACTIVE,
            RetractionCommand.STOP_RETRACTION,
        ),
    ],
)
def test_normalizes_all_non_adjustment_commands(
    transcript: str,
    state: RetractionState,
    command: RetractionCommand,
) -> None:
    normalized = normalize_retractor_command(transcript, state)

    assert normalized.command == command
    assert normalized.target_side == RetractionTargetSide.NONE
    assert normalized.distance_m == 0.0
    assert normalized.confidence > 0.0


@pytest.mark.parametrize(
    "transcript",
    [
        "왼 쪽 리트랙션 5cm 더",
        "왼쪽리트렉션 5 cm 더",
        "left retraction 5 centimeters more",
    ],
)
def test_adjustment_accepts_common_spacing_and_five_cm_forms(transcript: str) -> None:
    normalized = normalize_retractor_command(
        transcript,
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command == RetractionCommand.ADJUST_RETRACTION
    assert normalized.target_side == RetractionTargetSide.LEFT
    assert normalized.distance_m == pytest.approx(0.050)
    assert normalized.reason == "normalized_adjust_retraction_explicit_adjustment_distance"


def test_adjustment_accepts_korean_stt_side_typo() -> None:
    normalized = normalize_retractor_command(
        "오룬 쪽 리트렉션 5센치 더",
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command == RetractionCommand.ADJUST_RETRACTION
    assert normalized.target_side == RetractionTargetSide.RIGHT
    assert normalized.distance_m == pytest.approx(0.050)


def test_adjustment_without_a_number_uses_only_the_demo_default() -> None:
    normalized = normalize_retractor_command(
        "left retraction more",
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command == RetractionCommand.ADJUST_RETRACTION
    assert normalized.target_side == RetractionTargetSide.LEFT
    assert normalized.distance_m == DEFAULT_ADJUSTMENT_DISTANCE_M
    assert normalized.reason == "normalized_adjust_retraction_default_adjustment_distance"


@pytest.mark.parametrize(
    ("transcript", "side", "distance_m"),
    [
        ("오른쪽으로 한 번만 더 당겨", RetractionTargetSide.RIGHT, 0.050),
        ("왼쪽 5 cm", RetractionTargetSide.LEFT, 0.050),
        ("left 25 mm", RetractionTargetSide.LEFT, 0.025),
    ],
)
def test_adjustment_parameter_grounding_is_independent_of_intent_words(
    transcript: str,
    side: RetractionTargetSide,
    distance_m: float,
) -> None:
    grounded = normalize_retractor_adjustment_parameters(transcript)

    assert grounded.command == RetractionCommand.ADJUST_RETRACTION
    assert grounded.target_side == side
    assert grounded.distance_m == pytest.approx(distance_m)
    assert grounded.reason.startswith("grounded_adjust_retraction_")


@pytest.mark.parametrize(
    ("transcript", "reason"),
    [
        ("5cm 더", "adjustment_side_missing"),
        ("양쪽 5cm 더", "ambiguous_target_side"),
        ("left right 5cm", "ambiguous_target_side"),
        ("오른쪽 -5cm", "invalid_adjustment_distance"),
        ("오른쪽 6cm", "invalid_adjustment_distance"),
        ("오른쪽 5", "adjustment_distance_unit_missing"),
    ],
)
def test_adjustment_parameter_grounding_fails_closed(
    transcript: str,
    reason: str,
) -> None:
    grounded = normalize_retractor_adjustment_parameters(transcript)

    assert grounded.command is None
    assert grounded.reason == reason


@pytest.mark.parametrize(
    ("transcript", "reason"),
    [
        ("리트랙션 5cm 더", "adjustment_side_missing"),
        ("left right retraction 5cm more", "ambiguous_target_side"),
        ("왼쪽 리트랙션 5 더", "adjustment_distance_unit_missing"),
        ("왼쪽 리트랙션 5cm 10mm 더", "multiple_adjustment_distances"),
    ],
)
def test_adjustment_never_guesses_side_or_a_unit(
    transcript: str,
    reason: str,
) -> None:
    normalized = normalize_retractor_command(
        transcript,
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command is None
    assert normalized.target_side == RetractionTargetSide.NONE
    assert normalized.distance_m == 0.0
    assert normalized.confidence == 0.0
    assert normalized.reason == reason


@pytest.mark.parametrize(
    "transcript",
    [
        "left retraction -5cm more",
        "left retraction \N{MINUS SIGN}5cm more",
        "right retraction 0.051m more",
        "right retraction 5m more",
    ],
)
def test_adjustment_rejects_negative_or_out_of_range_distance(transcript: str) -> None:
    normalized = normalize_retractor_command(
        transcript,
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command is None
    assert normalized.reason == "invalid_adjustment_distance"


def test_adjustment_rejects_an_english_unit_prefix_inside_a_word() -> None:
    normalized = normalize_retractor_command(
        "right retraction 5molecule more",
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command is None
    assert normalized.reason == "adjustment_distance_unit_missing"


@pytest.mark.parametrize(
    ("transcript", "state", "reason"),
    [
        ("my friend is here", RetractionState.DIRECT_TEACHING, "no_supported_command"),
        ("my friend is here", RetractionState.RETRACTION_ACTIVE, "no_supported_command"),
        ("please restart the note", RetractionState.IDLE, "no_supported_command"),
        ("please start the note", RetractionState.IDLE, "no_supported_command"),
        (
            "upright retraction 5cm more",
            RetractionState.RETRACTION_ACTIVE,
            "adjustment_side_missing",
        ),
    ],
)
def test_english_terms_require_token_boundaries(
    transcript: str,
    state: RetractionState,
    reason: str,
) -> None:
    normalized = normalize_retractor_command(transcript, state)

    assert normalized.command is None
    assert normalized.reason == reason


def test_state_narrows_candidates_and_unknown_fails_closed() -> None:
    inactive = normalize_retractor_command("왼쪽 리트랙션 5cm 더", "idle")
    wrong_sequence = normalize_retractor_command(
        "retraction start",
        RetractionState.DIRECT_TEACHING,
    )
    unknown = normalize_retractor_command("direct teach start", "not-a-state")
    implicit_start = normalize_retractor_command("리트랙션", RetractionState.TAUGHT_READY)

    assert inactive.command is None
    assert inactive.reason == "command_not_allowed_in_idle"
    assert wrong_sequence.command is None
    assert wrong_sequence.reason == "command_not_allowed_in_direct_teaching"
    assert unknown.command is None
    assert unknown.reason == "state_unknown"
    assert implicit_start.command == RetractionCommand.START_RETRACTION


def test_allowed_commands_define_the_closed_state_machine_surface() -> None:
    assert allowed_retractor_commands("idle") == {
        RetractionCommand.START_DIRECT_TEACH,
        RetractionCommand.CHANGE_TOOL,
    }
    assert allowed_retractor_commands("taught_ready") == {
        RetractionCommand.START_DIRECT_TEACH,
        RetractionCommand.START_RETRACTION,
    }
    assert allowed_retractor_commands("retraction_active") == {
        RetractionCommand.ADJUST_RETRACTION,
        RetractionCommand.STOP_RETRACTION,
    }
    assert allowed_retractor_commands(RetractionState.UNKNOWN) == frozenset()


def test_state_transitions_only_after_service_admission() -> None:
    state = RetractionState.IDLE
    state = apply_retractor_service_admission(
        state,
        RetractionCommand.CHANGE_TOOL,
        request_accepted=True,
    )
    assert state == RetractionState.IDLE

    state = apply_retractor_service_admission(
        state,
        RetractionCommand.START_DIRECT_TEACH,
        request_accepted=False,
    )
    assert state == RetractionState.IDLE

    state = apply_retractor_service_admission(
        state,
        RetractionCommand.START_DIRECT_TEACH,
        request_accepted=True,
    )
    assert state == RetractionState.DIRECT_TEACHING
    state = apply_retractor_service_admission(
        state,
        RetractionCommand.FINISH_DIRECT_TEACH,
        request_accepted=True,
    )
    assert state == RetractionState.TAUGHT_READY
    state = apply_retractor_service_admission(
        state,
        RetractionCommand.START_RETRACTION,
        request_accepted=True,
    )
    assert state == RetractionState.RETRACTION_ACTIVE
    state = apply_retractor_service_admission(
        state,
        RetractionCommand.ADJUST_RETRACTION,
        request_accepted=True,
    )
    assert state == RetractionState.RETRACTION_ACTIVE
    state = apply_retractor_service_admission(
        state,
        RetractionCommand.STOP_RETRACTION,
        request_accepted=True,
    )
    assert state == RetractionState.IDLE


@pytest.mark.parametrize(
    "state",
    [
        RetractionState.DIRECT_TEACHING,
        RetractionState.TAUGHT_READY,
        RetractionState.RETRACTION_ACTIVE,
        RetractionState.UNKNOWN,
    ],
)
def test_tool_change_is_allowed_only_while_idle(state: RetractionState) -> None:
    normalized = normalize_retractor_command("Tool change", state)

    assert normalized.command is None
    assert normalized.reason == (
        "state_unknown"
        if state is RetractionState.UNKNOWN
        else f"command_not_allowed_in_{state.value}"
    )
    assert (
        apply_retractor_service_admission(
            state,
            RetractionCommand.CHANGE_TOOL,
            request_accepted=True,
        )
        is state
    )


@pytest.mark.parametrize(
    ("transcript", "state", "command"),
    [
        ("시작해", RetractionState.IDLE, RetractionCommand.START_DIRECT_TEACH),
        ("이제 끝", RetractionState.DIRECT_TEACHING, RetractionCommand.FINISH_DIRECT_TEACH),
        ("시작해", RetractionState.TAUGHT_READY, RetractionCommand.START_RETRACTION),
        ("이제 끝", RetractionState.RETRACTION_ACTIVE, RetractionCommand.STOP_RETRACTION),
    ],
)
def test_state_supplies_an_omitted_lifecycle_noun(
    transcript: str,
    state: RetractionState,
    command: RetractionCommand,
) -> None:
    assert normalize_retractor_command(transcript, state).command == command


def test_accepted_but_out_of_state_command_cannot_advance_local_state() -> None:
    assert apply_retractor_service_admission(
        RetractionState.IDLE,
        RetractionCommand.ADJUST_RETRACTION,
        request_accepted=True,
    ) == RetractionState.IDLE
