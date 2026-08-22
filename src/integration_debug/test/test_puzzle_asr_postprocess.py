from integration_debug.puzzle_asr_postprocess import KEYWORDS, correct


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
    assert sensitivity["Malleable"] == 8
    assert sensitivity["smooth forcep"] == 8
