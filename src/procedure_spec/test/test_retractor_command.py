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
        ("직접 교실 시작", RetractionState.IDLE, RetractionCommand.START_DIRECT_TEACH),
        ("손으로 가르쳐 시작", RetractionState.IDLE, RetractionCommand.START_DIRECT_TEACH),
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
            "툴을 바꿔줘",
            RetractionState.IDLE,
            RetractionCommand.CHANGE_TOOL,
        ),
        (
            "새 도구로 교환",
            RetractionState.IDLE,
            RetractionCommand.CHANGE_TOOL,
        ),
        (
            "새 장비로 바꿔줘",
            RetractionState.IDLE,
            RetractionCommand.CHANGE_TOOL,
        ),
        (
            "도구를 교환해",
            RetractionState.IDLE,
            RetractionCommand.CHANGE_TOOL,
        ),
        (
            "기구 바꿔",
            RetractionState.IDLE,
            RetractionCommand.CHANGE_TOOL,
        ),
        (
            "stop retraction",
            RetractionState.RETRACTION_ACTIVE,
            RetractionCommand.STOP_RETRACTION,
        ),
        (
            "리트랙션 스톱",
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
    ("transcript", "side"),
    [
        ("직접 교시 종료", RetractionTargetSide.NONE),
        ("왼팔 직접 교시 종료", RetractionTargetSide.LEFT),
        ("오른쪽 직접 교시 종료", RetractionTargetSide.RIGHT),
    ],
)
def test_finish_direct_teach_accepts_optional_target_side(
    transcript: str,
    side: RetractionTargetSide,
) -> None:
    normalized = normalize_retractor_command(
        transcript,
        RetractionState.DIRECT_TEACHING,
    )

    assert normalized.command is RetractionCommand.FINISH_DIRECT_TEACH
    assert normalized.target_side is side


def test_finish_direct_teach_rejects_bilateral_target() -> None:
    normalized = normalize_retractor_command(
        "양팔 직접 교시 종료",
        RetractionState.DIRECT_TEACHING,
    )

    assert normalized.command is None
    assert normalized.reason == "ambiguous_target_side"


@pytest.mark.parametrize(
    ("transcript", "side"),
    [
        ("왼쪽 완료", RetractionTargetSide.LEFT),
        ("오른쪽 마칠게", RetractionTargetSide.RIGHT),
        ("이제 끝낼게", RetractionTargetSide.NONE),
    ],
)
def test_direct_teach_state_accepts_elliptical_finish_with_optional_side(
    transcript: str,
    side: RetractionTargetSide,
) -> None:
    normalized = normalize_retractor_command(
        transcript,
        RetractionState.DIRECT_TEACHING,
    )

    assert normalized.command is RetractionCommand.FINISH_DIRECT_TEACH
    assert normalized.target_side is side


def test_retraction_state_does_not_treat_a_side_only_finish_as_stop() -> None:
    normalized = normalize_retractor_command(
        "오른쪽 완료",
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command is None
    assert normalized.reason == "no_supported_command"


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


def test_bilateral_adjustment_can_be_normalized_in_debug_state_bypass() -> None:
    normalized = normalize_retractor_command(
        "양쪽으로 1mm씩 당겨줘",
        RetractionState.IDLE,
        enforce_state=False,
    )

    assert normalized.command is RetractionCommand.ADJUST_RETRACTION
    assert normalized.target_side is RetractionTargetSide.BOTH
    assert normalized.distance_m == pytest.approx(0.001)


@pytest.mark.parametrize(
    ("transcript", "side", "distance_m"),
    [
        ("오른쪽 리트랙션 1cm 덜 당겨줘", RetractionTargetSide.RIGHT, -0.010),
        ("왼쪽 5mm만 덜 땡겨줘", RetractionTargetSide.LEFT, -0.005),
        ("양쪽으로 1mm씩 덜 당겨줘", RetractionTargetSide.BOTH, -0.001),
        ("right retraction 1 cm pull less", RetractionTargetSide.RIGHT, -0.010),
    ],
)
def test_less_pull_reuses_adjustment_with_negative_distance(
    transcript: str,
    side: RetractionTargetSide,
    distance_m: float,
) -> None:
    normalized = normalize_retractor_command(
        transcript,
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command is RetractionCommand.ADJUST_RETRACTION
    assert normalized.target_side is side
    assert normalized.distance_m == pytest.approx(distance_m)
    assert normalized.reason.endswith("_less_pull")


@pytest.mark.parametrize(
    ("transcript", "distance_m"),
    [
        ("오른쪽 다섯 센치 더", 0.050),
        ("왼쪽 한 센티 더", 0.010),
    ],
)
def test_adjustment_parses_korean_number_words_with_units(
    transcript: str,
    distance_m: float,
) -> None:
    normalized = normalize_retractor_command(
        transcript,
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command is RetractionCommand.ADJUST_RETRACTION
    assert normalized.distance_m == pytest.approx(distance_m)


def test_adjustment_rejects_a_korean_distance_above_the_contract_limit() -> None:
    normalized = normalize_retractor_command(
        "오른쪽 십 센치 더",
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command is None
    assert normalized.reason == "invalid_adjustment_distance"


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
        ("양쪽으로 1mm씩 당겨줘", RetractionTargetSide.BOTH, 0.001),
        ("both 1 mm more", RetractionTargetSide.BOTH, 0.001),
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
        ("left right 5cm", "ambiguous_target_side"),
        ("양쪽 오른쪽 5cm 더", "ambiguous_target_side"),
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


def test_debug_normalization_can_skip_only_the_local_state_admission_gate() -> None:
    normalized = normalize_retractor_command(
        "리트랙션 종료",
        RetractionState.IDLE,
        enforce_state=False,
    )

    assert normalized.command is RetractionCommand.STOP_RETRACTION
    assert normalized.target_side is RetractionTargetSide.NONE
    assert normalized.reason == "normalized_stop_retraction"


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
