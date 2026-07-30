"""Build runtime procedure bundles directly from compact procedure-prompt YAML."""

from __future__ import annotations

from collections import OrderedDict
import math
from pathlib import Path
import re
from typing import Any

from .procedure_prompt import PROMPT_FILE_NAMES, load_procedure_prompt


_TOOL_ID_RE = re.compile(r"\bT\d{2}\b")
_FIELD_DEPLOYED_ROLE_NAMES = {
    "field_deployed",
    "fixed_retraction",
}
_GENERIC_TOOL_WORDS = {
    "atraumatic",
    "bag",
    "blade",
    "cautery",
    "clamp",
    "diathermy",
    "dissecting",
    "dissector",
    "driver",
    "endoscopic",
    "fine",
    "forceps",
    "handle",
    "hemostat",
    "holder",
    "instrument",
    "irrigator",
    "laparoscopic",
    "long",
    "needle",
    "pencil",
    "retractor",
    "retrieval",
    "scalpel",
    "scissors",
    "shears",
    "stapler",
    "suction",
    "surgical",
    "tip",
    "tool",
    "with",
}
_GENERIC_KO_TOOL_WORDS = {
    "가위",
    "그라스퍼",
    "기구",
    "니들",
    "드라이버",
    "리트랙터",
    "메스",
    "박리기",
    "석션",
    "소작기",
    "스테이플러",
    "시어",
    "어플라이어",
    "전기소작기",
    "클램프",
    "포셉",
}

_KO_TOOL_NAMES = {
    "#15 Scalpel": "15번 메스",
    "#10 Scalpel": "10번 메스",
    "Adson forceps": "애드슨 포셉",
    "DeBakey forceps": "디베이키 포셉",
    "Allis clamp forceps": "알리스 클램프 포셉",
    "Bovie surgical cautery": "보비 전기소작기",
    "Long-tip electrocautery pencil": "롱팁 전기소작 펜슬",
    "army navy retractor": "아미-네이비 리트랙터",
    "Army navy retractor": "아미-네이비 리트랙터",
    "Senn miller retractor": "센 밀러 리트랙터",
    "Bipolar cautery": "바이폴라 전기소작기",
    "Mosquito forceps": "모스키토 포셉",
    "Harmonics shears": "하모닉 시어",
    "Yankeur suction": "양카우어 석션",
    "Kocher retractor": "코처 리트랙터",
    "Thyroid retractor (Middeldorpf)": "갑상선 리트랙터(미들돌프)",
    "Laparoscopic atraumatic grasper": "복강경 비외상성 그라스퍼",
    "Laparoscopic hook cautery": "복강경 후크 전기소작기",
    "Laparoscopic scissors": "복강경 가위",
    "Suction irrigator": "석션-세척기",
    "Maryland dissector": "메릴랜드 박리기",
    "Right-angle dissector": "라이트 앵글 박리기",
    "Clip applier": "클립 어플라이어",
    "Vascular stapler": "혈관 스테이플러",
    "Endoscopic retrieval bag": "내시경 검체 회수백",
    "Laparoscopic needle driver": "복강경 니들 드라이버",
    "Army navy retractor": "아미-네이비 리트랙터",
    "Richardson retractor": "리처드슨 리트랙터",
    "Deaver retractor": "디버 리트랙터",
    "Metzenbaum scissors": "메첸바움 가위",
    "Right-angle clamp": "라이트 앵글 클램프",
    "Satinsky vascular clamp": "사틴스키 혈관 클램프",
    "Kelly clamp": "켈리 클램프",
    "Scalpel handle with #15 blade": "15번 블레이드 핸들",
    "Toothed dissecting forceps": "유구 박리 포셉",
    "Monopolar diathermy pencil": "모노폴라 전기소작 펜슬",
    "Fine dissecting scissors": "미세 박리 가위",
    "Langenbeck retractor": "랑겐벡 리트랙터",
    "Crile hemostat": "크라일 지혈겸자",
    "Penrose drain": "펜로즈 드레인",
    "Babcock forceps": "밥콕 포셉",
    "Polypropylene mesh": "폴리프로필렌 메쉬",
    "Needle driver": "니들 드라이버",
    "Mayo-Hegar needle holder": "마요-헤가 니들 홀더",
    "Yankauer suction": "양카우어 석션",
    "Poole suction tip": "풀 석션 팁",
}


def has_procedure_prompt(bundle_dir: str | Path) -> bool:
    bundle_path = Path(bundle_dir)
    return any((bundle_path / name).is_file() for name in PROMPT_FILE_NAMES)


def discover_prompt_bundle_dirs(spec_root: str | Path) -> list[Path]:
    root = Path(spec_root)
    if not root.is_dir():
        return []
    return [
        candidate
        for candidate in sorted(root.iterdir(), key=lambda path: path.name)
        if candidate.is_dir() and has_procedure_prompt(candidate)
    ]


def _slug_label(raw: str) -> str:
    return raw.replace("_", " ").replace("-", " ").strip().title()


def _phase_label_maps(
    prompt: dict[str, Any],
) -> tuple[OrderedDict[str, str], OrderedDict[str, str], list[str], set[str]]:
    labels = prompt.get("phase_labels", {})
    normal = labels.get("normal", {}) if isinstance(labels, dict) else {}
    interrupt = labels.get("interrupt", {}) if isinstance(labels, dict) else {}
    ko_labels = prompt.get("phase_labels_ko", {})
    normal_ko = ko_labels.get("normal", {}) if isinstance(ko_labels, dict) else {}
    interrupt_ko = ko_labels.get("interrupt", {}) if isinstance(ko_labels, dict) else {}
    phase_labels: OrderedDict[str, str] = OrderedDict()
    phase_labels_ko: OrderedDict[str, str] = OrderedDict()
    normal_ids: list[str] = []
    for source in (normal, interrupt):
        if not isinstance(source, dict):
            continue
        for phase_id, label in source.items():
            phase_labels[str(phase_id)] = str(label)
            if source is normal:
                normal_ids.append(str(phase_id))
    for source in (normal_ko, interrupt_ko):
        if not isinstance(source, dict):
            continue
        for phase_id, label in source.items():
            phase_labels_ko[str(phase_id)] = str(label)
    return (
        phase_labels,
        phase_labels_ko,
        normal_ids,
        {str(phase_id) for phase_id in interrupt} if isinstance(interrupt, dict) else set(),
    )


def _tool_ids_in_text(value: object, known_tools: set[str]) -> list[str]:
    if value is None:
        return []
    return [tool_id for tool_id in _TOOL_ID_RE.findall(str(value)) if tool_id in known_tools]


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _tool_category(name: str) -> str:
    lower = name.lower()
    if "suction" in lower:
        return "suction"
    if "retractor" in lower:
        return "exposure"
    if "cautery" in lower or "bovie" in lower or "bipolar" in lower or "diathermy" in lower:
        return "hemostasis"
    if "scalpel" in lower or "blade" in lower or "shear" in lower or "scissor" in lower:
        return "cutting"
    if "clamp" in lower or "mosquito" in lower or "hemostat" in lower:
        return "vessel_control"
    if "needle" in lower:
        return "closure"
    if "mesh" in lower:
        return "implant"
    if "forceps" in lower:
        return "grasping"
    return "instrument"


def _handover_profile(category: str, name: str) -> str:
    lower = name.lower()
    if category == "grasping" or "forceps" in lower:
        return "pinch_grasp"
    if category in {"cutting", "hemostasis", "suction"}:
        return "shaft_grasp"
    return "handle_grasp"


def _distinctive_name_aliases(name: str, generic_words: set[str]) -> list[str]:
    words = [
        word
        for word in re.findall(r"[a-z0-9가-힣]+", name.lower())
        if word and not word.isdigit() and word not in generic_words
    ]
    if not words:
        return []
    aliases = [" ".join(words)]
    aliases.extend(words)
    return aliases


def _tool_aliases(tool_id: str, name: str, localized_name: str = "") -> list[str]:
    aliases = [name, tool_id]
    lower = name.lower()
    aliases.append(lower)
    aliases.extend(part.strip() for part in re.split(r"[/(),#-]", lower) if part.strip())
    aliases.extend(_distinctive_name_aliases(lower, _GENERIC_TOOL_WORDS))
    if localized_name:
        localized_lower = localized_name.lower()
        aliases.append(localized_name)
        aliases.append(localized_lower)
        aliases.extend(
            _distinctive_name_aliases(localized_lower, _GENERIC_KO_TOOL_WORDS)
        )
    if "bovie" in lower or "monopolar" in lower:
        aliases.extend(["bovie", "보비"])
    if "bipolar" in lower:
        aliases.extend(["bipolar", "바이폴라"])
    if "mesh" in lower:
        aliases.extend(["mesh", "메쉬"])
    if "mosquito" in lower:
        aliases.extend(["mosquito", "모스키토"])
    if "harmonics" in lower or "shear" in lower:
        aliases.extend(["harmonics", "harmonic", "하모닉"])
    if "kocher" in lower or "middeldorpf" in lower or "thyroid retractor" in lower:
        aliases.extend(
            [
                "thyroid retractor",
                "middeldorpf retractor",
                "갑상선 리트랙터",
                "미들돌프 리트랙터",
            ]
        )
    return _ordered_unique([alias for alias in aliases if alias])


def _prompt_tools(prompt: dict[str, Any]) -> OrderedDict[str, str]:
    tools = prompt.get("tools", {})
    result: OrderedDict[str, str] = OrderedDict()
    if isinstance(tools, dict):
        for tool_id, name in tools.items():
            result[str(tool_id)] = str(name)
    return result


def _prompt_inventory(
    prompt: dict[str, Any],
    tool_ids: list[str],
) -> dict[str, int]:
    raw_inventory = prompt.get("tool_inventory", {})
    inventory: dict[str, int] = {}
    if raw_inventory not in ({}, None) and not isinstance(raw_inventory, dict):
        raise ValueError("procedure prompt tool_inventory must be a mapping.")
    for tool_id in tool_ids:
        raw_count = (
            raw_inventory.get(tool_id, 1)
            if isinstance(raw_inventory, dict)
            else 1
        )
        if isinstance(raw_count, bool):
            raise ValueError(
                f"procedure prompt tool_inventory.{tool_id} must be a positive integer."
            )
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"procedure prompt tool_inventory.{tool_id} must be a positive integer."
            ) from exc
        if count <= 0 or float(raw_count) != float(count):
            raise ValueError(
                f"procedure prompt tool_inventory.{tool_id} must be a positive integer."
            )
        inventory[tool_id] = count
    if isinstance(raw_inventory, dict):
        unknown = sorted(set(str(key) for key in raw_inventory) - set(tool_ids))
        if unknown:
            raise ValueError(
                "procedure prompt tool_inventory references unknown tools: "
                + ", ".join(unknown)
            )
    return inventory


def _phase_tools(prompt: dict[str, Any], known_tools: set[str]) -> dict[str, list[str]]:
    phase_tools: dict[str, list[str]] = {}
    phase_details = prompt.get("phase_details", {})
    if not isinstance(phase_details, dict):
        return phase_tools
    for phase_id, detail in phase_details.items():
        if not isinstance(detail, dict):
            continue
        tools: list[str] = []
        for cue in detail.get("visual_cues", []) or []:
            tools.extend(_tool_ids_in_text(cue, known_tools))
        for item in detail.get("expected_tool_sequence", []) or []:
            if not isinstance(item, dict):
                continue
            tools.extend(_tool_ids_in_text(item.get("current"), known_tools))
            tools.extend(_tool_ids_in_text(item.get("next"), known_tools))
            tools.extend(_tool_ids_in_text(item.get("cue"), known_tools))
        tool_roles = detail.get("tool_roles", {})
        if isinstance(tool_roles, dict):
            for role_tools in tool_roles.values():
                items = (
                    role_tools
                    if isinstance(role_tools, list)
                    else [role_tools]
                )
                for item in items:
                    tools.extend(_tool_ids_in_text(item, known_tools))
        phase_tools[str(phase_id)] = _ordered_unique(tools)
    return phase_tools


def _phase_field_deployed_tools(
    prompt: dict[str, Any],
    known_tools: set[str],
) -> dict[str, list[str]]:
    phase_tools: dict[str, list[str]] = {}
    phase_details = prompt.get("phase_details", {})
    if not isinstance(phase_details, dict):
        return phase_tools
    for phase_id, detail in phase_details.items():
        if not isinstance(detail, dict):
            continue
        tool_roles = detail.get("tool_roles", {})
        if not isinstance(tool_roles, dict):
            continue
        deployed: list[str] = []
        for raw_role, raw_tools in tool_roles.items():
            role = re.sub(
                r"[^a-z0-9]+",
                "_",
                str(raw_role).strip().lower(),
            ).strip("_")
            if role not in _FIELD_DEPLOYED_ROLE_NAMES:
                continue
            items = raw_tools if isinstance(raw_tools, list) else [raw_tools]
            for item in items:
                raw_tool = str(item or "").strip()
                if not raw_tool:
                    continue
                matched = _tool_ids_in_text(raw_tool, known_tools)
                deployed.extend(matched or [raw_tool])
        phase_tools[str(phase_id)] = _ordered_unique(deployed)
    return phase_tools


def _phase_next_map(prompt: dict[str, Any], phase_ids: list[str], interrupt_ids: set[str]) -> dict[str, list[str]]:
    phase_next: dict[str, list[str]] = {phase_id: [] for phase_id in phase_ids}
    phase_flow = prompt.get("phase_flow", {})
    if isinstance(phase_flow, dict):
        for transition in phase_flow.get("normal_sequence", []) or []:
            if not isinstance(transition, dict):
                continue
            source = str(transition.get("from", ""))
            target = str(transition.get("to", ""))
            if source in phase_next and target in phase_next:
                phase_next[source].append(target)
        interrupt = phase_flow.get("interrupt_transition", {}) or {}
        if isinstance(interrupt, dict):
            interrupt_phase = str(interrupt.get("phase", ""))
            if interrupt_phase in phase_next:
                interrupt_ids.add(interrupt_phase)
    for phase_id in phase_ids:
        if phase_id not in interrupt_ids:
            phase_next[phase_id].extend(sorted(interrupt_ids))
    return {phase_id: _ordered_unique(next_ids) for phase_id, next_ids in phase_next.items()}


def _build_scene_layout(
    tool_ids: list[str],
    initial_instrument_states: Any = None,
) -> dict[str, Any]:
    locations = [
        {"id": f"main_tray_slot_{index + 1}", "type": "tray_slot"}
        for index in range(len(tool_ids))
    ]
    locations.extend(
        [
            {"id": "mayo_recovery_zone", "type": "mayo_recovery_zone"},
            {"id": "mayo_reuse_zone", "type": "mayo_reuse_zone"},
            {"id": "field_region_procedure", "type": "surgical_field"},
            {"id": "surgeon_handover_zone", "type": "handover_zone"},
            {"id": "surgeon_return_zone", "type": "return_zone"},
            {"id": "robot_right_hand", "type": "robot_right_hand"},
            {"id": "robot_left_hand", "type": "robot_left_hand"},
            {"id": "cleaner_slot", "type": "cleaner_slot"},
            {"id": "surgeon_hand", "type": "surgeon_hand"},
        ]
    )
    return {
        "locations": locations,
        "initial_instrument_placement": [
            {"instrument_id": tool_id, "location_id": f"main_tray_slot_{index + 1}"}
            for index, tool_id in enumerate(tool_ids)
        ],
        "initial_instrument_states": (
            initial_instrument_states
            if isinstance(initial_instrument_states, list)
            else []
        ),
    }


def _slot_anchor(index: int, slot_count: int) -> dict[str, Any]:
    columns = 2
    rows = max(1, math.ceil(slot_count / columns))
    column = index % columns
    row = index // columns
    return {
        "id": f"main_tray_slot_{index + 1}",
        "attached_to": "instrument_rack",
        "x": 14.5 + column * 9.0,
        "y": 50.0 + row * (22.0 / max(rows - 1, 1)),
        "label": str(index + 1),
    }


def _build_simulation_layout(procedure_id: str, tool_ids: list[str]) -> dict[str, Any]:
    field_id = "field_region_procedure"
    return {
        "entities": [
            {"id": "humanoid_body", "type": "humanoid", "x": 36.5, "y": 34.0, "width": 17.0, "height": 32.0, "label": "Humanoid Assistant"},
            {"id": "surgeon_actor", "type": "surgeon", "x": 84.5, "y": 29.0, "width": 12.0, "height": 27.0, "label": "Surgeon"},
            {"id": f"{procedure_id}_bed", "type": "surgical_bed", "x": 58.0, "y": 32.5, "width": 24.0, "height": 28.0, "label": "OR Bed"},
            {"id": "instrument_rack", "type": "instrument_rack", "x": 9.5, "y": 43.5, "width": 23.0, "height": 32.0, "label": "Instrument Rack"},
            {"id": "mayo_stand", "type": "mayo_stand", "x": 55.5, "y": 62.5, "width": 29.0, "height": 8.5, "label": "Mayo Stand"},
            {"id": "cleaner_station", "type": "cleaner_station", "x": 13.0, "y": 14.0, "width": 11.5, "height": 11.5, "label": "Cleaner"},
            {"id": "unknown_zone", "type": "unknown_zone", "x": 88.0, "y": 62.0, "width": 10.0, "height": 8.0, "label": "Unknown"},
        ],
        "anchors": [
            {"id": "cleaner_slot", "attached_to": "cleaner_station", "x": 18.8, "y": 19.8, "label": "Cleaner Slot"},
            {"id": "robot_left_hand", "attached_to": "humanoid_body", "x": 37.5, "y": 46.0, "label": "Left Hand"},
            {"id": "robot_right_hand", "attached_to": "humanoid_body", "x": 53.5, "y": 41.5, "label": "Right Hand"},
            {"id": "surgeon_receive_zone", "attached_to": "surgeon_actor", "x": 79.8, "y": 43.5, "label": "Receive Zone"},
            {"id": "surgeon_return_zone", "attached_to": "surgeon_actor", "x": 78.5, "y": 53.0, "label": "Return Zone"},
            {"id": "surgeon_hand", "attached_to": "surgeon_actor", "x": 88.0, "y": 42.5, "label": "Surgeon Hand"},
            {"id": field_id, "attached_to": f"{procedure_id}_bed", "x": 66.2, "y": 44.8, "label": "Surgical Field"},
            {"id": "mayo_recovery_zone", "attached_to": "mayo_stand", "x": 61.5, "y": 66.5, "label": "Recovery Zone"},
            {"id": "mayo_reuse_zone", "attached_to": "mayo_stand", "x": 76.5, "y": 66.5, "label": "Reuse Zone"},
            {"id": "unknown_zone_anchor", "attached_to": "unknown_zone", "x": 93.0, "y": 66.0, "label": "Unknown"},
            *[_slot_anchor(index, len(tool_ids)) for index in range(len(tool_ids))],
        ],
    }


def _build_policy() -> dict[str, Any]:
    return {
        "phase_guard": {
            "min_confidence_to_keep": 0.55,
            "min_confidence_to_switch": 0.8,
            "smoothing_window": 5,
            "min_dwell_time_sec": 5.0,
            "allow_unknown_phase": True,
        },
        "action_guard": {
            "block_handover_when_phase_uncertain": True,
            "require_multi_evidence_for_handover": True,
            "allow_prepositioning_when_uncertain": False,
            "explicit_request_priority": True,
        },
        "humanoid_policy": {
            "handover_arm": "right",
            "recovery_arm": "left",
            "require_cleaning_after_surgeon_use": True,
            "allow_anticipatory_hold": True,
            "voice_override_preempts_preposition": True,
            "direct_return_to_rack_for_unused_prepositioned_tool": True,
        },
    }


def _build_mock_surgeon(phase_tools: dict[str, list[str]], tools: OrderedDict[str, str]) -> dict[str, Any]:
    stages: list[dict[str, Any]] = []
    for phase_id, tool_ids in phase_tools.items():
        for index, tool_id in enumerate(tool_ids):
            tool_name = tools.get(tool_id, tool_id)
            stages.append(
                {
                    "name": f"{phase_id.lower()}_{index + 1}_{tool_id.lower()}",
                    "phase_id": phase_id,
                    "duration_ticks": 3,
                    "event_type": "voice_request",
                    "intent": "handover",
                    "requested_tool": tool_id,
                    "voice_text": f"{tool_name} please",
                    "ready_for_handover": True,
                    "ready_for_retrieval": False,
                    "scene_note": f"Surgeon requests {tool_name} during {phase_id}.",
                }
            )
    return {"period_sec": 1.0, "stages": stages}


def _build_mock_perception(phase_tools: dict[str, list[str]], tool_ids: list[str]) -> dict[str, Any]:
    first_phase = next(iter(phase_tools), "")
    home_observations = [
        {
            "instrument_id": tool_id,
            "location_id": f"main_tray_slot_{index + 1}",
            "location_type": "tray_slot",
            "confidence": 0.98,
            "visible": True,
        }
        for index, tool_id in enumerate(tool_ids)
    ]
    stages: list[dict[str, Any]] = [
        {
            "name": "bootstrap_home",
            "duration_ticks": 2,
            "phase_hypotheses": [{"phase_id": first_phase, "confidence": 0.72}] if first_phase else [],
            "observations": home_observations,
            "scene_summary": "All procedure-prompt instruments are visible at their home tray slots.",
            "uncertainty": 0.1,
        }
    ]
    for phase_id, expected_tools in phase_tools.items():
        request_tool = expected_tools[0] if expected_tools else ""
        stages.append(
            {
                "name": f"{phase_id.lower()}_evidence",
                "duration_ticks": 5,
                "phase_hypotheses": [{"phase_id": phase_id, "confidence": 0.86}],
                "observations": home_observations,
                "surgeon_gesture": {
                    "event_type": "request_tool",
                    "requested_tool": request_tool,
                    "hand_pose": "open_palm" if request_tool else "",
                    "confidence": 0.75 if request_tool else 0.0,
                    "note": f"Prompt-derived cue for {phase_id}.",
                }
                if request_tool
                else None,
                "scene_summary": f"Prompt-derived VLM evidence for {phase_id}.",
                "uncertainty": 0.18,
                "explicit_request": request_tool,
            }
        )
    return {"period_sec": 1.0, "stages": stages}


def build_raw_bundle_from_prompt(bundle_dir: str | Path, display_catalog: dict[str, dict]) -> dict[str, Any]:
    bundle_path = Path(bundle_dir)
    prompt = load_procedure_prompt(bundle_path)
    if not prompt:
        raise ValueError(f"{bundle_path} does not contain a procedure prompt YAML.")

    procedure_payload = prompt.get("procedure", {}) if isinstance(prompt.get("procedure"), dict) else {}
    procedure_id = str(procedure_payload.get("id") or bundle_path.name)
    procedure_name = str(procedure_payload.get("name") or _slug_label(procedure_id))
    procedure_name_ko = str(procedure_payload.get("ko") or procedure_name)

    tools = _prompt_tools(prompt)
    if not tools:
        raise ValueError(f"{bundle_path} procedure prompt must define tools.")
    tool_ids = list(tools.keys())
    tool_inventory = _prompt_inventory(prompt, tool_ids)
    known_tools = set(tool_ids)
    phase_labels, phase_labels_ko, normal_phase_ids, interrupt_ids = _phase_label_maps(prompt)
    if not phase_labels:
        raise ValueError(f"{bundle_path} procedure prompt must define phase_labels.")
    phase_ids = list(phase_labels.keys())
    phase_tools = _phase_tools(prompt, known_tools)
    phase_field_deployed_tools = _phase_field_deployed_tools(
        prompt,
        known_tools,
    )
    phase_next = _phase_next_map(prompt, phase_ids, interrupt_ids)

    return {
        "procedure": {
            "procedure_id": procedure_id,
            "procedure_display_name": procedure_name,
            "procedure_display_name_ko": procedure_name_ko,
            "default_phase_id": str(procedure_payload.get("default_phase_id", "")),
            "normal_phase_ids": normal_phase_ids,
            "interrupt_phase_ids": sorted(interrupt_ids),
            "phases": [
                {
                    "id": phase_id,
                    "display_name": phase_labels[phase_id],
                    "display_name_ko": phase_labels_ko.get(phase_id, phase_labels[phase_id]),
                    "possible_next": phase_next.get(phase_id, []),
                    "expected_instruments": phase_tools.get(phase_id, []),
                    "field_deployed_instruments": (
                        phase_field_deployed_tools.get(phase_id, [])
                    ),
                    "min_duration_sec": 2.0 if phase_id in interrupt_ids else 5.0,
                }
                for phase_id in phase_ids
            ],
        },
        "instruments": {
            "instruments": [
                {
                    "id": tool_id,
                    "display_name": tool_name,
                    "display_name_ko": _KO_TOOL_NAMES.get(tool_name, tool_name),
                    "aliases": _tool_aliases(
                        tool_id,
                        tool_name,
                        _KO_TOOL_NAMES.get(tool_name, tool_name),
                    ),
                    "category": _tool_category(tool_name),
                    "inventory_count": tool_inventory[tool_id],
                    "requestable": True,
                    "role": _tool_category(tool_name),
                    "handover_profile": _handover_profile(_tool_category(tool_name), tool_name),
                }
                for tool_id, tool_name in tools.items()
            ]
        },
        "scene_layout": _build_scene_layout(
            tool_ids,
            prompt.get("initial_instrument_states", []),
        ),
        "policy": _build_policy(),
        "simulation_layout": _build_simulation_layout(procedure_id, tool_ids),
        "mock_surgeon": _build_mock_surgeon(phase_tools, tools),
        "mock_perception": _build_mock_perception(phase_tools, tool_ids),
        "bed_robot_arm_groups": prompt.get("bed_robot_arm_groups", {}),
        "display_catalog": display_catalog,
    }
