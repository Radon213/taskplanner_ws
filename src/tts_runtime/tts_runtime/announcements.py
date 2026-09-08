"""Deterministic Korean speech for execution-owned admission facts.

This module is presentation-only.  It turns the one bounded execution fact
emitted after a target Action/Service acknowledges a command into a durable
audio request; it never admits, routes, or invokes robot control.  VLM
dialogue remains on the separate ``HumanoidReply`` lane.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import hashlib
import json
import math
import re
import threading
import time
import unicodedata
from typing import Iterable, Mapping

from .core import (
    COMPLETION_CLEANUP_REPLY_ID_PREFIX,
    ReplyRequest,
    TTS_PRIORITY_AUTONOMOUS,
    TTS_PRIORITY_NORMAL,
    TTS_PRIORITY_SURGEON,
)


_SCHEMA = "taskplanner.tts.execution-announcement.v1"
EXECUTION_ANNOUNCEMENT_SCHEMA = "taskplanner.execution_announcement.v1"
EXECUTION_ANNOUNCEMENT_TOPIC = "/taskplanner/execution/announcement"
EXECUTION_ANNOUNCEMENT_MAX_CHARS = 8 * 1024
_MAX_FACTS = 512
_EXPLICIT_RETRIEVAL_BIND_WINDOW_SEC = 10.0
_TOOL_ROUTE = "tool_transfer"
_RETRACTION_ROUTE = "retraction"
# A local ``send_goal_async``/``call_async`` return only proves that the local
# client accepted work.  It must not produce operator audio: the action goal
# can still be rejected and a service response can still fail.  The bridge
# emits ``accepted`` only after the selected endpoint has acknowledged the
# request, which is the first evidence strong enough for an announcement.
_ACCEPTED_STAGE = "accepted"
_TOOL_ACTION_TRANSPORT = "action"
_RETRACTION_SERVICE_TRANSPORT = "service"
_TOOL_ACTION_RECEIPT_EVIDENCE = "goal_response"
_RETRACTION_SERVICE_RECEIPT_EVIDENCE = "service_admission_only"
_LIFECYCLE_SCHEMA = "taskplanner.tts.lifecycle-announcement.v1"
SURGERY_RECORD_STATUS_SCHEMA = "taskplanner.operational_surgery_record.status.v1"
SURGERY_RECORD_STATUS_MAX_CHARS = 64 * 1024

PROCEDURE_START_TEXT = "수술을 시작합니다."
PROCEDURE_FINISHING_TEXT = "기구를 정리한 후 수술을 마무리하겠습니다."
PROCEDURE_STOP_TEXT = "수술을 종료합니다."
SURGERY_RECORD_COMPLETED_TEXT = "수술기록지를 생성하였습니다."

_LIFECYCLE_TEXT_BY_EVENT: Mapping[str, str] = {
    "procedure_start": PROCEDURE_START_TEXT,
    "procedure_finishing": PROCEDURE_FINISHING_TEXT,
    "procedure_stop": PROCEDURE_STOP_TEXT,
    "surgery_record_completed": SURGERY_RECORD_COMPLETED_TEXT,
}

# SimulationManager publishes this small, internal event immediately after a
# valid voice ``finish`` request enters its finishing state.  It is separate
# from the terminal receipt because the latter is intentionally emitted only
# after cleanup and executor settlement.
PROCEDURE_LIFECYCLE_EVENT_SCHEMA = "taskplanner.simulation.lifecycle_event.v1"
PROCEDURE_LIFECYCLE_EVENT_TOPIC = "/simulation/lifecycle_event"


# These are intentionally short spoken names, rather than the longer public
# controller labels.  ``tool_aliases`` is a TTS-owner parameter so a scenario
# can add a local alias with a TTS-only restart instead of changing the planner.
DEFAULT_TOOL_ALIASES: tuple[str, ...] = (
    "T01=앨리스",
    "T02=애드슨",
    "T03=메첸바움",
    "T04=보비",
    "T05=아미",
    "T06=센",
    "T07=바이폴라",
    "T08=모스키토",
    "T09=하모닉",
    "T10=석션",
    "adson=애드슨",
    "adson forceps=애드슨",
    "bovie=보비",
    "bovie surgical cautery=보비",
    "bipolar=바이폴라",
    "bipolar cautery=바이폴라",
    "bipolar forceps=바이폴라",
    "mosquito=모스키토",
    "mosquito forceps=모스키토",
)


# Action names are execution-owned values.  This small map only assigns
# operator wording to an already-sent action; it has no endpoint or admission
# information and cannot make a string executable.
_TOOL_ACTION_KINDS: Mapping[str, str] = {
    "pick_up_and_handover": "handover",
    "pick_up_from_mayo_and_handover": "handover",
    "direct_handover": "handover",
    "tool_handover": "handover",
    "predicted_tool_handover": "handover",
    "put_down_and_handover": "handover",
    "replace_and_handover": "handover",
    "predict_tool": "prepare",
    "prepare_tool": "prepare",
    "tool_predict": "prepare",
    # Only the normal Mayo -> recovery route is spoken as a recovery.  A
    # prepared tool being parked to free the robot hand is *not* a recovery,
    # nor are legacy/manual aliases.  This keeps "회수하겠습니다" reserved for
    # the autonomous Mayo-recovery policy the operator asked to hear.
    "retrieve_from_mayo": "retrieve",
}

# The behavior tree uses this Action to park a previously predicted tool before
# it can service a newer explicit request.  It is intentionally absent from
# ``_TOOL_ACTION_KINDS``: parking a tool is not a recovery announcement.  A
# very narrow, voice-correlated exception is handled below once that Action is
# actually accepted by its endpoint.
_RETURN_UNUSED_PREPOSITION_ACTION = "return_unused_preposition"


def _tts_priority_for_tool(*, action: object, voice_backed: bool) -> int:
    """Keep operator-originated tool feedback ahead of autonomous narration."""

    if voice_backed:
        return TTS_PRIORITY_SURGEON
    if str(action or "").strip() in {
        "predict_tool",
        "prepare_tool",
        "tool_predict",
        "retrieve_from_mayo",
    }:
        return TTS_PRIORITY_AUTONOMOUS
    return TTS_PRIORITY_NORMAL

_RETRACTION_TEXT_BY_COMMAND: Mapping[int, str] = {
    1: "직접교시를 시작합니다.",
    2: "직접교시를 종료합니다.",
    3: "리트랙션을 시작합니다.",
    5: "도구를 교체합니다.",
    6: "리트랙션을 종료합니다.",
    7: "석션 들어가겠습니다.",
    8: "석션 빼겠습니다.",
}

_RETRACTION_TARGET_SIDE_TEXT: Mapping[object, str] = {
    0: "",
    1: "왼쪽으로",
    2: "오른쪽으로",
    3: "양쪽으로",
    "": "",
    "none": "",
    "left": "왼쪽으로",
    "right": "오른쪽으로",
    "both": "양쪽으로",
}


def retraction_adjustment_text(
    target_side: object,
    distance_m: object,
) -> str:
    """Render one accepted adjustment with its actual side and signed delta.

    The public Service carries a signed distance: positive means more pull and
    negative means less pull.  ``target_side=none`` intentionally omits the
    directional prefix instead of guessing one for a bilateral/unspecified
    request.
    """

    try:
        distance = float(distance_m)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(distance) or distance == 0.0:
        return ""

    side_key: object = target_side
    if isinstance(target_side, bool):
        return ""
    if isinstance(target_side, str):
        side_key = target_side.strip().casefold()
        try:
            side_key = int(side_key)
        except ValueError:
            pass
    side_text = _RETRACTION_TARGET_SIDE_TEXT.get(side_key)
    if side_text is None:
        return ""

    centimeters = abs(distance) * 100.0
    if not math.isfinite(centimeters) or centimeters <= 0.0:
        return ""
    amount = f"{centimeters:.3f}".rstrip("0").rstrip(".")
    degree = "더" if distance > 0.0 else "덜"
    prefix = f"{side_text} " if side_text else ""
    return f"{prefix}{amount} 센티 {degree} 당기겠습니다."


@dataclass(frozen=True, slots=True)
class _GatewayScope:
    gateway_instance_id: str
    procedure_run_id: str
    procedure_active: bool

    @property
    def active(self) -> bool:
        return bool(
            self.procedure_active
            and self.gateway_instance_id
            and self.procedure_run_id
        )


@dataclass(frozen=True, slots=True)
class _ToolCommand:
    command_id: str
    action: str
    instrument_id: str
    request_generation: int
    voice_backed: bool
    announcement_suppressed: bool = False
    replacement_handover_signature: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class _TypedVoiceToolIntent:
    """One resolver-owned, directly observed spoken handover proposal."""

    utterance_id: str
    signature: tuple[str, str]
    request_generation: int = 0


@dataclass(frozen=True, slots=True)
class SurgeryRecordCompletion:
    """Minimal confirmed-success projection consumed by the TTS owner."""

    request_id: str
    procedure_run_id: str


@dataclass(frozen=True, slots=True)
class ProcedureLifecycleEvent:
    """One manager-owned lifecycle edge before terminal settlement."""

    event: str
    procedure_run_id: str


def parse_procedure_lifecycle_event(payload: object) -> ProcedureLifecycleEvent | None:
    """Parse the bounded manager event used for early finish TTS."""

    raw = str(payload or "")
    if not raw or len(raw) > 1024:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema") != PROCEDURE_LIFECYCLE_EVENT_SCHEMA:
        return None
    event = str(value.get("event") or "").strip()
    procedure_run_id = str(value.get("procedure_run_id") or "").strip()
    if event != "procedure_finishing" or not procedure_run_id or len(procedure_run_id) > 64:
        return None
    return ProcedureLifecycleEvent(event=event, procedure_run_id=procedure_run_id)


@dataclass(frozen=True, slots=True)
class ExecutionAnnouncementFact:
    """One execution-owned, endpoint-admitted speech fact.

    It contains no endpoint, free text, controller receipt, or command
    argument channel.  The bridge has already done admission; this only lets
    the TTS owner choose the fixed Korean presentation string once.
    """

    command_id: str
    procedure_run_id: str
    route: str
    action: str = ""
    instrument_id: str = ""
    request_generation: int = 0
    voice_backed: bool = False
    retraction_command: int = 0
    retraction_target_side: int = 0
    retraction_distance_m: float = 0.0
    # A narrow execution-owned correlation key may make two accepted legs of
    # one voice replacement share one durable spoken acknowledgement.
    announcement_key: str = ""
    # Set only by execution after a same-run cleanup Action is accepted while
    # the authoritative procedure state is finishing.
    completion_cleanup: bool = False


def parse_execution_announcement_fact(
    payload: object,
) -> ExecutionAnnouncementFact | None:
    """Parse only the bounded private execution announcement schema."""

    raw = str(payload or "")
    if not raw or len(raw) > EXECUTION_ANNOUNCEMENT_MAX_CHARS:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema") != EXECUTION_ANNOUNCEMENT_SCHEMA:
        return None
    command_id = str(value.get("command_id") or "").strip()
    procedure_run_id = str(value.get("procedure_run_id") or "").strip()
    route = str(value.get("route") or "").strip()
    completion_cleanup = value.get("completion_cleanup", False)
    if (
        not command_id
        or len(command_id) > 128
        or not procedure_run_id
        or len(procedure_run_id) > 64
        or route not in {_TOOL_ROUTE, _RETRACTION_ROUTE}
        or not isinstance(completion_cleanup, bool)
        or (route != _TOOL_ROUTE and completion_cleanup)
    ):
        return None
    if route == _TOOL_ROUTE:
        action = str(value.get("action") or "").strip()
        instrument_id = str(value.get("instrument_id") or "").strip()
        announcement_key = str(value.get("announcement_key") or "").strip()
        raw_generation = value.get("request_generation", 0)
        if isinstance(raw_generation, bool):
            return None
        try:
            request_generation = int(raw_generation)
        except (TypeError, ValueError):
            return None
        if (
            not action
            or len(action) > 96
            or len(instrument_id) > 128
            or len(announcement_key) > 128
            or (
                announcement_key
                and re.fullmatch(r"[A-Za-z0-9_.:-]+", announcement_key) is None
            )
            or request_generation < 0
            or not isinstance(value.get("voice_backed", False), bool)
            or (
                completion_cleanup
                and (
                    action != "retrieve_from_mayo"
                    or bool(value.get("voice_backed", False))
                )
            )
        ):
            return None
        return ExecutionAnnouncementFact(
            command_id=command_id,
            procedure_run_id=procedure_run_id,
            route=route,
            action=action,
            instrument_id=instrument_id,
            request_generation=request_generation,
            voice_backed=bool(value.get("voice_backed", False)),
            announcement_key=announcement_key,
            completion_cleanup=completion_cleanup,
        )
    raw_command = value.get("retraction_command")
    if isinstance(raw_command, bool):
        return None
    try:
        retraction_command = int(raw_command)
    except (TypeError, ValueError):
        return None
    if retraction_command < 0 or retraction_command > 255:
        return None
    raw_target_side = value.get("target_side", 0)
    if isinstance(raw_target_side, bool):
        return None
    if isinstance(raw_target_side, str):
        normalized_target_side = raw_target_side.strip().casefold()
        retraction_target_side = {
            "none": 0,
            "left": 1,
            "right": 2,
            "both": 3,
        }.get(normalized_target_side)
        if retraction_target_side is None:
            try:
                retraction_target_side = int(normalized_target_side)
            except (TypeError, ValueError):
                return None
    else:
        try:
            retraction_target_side = int(raw_target_side)
        except (TypeError, ValueError):
            return None
    if retraction_target_side < 0 or retraction_target_side > 3:
        return None
    raw_distance_m = value.get("distance_m", 0.0)
    if isinstance(raw_distance_m, bool):
        return None
    try:
        retraction_distance_m = float(raw_distance_m)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(retraction_distance_m):
        return None
    return ExecutionAnnouncementFact(
        command_id=command_id,
        procedure_run_id=procedure_run_id,
        route=route,
        retraction_command=retraction_command,
        retraction_target_side=retraction_target_side,
        retraction_distance_m=retraction_distance_m,
    )


def execution_announcement_request(
    fact: ExecutionAnnouncementFact,
    *,
    gateway_instance_id: object,
    procedure_run_id: object,
    aliases: Mapping[str, str],
) -> ReplyRequest | None:
    """Build one durable fixed-phrase request from an admitted fact."""

    gateway = str(gateway_instance_id or "").strip()
    active_run_id = str(procedure_run_id or "").strip()
    if (
        not gateway
        or not active_run_id
        or fact.procedure_run_id != active_run_id
    ):
        return None
    if fact.route == _TOOL_ROUTE:
        text = tool_action_text(fact.action, fact.instrument_id, aliases)
    elif fact.route == _RETRACTION_ROUTE:
        if fact.retraction_command == 4:
            text = retraction_adjustment_text(
                fact.retraction_target_side,
                fact.retraction_distance_m,
            )
        else:
            text = _RETRACTION_TEXT_BY_COMMAND.get(fact.retraction_command, "")
    else:  # defensive: parser already constrains route
        return None
    if not text:
        return None
    priority = (
        TTS_PRIORITY_SURGEON
        if fact.route == _RETRACTION_ROUTE or fact.voice_backed
        else _tts_priority_for_tool(action=fact.action, voice_backed=False)
    )
    material = {
        "action": fact.action,
        "command_id": fact.announcement_key or fact.command_id,
        "gateway_instance_id": gateway,
        "procedure_run_id": active_run_id,
        "request_generation": fact.request_generation,
        "retraction_command": fact.retraction_command,
        "retraction_target_side": fact.retraction_target_side,
        "retraction_distance_m": fact.retraction_distance_m,
        "route": fact.route,
        "schema": EXECUTION_ANNOUNCEMENT_SCHEMA,
    }
    if fact.completion_cleanup:
        material["completion_cleanup"] = True
    if fact.announcement_key:
        # Preserve the prior durable identities for all ordinary facts. Only
        # the two controller legs intentionally sharing a key also carry the
        # requested tool in their presentation identity.
        material["announcement_key"] = fact.announcement_key
        material["instrument_id"] = fact.instrument_id
    digest = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    reply_id_prefix = (
        COMPLETION_CLEANUP_REPLY_ID_PREFIX
        if fact.completion_cleanup
        else "execution-announcement:"
    )
    return ReplyRequest(
        reply_id=f"{reply_id_prefix}{digest}",
        turn_id=f"execution-turn:{digest}",
        utterance_id=f"execution-utterance:{digest}",
        gateway_instance_id=gateway,
        procedure_run_id=active_run_id,
        text=text,
        timing="immediate",
        priority=priority,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalized_key(value: object) -> str:
    """Normalize an instrument ID/name only for local alias lookup."""

    text = unicodedata.normalize("NFC", str(value or "")).strip()
    text = text.split("#", 1)[0]
    return re.sub(r"[^0-9a-z가-힣]+", "", text.casefold())


def parse_tool_aliases(entries: Iterable[object]) -> dict[str, str]:
    """Parse ``instrument=spoken Korean name`` entries for the TTS owner."""

    aliases: dict[str, str] = {}
    for raw in entries:
        entry = str(raw or "").strip()
        instrument, separator, spoken = entry.partition("=")
        key = _normalized_key(instrument)
        value = unicodedata.normalize("NFC", spoken).strip()
        if not separator or not key or not value:
            raise ValueError(
                "tool_aliases must use non-empty 'instrument=spoken_name' entries"
            )
        aliases[key] = value
    return aliases


def short_tool_name(instrument_id: object, aliases: Mapping[str, str]) -> str:
    """Return a concise Korean name without exposing controller IDs to speech."""

    key = _normalized_key(instrument_id)
    resolved = str(aliases.get(key, "")).strip()
    if resolved:
        return resolved
    original = unicodedata.normalize("NFC", str(instrument_id or "")).strip()
    # Keep a human-authored Korean name when a scenario has already provided
    # one, but never pronounce an opaque controller ID such as ``T04``.
    if original and any("가" <= character <= "힣" for character in original):
        return original.split("#", 1)[0].strip()
    return "도구"


def _object_particle(word: str) -> str:
    """Return the Korean object particle for one already-short spoken noun."""

    normalized = unicodedata.normalize("NFC", word).strip()
    if not normalized:
        return "를"
    final = normalized[-1]
    if "가" <= final <= "힣":
        return "을" if (ord(final) - ord("가")) % 28 else "를"
    return "을"


def tool_action_text(action: object, instrument_id: object, aliases: Mapping[str, str]) -> str:
    """Format the reviewed Korean sentence for one known tool Action."""

    kind = _TOOL_ACTION_KINDS.get(str(action or "").strip())
    if kind is None:
        return ""
    tool = short_tool_name(instrument_id, aliases)
    particle = _object_particle(tool)
    suffix = {
        "handover": "전달드리겠습니다.",
        "prepare": "준비하겠습니다.",
        "retrieve": "회수하겠습니다.",
    }[kind]
    return f"{tool}{particle} {suffix}"


def lifecycle_announcement_request(
    *,
    event: str,
    gateway_instance_id: object,
    procedure_run_id: object,
    correlation_id: object = "",
) -> ReplyRequest | None:
    """Build one deterministic lifecycle announcement for an existing run.

    This helper is presentation-only.  It cannot start or stop a scenario and
    it does not interpret free text.  Stable IDs let the durable playback
    store suppress repeated status heartbeats and process restarts.
    """

    normalized_event = str(event or "").strip()
    if normalized_event not in _LIFECYCLE_TEXT_BY_EVENT:
        raise ValueError(f"unsupported lifecycle announcement: {normalized_event}")
    gateway = str(gateway_instance_id or "").strip()
    run_id = str(procedure_run_id or "").strip()
    if not gateway or not run_id:
        return None
    material = {
        "correlation_id": str(correlation_id or "").strip(),
        "event": normalized_event,
        "gateway_instance_id": gateway,
        "procedure_run_id": run_id,
        "schema": _LIFECYCLE_SCHEMA,
    }
    digest = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    return ReplyRequest(
        reply_id=f"lifecycle-announcement:{digest}",
        turn_id=f"lifecycle-turn:{digest}",
        utterance_id=f"lifecycle-utterance:{digest}",
        gateway_instance_id=gateway,
        procedure_run_id=run_id,
        text=_LIFECYCLE_TEXT_BY_EVENT[normalized_event],
        timing="immediate",
        priority=(
            TTS_PRIORITY_SURGEON
            if normalized_event in {"procedure_start", "procedure_finishing", "procedure_stop"}
            else TTS_PRIORITY_NORMAL
        ),
    )


def parse_successful_surgery_record_status(
    payload: object,
) -> SurgeryRecordCompletion | None:
    """Return only a bounded, confirmed surgery-record POST completion."""

    raw = str(payload or "")
    if not raw or len(raw) > SURGERY_RECORD_STATUS_MAX_CHARS:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    if (
        value.get("schema") != SURGERY_RECORD_STATUS_SCHEMA
        or value.get("submit_state") != "SUCCEEDED"
        or value.get("success") is not True
    ):
        return None
    request_id = str(value.get("request_id") or "").strip()
    procedure_run_id = str(value.get("procedure_run_id") or "").strip()
    completed_at = str(value.get("completed_at") or "").strip()
    if not request_id or not procedure_run_id or not completed_at:
        return None
    return SurgeryRecordCompletion(
        request_id=request_id,
        procedure_run_id=procedure_run_id,
    )


def _tool_reply_signature(
    text: object,
    aliases: Mapping[str, str],
) -> tuple[str, str] | None:
    """Return a compact action/tool key for a spoken execution acknowledgement.

    This is deliberately presentation-only.  It recognizes the three fixed
    Korean announcement verbs and one configured instrument alias so the TTS
    owner can avoid speaking the VLM's duplicate acknowledgement for the same
    already-admitted voice command.  It neither parses a command nor grants
    any execution authority to text.
    """

    normalized = _normalized_key(text)
    if not normalized:
        return None
    action_kind = next(
        (
            kind
            for marker, kind in (
                ("전달", "handover"),
                ("준비", "prepare"),
                ("회수", "retrieve"),
            )
            if marker in normalized
        ),
        "",
    )
    if not action_kind:
        return None

    # Match the longest configured identifier/spoken alias first.  A VLM may
    # use either ``Mosquito`` or ``모스키토`` while the deterministic owner
    # always speaks the latter, so both must collapse to one signature.
    candidates: dict[str, str] = {}
    for identifier, spoken in aliases.items():
        canonical = _normalized_key(spoken)
        identifier_key = _normalized_key(identifier)
        if canonical:
            candidates[canonical] = canonical
            if identifier_key:
                candidates[identifier_key] = canonical
    for candidate, canonical in sorted(
        candidates.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if candidate and candidate in normalized:
            return (action_kind, canonical)
    return None


def _tool_command_signature(
    command: _ToolCommand,
    aliases: Mapping[str, str],
) -> tuple[str, str] | None:
    return _tool_signature_from_action_and_instrument(
        command.action,
        command.instrument_id,
        aliases,
    )


def _tool_signature_from_action_and_instrument(
    action: object,
    instrument_id: object,
    aliases: Mapping[str, str],
) -> tuple[str, str] | None:
    action_kind = _TOOL_ACTION_KINDS.get(str(action or "").strip())
    if action_kind is None:
        return None
    instrument_key = _normalized_key(instrument_id)
    spoken = aliases.get(instrument_key, instrument_id)
    spoken_key = _normalized_key(spoken)
    if not spoken_key:
        return None
    return (action_kind, spoken_key)


def default_prewarm_texts(
    tool_aliases: Iterable[object] = DEFAULT_TOOL_ALIASES,
) -> list[str]:
    """Return likely first-use phrases that should be cache-ready in Live.

    The catalog intentionally includes the complete default tool set, not only
    the instruments observed in one run.  Retraction adjustments are rendered
    from the same formatter used for accepted service facts, so a commonly
    requested centimetre/side combination can take the precomputed path too.
    Any text outside this bounded catalog still uses the normal immediate
    synthesis fallback.
    """

    aliases = parse_tool_aliases(tool_aliases)
    # Keep the catalog stable across scenarios while covering every default
    # controller tool.  Custom aliases remain supported by the normal fallback
    # path and can be added to this list explicitly in a scenario if needed.
    tool_ids = tuple(f"T{index:02d}" for index in range(1, 11))
    texts = [
        tool_action_text(action, tool, aliases)
        for tool in tool_ids
        for action in ("pick_up_and_handover", "predict_tool", "retrieve_from_mayo")
    ]
    texts.extend(_RETRACTION_TEXT_BY_COMMAND.values())
    # Kept for replay compatibility with the 2026-08-31 run, whose accepted
    # adjustment facts used this fixed wording before direction/centimetre
    # values were exposed in the announcement contract.
    texts.append("리트랙션을 조정합니다.")
    # These are the small, repeated adjustments seen in field usage.  Include
    # both signed directions and the bilateral form; the 10 센티 case preserves
    # the larger correction observed in the 2026-08-31 records.  The exact
    # service formatter is used so cache lookup and live announcements match.
    for target_side, distance_m in (
        ("none", 0.01),
        ("none", -0.01),
        ("none", 0.02),
        ("none", -0.02),
        ("left", 0.01),
        ("left", -0.01),
        ("left", 0.02),
        ("left", -0.02),
        ("right", 0.01),
        ("right", -0.01),
        ("right", 0.02),
        ("right", -0.02),
        ("right", 0.10),
        ("both", 0.01),
        ("both", -0.01),
    ):
        texts.append(retraction_adjustment_text(target_side, distance_m))
    texts.extend(_LIFECYCLE_TEXT_BY_EVENT.values())
    return list(dict.fromkeys(text for text in texts if text))


class ExecutionAnnouncementResolver:
    """Join sent execution evidence to fixed Korean TTS requests.

    The resolver keeps only bounded, in-process association facts.  The
    ``PlaybackStore`` remains the durable, final replay/deduplication owner.
    """

    def __init__(
        self,
        *,
        tool_aliases: Iterable[object] = DEFAULT_TOOL_ALIASES,
        max_facts: int = _MAX_FACTS,
    ) -> None:
        self._aliases = parse_tool_aliases(tool_aliases)
        self._max_facts = max(32, int(max_facts))
        self._lock = threading.RLock()
        self._scope = _GatewayScope("", "", False)
        self._tool_commands: OrderedDict[str, _ToolCommand] = OrderedDict()
        self._voice_generations: OrderedDict[str, int] = OrderedDict()
        self._typed_voice_tool_intents: OrderedDict[
            str, _TypedVoiceToolIntent
        ] = OrderedDict()
        self._typed_voice_command_utterances: OrderedDict[str, str] = OrderedDict()
        self._replacement_handover_signatures: OrderedDict[
            int, tuple[str, str]
        ] = OrderedDict()
        self._explicit_retrieval_tools: OrderedDict[str, float] = OrderedDict()
        self._accepted_tool_commands: OrderedDict[str, None] = OrderedDict()
        self._announced: OrderedDict[str, None] = OrderedDict()

    @property
    def aliases(self) -> Mapping[str, str]:
        return dict(self._aliases)

    def observe_gateway_info(
        self,
        *,
        gateway_instance_id: object,
        procedure_run_id: object,
        procedure_active: object,
    ) -> None:
        candidate = _GatewayScope(
            str(gateway_instance_id or "").strip(),
            str(procedure_run_id or "").strip(),
            bool(procedure_active),
        )
        with self._lock:
            if candidate == self._scope:
                return
            self._scope = candidate
            self._tool_commands.clear()
            self._voice_generations.clear()
            self._typed_voice_tool_intents.clear()
            self._typed_voice_command_utterances.clear()
            self._replacement_handover_signatures.clear()
            self._explicit_retrieval_tools.clear()
            self._accepted_tool_commands.clear()
            self._announced.clear()

    def observe_voice_intent(
        self,
        *,
        utterance_id: object,
        request_generation: object,
        accepted: object,
    ) -> tuple[ReplyRequest, ...]:
        """Remember an accepted typed voice turn and join pending parking.

        The TwinEvent and the typed ``VoiceCommandIntent`` are independent DDS
        streams, so either may arrive after the return Action has already been
        accepted.  Returning any newly eligible presentation request here
        keeps that ordering race from losing the deterministic acknowledgement.
        """

        normalized_utterance_id = str(utterance_id or "").strip()
        if isinstance(request_generation, bool):
            return ()
        try:
            generation = int(request_generation)
        except (TypeError, ValueError):
            return ()
        if not accepted or not normalized_utterance_id or generation <= 0:
            return ()
        with self._lock:
            if not self._scope.active:
                return ()
            self._put_bounded(
                self._voice_generations,
                normalized_utterance_id,
                generation,
            )
            proposal = self._typed_voice_tool_intents.get(normalized_utterance_id)
            if proposal is not None and proposal.request_generation != generation:
                self._typed_voice_tool_intents[normalized_utterance_id] = replace(
                    proposal,
                    request_generation=generation,
                )
            return self._link_pending_explicit_replacement_returns_locked()

    def observe_typed_voice_tool_intent(
        self,
        *,
        utterance_id: object,
        intent: object,
        tool_id: object,
        accepted: object,
    ) -> tuple[ReplyRequest, ...]:
        """Observe the resolver's current typed proposal for presentation.

        The former TwinEvent relay is no longer produced after the ownership
        split, so the TTS observer must read the resolver's already typed
        proposal directly.  This remains presentation-only: it only suppresses
        a matching VLM success phrase until the execution endpoint receipt
        releases the deterministic announcement.
        """

        normalized_utterance_id = str(utterance_id or "").strip()
        normalized_intent = str(intent or "").strip()
        if (
            not accepted
            or normalized_intent not in {"tool_handover", "tool_retrieve"}
            or not normalized_utterance_id
        ):
            return ()
        signature = _tool_signature_from_action_and_instrument(
            (
                "tool_handover"
                if normalized_intent == "tool_handover"
                else "retrieve_from_mayo"
            ),
            tool_id,
            self._aliases,
        )
        if signature is None:
            return ()
        with self._lock:
            if not self._scope.active:
                return ()
            generation = self._voice_generations.get(normalized_utterance_id, 0)
            self._put_bounded(
                self._typed_voice_tool_intents,
                normalized_utterance_id,
                _TypedVoiceToolIntent(
                    utterance_id=normalized_utterance_id,
                    signature=signature,
                    request_generation=generation,
                ),
            )
            return self._link_pending_explicit_replacement_returns_locked()

    def observe_explicit_retrieval_request(
        self,
        *,
        tool_id: object,
        accepted: object,
    ) -> None:
        """Mark one manual return request so it does not speak auto-recovery.

        A manually requested return uses the same existing action path as
        autonomous Mayo recovery.  The action remains identical; this small
        observer-only association simply prevents its presentation text from
        claiming it was an automatic prediction.
        """

        if not accepted:
            return
        key = _normalized_key(tool_id)
        if not key:
            return
        with self._lock:
            if not self._scope.active:
                return
            self._expire_explicit_retrievals_locked()
            self._put_bounded(self._explicit_retrieval_tools, key, time.monotonic())

    def observe_skill_command(
        self,
        *,
        command_id: object,
        action: object,
        instrument_id: object,
        request_generation: object = 0,
        voice_backed: object = False,
    ) -> tuple[ReplyRequest, ...]:
        if isinstance(request_generation, bool):
            generation = 0
        else:
            try:
                generation = max(0, int(request_generation))
            except (TypeError, ValueError):
                generation = 0
        command = _ToolCommand(
            command_id=str(command_id or "").strip(),
            action=str(action or "").strip(),
            instrument_id=str(instrument_id or "").strip(),
            request_generation=generation,
            voice_backed=bool(voice_backed),
        )
        is_explicit_replacement_return = (
            command.action == _RETURN_UNUSED_PREPOSITION_ACTION
            and command.request_generation > 0
        )
        if (
            not command.command_id
            or (
                command.action not in _TOOL_ACTION_KINDS
                and not is_explicit_replacement_return
            )
        ):
            return ()
        with self._lock:
            if not self._scope.active:
                return ()
            if self._consume_explicit_retrieval_locked(command):
                command = _ToolCommand(
                    command_id=command.command_id,
                    action=command.action,
                    instrument_id=command.instrument_id,
                    request_generation=command.request_generation,
                    voice_backed=command.voice_backed,
                    announcement_suppressed=True,
                )
            if self._is_replacement_handover_pickup_locked(command):
                command = replace(command, announcement_suppressed=True)
            self._put_bounded(self._tool_commands, command.command_id, command)
            self._bind_typed_voice_command_locked(command)
            requests = list(self._link_pending_explicit_replacement_returns_locked())
            request = self._tool_request_if_accepted(command.command_id)
            if request is not None:
                requests.append(request)
            return tuple(requests)

    def is_duplicate_voice_reply(
        self,
        *,
        utterance_id: object,
        text: object,
    ) -> bool:
        """Whether a VLM acknowledgement duplicates its typed tool command.

        The correlation is intentionally narrow: it requires the same typed
        voice proposal or admitted voice-turn generation and the same
        configured tool/action wording. Unrelated dialogue and autonomous
        tool work remain audible.
        """

        normalized_utterance_id = str(utterance_id or "").strip()
        reply_signature = _tool_reply_signature(text, self._aliases)
        if not normalized_utterance_id or reply_signature is None:
            return False
        with self._lock:
            if not self._scope.active:
                return False
            # A typed tool intent is already a command-shaped request.  Do
            # not let the VLM phrase the same handover/retrieval before the
            # execution endpoint has accepted it; if the request is rejected,
            # there must be no success-like TTS at all.
            typed_intent = self._typed_voice_tool_intents.get(
                normalized_utterance_id
            )
            if (
                typed_intent is not None
                and typed_intent.signature == reply_signature
            ):
                return True
            generation = self._voice_generations.get(normalized_utterance_id)
            if generation is not None:
                for command in reversed(self._tool_commands.values()):
                    if (
                        not command.voice_backed
                        or command.request_generation != generation
                        or command.command_id not in self._accepted_tool_commands
                        or _tool_command_signature(command, self._aliases)
                        != reply_signature
                    ):
                        continue
                    return True
            command_id = self._typed_voice_command_utterances.get(
                normalized_utterance_id
            )
            command = self._tool_commands.get(command_id or "")
            if (
                command is not None
                and command.voice_backed
                and command.command_id in self._accepted_tool_commands
                and _tool_command_signature(command, self._aliases) == reply_signature
            ):
                return True
        return False

    def observe_execution_trace(
        self,
        *,
        command_id: object,
        route: object,
        transport: object,
        stage: object,
        evidence: object,
        dispatch_submitted: object,
        retraction_command: object = 0,
        retraction_target_side: object = 0,
        retraction_distance_m: object = 0.0,
    ) -> tuple[ReplyRequest, ...]:
        normalized_command_id = str(command_id or "").strip()
        normalized_route = str(route or "").strip()
        normalized_transport = str(transport or "").strip().lower()
        normalized_stage = str(stage or "").strip()
        normalized_evidence = str(evidence or "").strip().lower()
        if (
            not normalized_command_id
            or normalized_stage != _ACCEPTED_STAGE
            or dispatch_submitted is not True
        ):
            return ()
        with self._lock:
            if not self._scope.active:
                return ()
            if normalized_route == _TOOL_ROUTE:
                if (
                    normalized_transport != _TOOL_ACTION_TRANSPORT
                    or normalized_evidence != _TOOL_ACTION_RECEIPT_EVIDENCE
                ):
                    return ()
                self._put_bounded(
                    self._accepted_tool_commands, normalized_command_id, None
                )
                requests = list(self._link_pending_explicit_replacement_returns_locked())
                request = self._tool_request_if_accepted(normalized_command_id)
                if request is not None:
                    requests.append(request)
                return tuple(requests)
            if normalized_route != _RETRACTION_ROUTE:
                return ()
            if (
                normalized_transport != _RETRACTION_SERVICE_TRANSPORT
                or normalized_evidence != _RETRACTION_SERVICE_RECEIPT_EVIDENCE
            ):
                return ()
            try:
                command = int(retraction_command)
            except (TypeError, ValueError):
                return ()
            text = (
                retraction_adjustment_text(
                    retraction_target_side,
                    retraction_distance_m,
                )
                if command == 4
                else _RETRACTION_TEXT_BY_COMMAND.get(command, "")
            )
            if not text:
                return ()
            return self._request_once(
                semantic_key=f"retraction:{command}:{normalized_command_id}",
                command_id=normalized_command_id,
                text=text,
                priority=TTS_PRIORITY_SURGEON,
            )

    def _tool_request_if_accepted(self, command_id: str) -> ReplyRequest | None:
        if command_id not in self._accepted_tool_commands:
            return None
        command = self._tool_commands.get(command_id)
        if command is None or command.announcement_suppressed:
            return None
        if command.replacement_handover_signature is not None:
            text = tool_action_text(
                "pick_up_and_handover",
                command.replacement_handover_signature[1],
                self._aliases,
            )
            semantic_key = f"tool:voice_handover_on_return:{command.command_id}"
        else:
            text = tool_action_text(command.action, command.instrument_id, self._aliases)
            semantic_key = f"tool:{command.action}:{command.command_id}"
        if not text:
            return None
        requests = self._request_once(
            semantic_key=semantic_key,
            command_id=command.command_id,
            text=text,
            priority=(
                TTS_PRIORITY_SURGEON
                if command.replacement_handover_signature is not None
                else _tts_priority_for_tool(
                    action=command.action,
                    voice_backed=command.voice_backed,
                )
            ),
        )
        if requests and command.replacement_handover_signature is not None:
            self._put_bounded(
                self._replacement_handover_signatures,
                command.request_generation,
                command.replacement_handover_signature,
            )
        return requests[0] if requests else None

    def _link_pending_explicit_replacement_returns_locked(
        self,
    ) -> tuple[ReplyRequest, ...]:
        """Attach an explicit handover to a pending accepted parking Action.

        Only a generated, typed voice handover may use this path.  Ordinary
        autonomous parking remains silent, and a failed/no-receipt Action can
        never produce a success-like TTS phrase.
        """

        requests: list[ReplyRequest] = []
        for command_id, command in tuple(self._tool_commands.items()):
            if (
                command.action != _RETURN_UNUSED_PREPOSITION_ACTION
                or command.request_generation <= 0
                or command.replacement_handover_signature is not None
            ):
                continue
            signature = self._voice_handover_signature_for_generation_locked(
                command.request_generation
            )
            if signature is None:
                continue
            command = replace(command, replacement_handover_signature=signature)
            self._tool_commands[command_id] = command
            request = self._tool_request_if_accepted(command_id)
            if request is not None:
                requests.append(request)
        return tuple(requests)

    def _voice_handover_signature_for_generation_locked(
        self,
        request_generation: int,
    ) -> tuple[str, str] | None:
        for proposal in reversed(self._typed_voice_tool_intents.values()):
            if (
                proposal.request_generation == request_generation
                and proposal.signature[0] == "handover"
            ):
                return proposal.signature
        return None

    def _is_replacement_handover_pickup_locked(self, command: _ToolCommand) -> bool:
        """Suppress the later pickup after the parking Action spoke for it."""

        if not command.voice_backed or command.request_generation <= 0:
            return False
        signature = _tool_command_signature(command, self._aliases)
        return (
            signature is not None
            and self._replacement_handover_signatures.get(command.request_generation)
            == signature
        )

    def _bind_typed_voice_command_locked(self, command: _ToolCommand) -> None:
        """Bind the next direct voice proposal to its voice-backed command."""

        if not command.voice_backed:
            return
        signature = _tool_command_signature(command, self._aliases)
        if signature is None:
            return
        for utterance_id, proposal in self._typed_voice_tool_intents.items():
            if (
                utterance_id in self._typed_voice_command_utterances
                or proposal.signature != signature
            ):
                continue
            self._put_bounded(
                self._typed_voice_command_utterances,
                utterance_id,
                command.command_id,
            )
            return

    def _consume_explicit_retrieval_locked(self, command: _ToolCommand) -> bool:
        if command.action != "retrieve_from_mayo":
            return False
        self._expire_explicit_retrievals_locked()
        key = _normalized_key(command.instrument_id)
        if not key or key not in self._explicit_retrieval_tools:
            return False
        self._explicit_retrieval_tools.pop(key, None)
        return True

    def _expire_explicit_retrievals_locked(self) -> None:
        now = time.monotonic()
        for key, created_monotonic in tuple(self._explicit_retrieval_tools.items()):
            if now - created_monotonic > _EXPLICIT_RETRIEVAL_BIND_WINDOW_SEC:
                self._explicit_retrieval_tools.pop(key, None)

    def _request_once(
        self,
        *,
        semantic_key: str,
        command_id: str,
        text: str,
        priority: int = TTS_PRIORITY_NORMAL,
    ) -> tuple[ReplyRequest, ...]:
        if semantic_key in self._announced:
            return ()
        scope = self._scope
        material = {
            "command_id": command_id,
            "gateway_instance_id": scope.gateway_instance_id,
            "procedure_run_id": scope.procedure_run_id,
            "schema": _SCHEMA,
            "semantic_key": semantic_key,
        }
        digest = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
        self._put_bounded(self._announced, semantic_key, None)
        return (
            ReplyRequest(
                reply_id=f"execution-announcement:{digest}",
                turn_id=f"execution-turn:{digest}",
                utterance_id=f"execution-utterance:{digest}",
                gateway_instance_id=scope.gateway_instance_id,
                procedure_run_id=scope.procedure_run_id,
                text=text,
                timing="immediate",
                priority=priority,
            ),
        )

    def _put_bounded(self, mapping: OrderedDict, key: object, value: object) -> None:
        mapping.pop(key, None)
        mapping[key] = value
        while len(mapping) > self._max_facts:
            mapping.popitem(last=False)
