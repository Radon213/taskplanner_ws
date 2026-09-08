"""Static boundaries that keep the observer free of optional runtime owners."""

from __future__ import annotations

import ast
from pathlib import Path


NODE_PATH = Path(__file__).resolve().parents[1] / "integration_debug" / "node.py"

OPTIONAL_IMPORTS = {
    ("integration_debug.asr_runtime", "AsrMicrophoneRuntime"): "asr",
    ("integration_debug.surgery_record_runtime", "SurgeryRecordRuntime"): "record",
    ("integration_debug.networking", "collect_network_status"): "network",
}


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _is_capability_guarded(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    capability: str,
) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.If):
            condition = ast.unparse(current.test)
            if f"_capability_enabled('{capability}')" in condition or (
                f'_capability_enabled("{capability}")' in condition
            ):
                return True
        current = parents.get(current)
    return False


def test_optional_owners_are_not_imported_at_node_module_load() -> None:
    tree = ast.parse(NODE_PATH.read_text(encoding="utf-8"), filename=str(NODE_PATH))
    top_level_imports = {
        (node.module, alias.name)
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not top_level_imports & set(OPTIONAL_IMPORTS)


def test_optional_owner_imports_stay_inside_their_capability_boundary() -> None:
    tree = ast.parse(NODE_PATH.read_text(encoding="utf-8"), filename=str(NODE_PATH))
    parents = _parents(tree)
    seen: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            key = (node.module or "", alias.name)
            capability = OPTIONAL_IMPORTS.get(key)
            if capability is None:
                continue
            seen.add(key)
            assert _is_capability_guarded(node, parents, capability), key
    assert seen == set(OPTIONAL_IMPORTS)


def test_debug_node_has_no_legacy_text_vlm_voice_path() -> None:
    source = NODE_PATH.read_text(encoding="utf-8")

    assert "retractor_voice_interpreter" not in source
    assert "retraction_voice_vlm" not in source
    assert "vlm_interpret" not in source
