from integration_debug.puzzle_asr_postprocess import (
    KEYWORDS,
    correct,
    correct_for_command,
)


def test_received_puzzle_corrections_normalize_surgical_terms() -> None:
    corrected, changes = correct("Alice 와 mass, malleble 그리고 scattered")

    assert corrected == "Allis 와 메스, Malleable 그리고 Mosquito"
    assert changes == [
        ("Alice", "Allis"),
        ("mass", "메스"),
        ("malleble", "Malleable"),
        ("scattered", "Mosquito"),
    ]


def test_command_vocabulary_is_retained_alongside_received_zip_vocabulary() -> None:
    sensitivity = dict(KEYWORDS)

    assert sensitivity["직접 교시"] == 9
    assert sensitivity["tool change"] == 9
    assert sensitivity["Bovie"] == 8
    assert sensitivity["Malleable"] == 8
    assert sensitivity["smooth forcep"] == 8
    assert sensitivity["교시"] == 7
    assert sensitivity["당겨줘"] == 7


def test_received_additive_corrections_keep_lexical_only_canonicals() -> None:
    corrected, changes = correct(
        "O B, 5 B, bovievi, bovie 와 Adson's, Osquito, stool, 남겨줘, 교실"
    )

    assert corrected == (
        "Bovie, Bovie, Bovie, Bovie 와 Adson, Mosquito, tool, 당겨줘, 교시"
    )
    assert changes == [
        ("O B", "Bovie"),
        ("5 B", "Bovie"),
        ("bovievi", "Bovie"),
        ("bovie", "Bovie"),
        ("Adson's", "Adson"),
        ("Osquito", "Mosquito"),
        ("stool", "tool"),
        ("남겨줘", "당겨줘"),
        ("교실", "교시"),
    ]


def test_new_adson_variant_is_kept_without_a_two_letter_overcorrection() -> None:
    corrected, changes = correct("addits 와 sc")

    assert corrected == "Adson 와 sc"
    assert changes == [("addits", "Adson")]


def test_command_correction_requires_a_proven_operation_context() -> None:
    # Global ZIP correction remains useful diagnostics, but it must not turn a
    # generic phrase into a bare-tool or robot-control command.
    corrected, changes = correct_for_command("부위는 종류가 다릅니다. 남겨줘")

    assert corrected == "부위는 종류가 다릅니다. 남겨줘"
    assert changes == []

    prose, prose_changes = correct_for_command("부위 설명해줘")
    assert prose == "부위 설명해줘"
    assert prose_changes == []


def test_command_correction_repairs_only_a_contextualized_tool_or_suction() -> None:
    tool_text, tool_changes = correct_for_command("부위 주세요")
    suction_text, suction_changes = correct_for_command("obsuction 빠져")
    noisy_suction_text, noisy_suction_changes = correct_for_command(
        "obsuction 하나나 빠져"
    )

    assert tool_text == "Bovie 주세요"
    assert tool_changes == [("부위", "Bovie")]
    assert suction_text == "suction 빠져"
    assert suction_changes == [("obsuction", "suction")]
    assert noisy_suction_text == "suction 하나나 빠져"
    assert noisy_suction_changes == [("obsuction", "suction")]
