"""Text-only VLM interpretation for the closed retractor command vocabulary.

This client is deliberately independent from the schema-v4 visual VLM path.
That path only emits legacy retraction-adjustment proposals, whereas this
module consumes one final STT transcript and asks an OpenAI-compatible local
model endpoint for one of the six reviewed commands.  Every response is
validated locally against :mod:`procedure_spec.retractor_command`; model output
never becomes a ROS command directly.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Callable
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from procedure_spec import (
    NormalizedRetractionCommand,
    RetractionCommand,
    RetractionState,
    RetractionTargetSide,
    allowed_retractor_commands,
    normalize_retractor_adjustment_parameters,
    normalize_retractor_command,
)


_SCHEMA_VERSION = "1"
_MAX_ADJUSTMENT_DISTANCE_M = 0.050

_TEACH_DOMAIN_TERMS = (
    "직접교시",
    "직접교수",
    "가르치",
    "가르키",
    "가리키",
    "가리치",
    "티치",
    "direct teach",
    "direct teaching",
    "teaching",
    "teach",
    "hand guide",
    "hand guiding",
)
_RETRACTION_DOMAIN_TERMS = (
    "리트랙",
    "리트렉",
    "리트락",
    "견인",
    "retract",
    "retraction",
    "retractor",
)
_TOOL_DOMAIN_TERMS = (
    "툴",
    "도구",
    "기구",
    "장비",
    "엔드이펙터",
    "tool",
    "instrument",
    "equipment",
    "end effector",
)
_START_CUE_TERMS = (
    "시작",
    "개시",
    "켜",
    "들어가",
    "가자",
    "하자",
    "해줘",
    "부탁",
    "진행",
    "start",
    "begin",
    "commence",
    "activate",
    "enable",
    "engage",
)
_STOP_CUE_TERMS = (
    "종료",
    "끝",
    "완료",
    "그만",
    "중지",
    "멈",
    "마치",
    "마무리",
    "됐",
    "다했",
    "다했어",
    "끄",
    "해제",
    "stop",
    "end",
    "finish",
    "done",
    "complete",
    "wrap up",
    "disable",
)
_CHANGE_CUE_TERMS = (
    "교체",
    "교환",
    "변경",
    "바꿔",
    "바꾸",
    "다른",
    "새걸",
    "새것",
    "스위치",
    "change",
    "swap",
    "switch",
    "replace",
    "different",
    "another",
)
_ADJUST_CUE_TERMS = (
    "더",
    "추가",
    "조정",
    "이동",
    "당겨",
    "당기",
    "끌어",
    "밀어",
    "한번",
    "한차례",
    "more",
    "adjust",
    "move",
    "pull",
    "shift",
    "once",
)


def _transcript_forms(value: object) -> tuple[str, str, tuple[str, ...]]:
    """Canonical forms used only to prove command-family evidence.

    This is intentionally a broad semantic gate, not a second command
    normalizer.  The model still classifies intent, while this gate prevents a
    state-constrained model from turning unrelated operating-room speech into
    the sole command allowed by that state.
    """

    spaced = unicodedata.normalize("NFKC", str(value or "")).casefold()
    spaced = re.sub(r"[^0-9a-z가-힣]+", " ", spaced)
    spaced = re.sub(r"\s+", " ", spaced).strip()
    compact = spaced.replace(" ", "")
    english_words = tuple(re.findall(r"[a-z]+", spaced))
    return spaced, compact, english_words


def _contains_evidence_term(
    forms: tuple[str, str, tuple[str, ...]],
    term: str,
) -> bool:
    spaced, compact, english_words = forms
    if not term.isascii():
        return term.replace(" ", "") in compact
    words = tuple(re.findall(r"[a-z]+", term.casefold()))
    if not words:
        return False
    if len(words) == 1:
        anchor = words[0]
        return any(
            word == anchor or (len(anchor) >= 5 and word.startswith(anchor))
            for word in english_words
        )
    phrase = " ".join(words)
    return bool(re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", spaced))


def _has_any_evidence(
    forms: tuple[str, str, tuple[str, ...]],
    terms: tuple[str, ...],
) -> bool:
    return any(_contains_evidence_term(forms, term) for term in terms)


def _has_command_family_evidence(
    transcript: str,
    command: RetractionCommand,
) -> bool:
    """Require command-specific raw STT evidence before trusting fuzzy VLM intent."""

    forms = _transcript_forms(transcript)
    teach_domain = _has_any_evidence(forms, _TEACH_DOMAIN_TERMS)
    retraction_domain = _has_any_evidence(forms, _RETRACTION_DOMAIN_TERMS)
    if command == RetractionCommand.START_DIRECT_TEACH:
        return teach_domain and _has_any_evidence(forms, _START_CUE_TERMS)
    if command == RetractionCommand.FINISH_DIRECT_TEACH:
        return teach_domain and _has_any_evidence(forms, _STOP_CUE_TERMS)
    if command == RetractionCommand.START_RETRACTION:
        return retraction_domain and _has_any_evidence(forms, _START_CUE_TERMS)
    if command == RetractionCommand.STOP_RETRACTION:
        return retraction_domain and _has_any_evidence(forms, _STOP_CUE_TERMS)
    if command == RetractionCommand.CHANGE_TOOL:
        return _has_any_evidence(forms, _TOOL_DOMAIN_TERMS) and _has_any_evidence(
            forms, _CHANGE_CUE_TERMS
        )
    if command == RetractionCommand.ADJUST_RETRACTION:
        # Side and distance are separately parsed from raw STT below.  A
        # movement/retraction cue is still required so a spatial observation
        # such as "오른쪽 절개 5 cm" cannot become robot motion.
        return retraction_domain or _has_any_evidence(forms, _ADJUST_CUE_TERMS)
    return False


def is_retractor_voice_protocol_candidate(transcript: str) -> bool:
    """Return whether raw STT contains evidence for any of the six commands.

    This is the broad, pre-VLM routing gate used by the operational
    orchestrator.  It deliberately shares the same Korean/English paraphrase
    evidence as the model-output grounding step so a phrase that the
    interpreter supports cannot be diverted into the legacy adjustment route
    before the model sees it.  State and physical parameters are still
    validated later; this helper grants no execution authority.
    """

    return any(
        _has_command_family_evidence(transcript, command)
        for command in RetractionCommand
    )


@dataclass(frozen=True, slots=True)
class RetractionVoiceInterpretation:
    """A normalized result with honest interpreter provenance."""

    normalized: NormalizedRetractionCommand
    interpreter_source: str
    vlm_invoked: bool
    detail: str


def _chat_completions_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("text_vlm_response_not_json") from None
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("text_vlm_response_not_object")
    return payload


def _response_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("text_vlm_transport_payload_not_object")
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict):
                content = message.get("content", "")
                if isinstance(content, str):
                    return content
            text = choice.get("text")
            if isinstance(text, str):
                return text
    raise ValueError("text_vlm_response_missing_content")


class TextOnlyRetractionVLMInterpreter:
    """Call a local OpenAI-compatible text model with deterministic fallback.

    ``request_json`` is injectable solely for unit tests.  Production uses
    ``urllib`` so the BT package does not take a dependency on the visual VLM
    package or its image-oriented client.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        timeout_sec: float = 2.0,
        api_key: str = "",
        request_json: Callable[[str, dict[str, Any], float, dict[str, str]], object]
        | None = None,
    ) -> None:
        self._base_url = str(base_url or "").strip()
        self._model_id = str(model_id or "").strip()
        self._timeout_sec = max(0.1, float(timeout_sec))
        self._api_key = str(api_key or "").strip()
        self._request_json = request_json or self._post_json

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You normalize a final surgeon STT transcript into exactly one "
            "retractor command. Return JSON only, with exactly these keys: "
            "v, command, target_side, distance_m. v must be the string '1'. "
            "command must be one of start_direct_teach, finish_direct_teach, "
            "start_retraction, adjust_retraction, change_tool, stop_retraction, "
            "or none. target_side must be none, left, or right. distance_m "
            "must be 0 for every non-adjust command; an adjustment requires "
            "a single side and a positive metres value. Do not infer a missing "
            "side. If an adjustment names exactly one side but omits distance, "
            "use 0.05 metres. If its side is missing or bilateral, return "
            "command='none', target_side='none', distance_m=0. Do not report "
            "execution or physical completion. The current state and allowed "
            "commands are authoritative. Be tolerant of STT spelling errors, "
            "particles, and natural paraphrases, but return command='none' for "
            "unrelated operating-room speech. Examples: '직접 교시 시작' or "
            "'리트렉터 직접 가르치기 모드 켜줘' -> start_direct_teach; "
            "'리트랙션 오른쪽 5cm 더' or '오른쪽으로 한 번만 더 당겨' "
            "-> adjust_retraction/right/0.05; '장비 다른 걸로 바꿔줘' -> "
            "change_tool; '석션 주세요' -> none."
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _post_json(
        url: str,
        body: dict[str, Any],
        timeout_sec: float,
        headers: dict[str, str],
    ) -> object:
        request = Request(
            url,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=timeout_sec) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _rejected(reason: str) -> NormalizedRetractionCommand:
        return NormalizedRetractionCommand(
            command=None,
            target_side=RetractionTargetSide.NONE,
            distance_m=0.0,
            confidence=0.0,
            reason=reason,
        )

    @staticmethod
    def _coerce_state(value: RetractionState | str) -> RetractionState:
        try:
            return value if isinstance(value, RetractionState) else RetractionState(str(value))
        except ValueError:
            return RetractionState.UNKNOWN

    def _validate_model_response(
        self,
        raw: str,
        transcript: str,
        current_state: RetractionState | str,
    ) -> NormalizedRetractionCommand:
        payload = _extract_json_object(raw)
        if set(payload) != {"v", "command", "target_side", "distance_m"}:
            return self._rejected("text_vlm_schema_keys_invalid")
        # The local NInfer model commonly emits numeric 1 despite being asked
        # for string "1".  This is an internal parsing tolerance only; the ROS
        # Service wire schema remains unchanged and strict.
        if str(payload.get("v", "")) != _SCHEMA_VERSION:
            return self._rejected("text_vlm_schema_version_invalid")
        command_value = str(payload.get("command", "")).strip()
        if command_value == "none":
            return self._rejected("text_vlm_declined")
        try:
            command = RetractionCommand(command_value)
        except ValueError:
            return self._rejected("text_vlm_command_invalid")
        state = self._coerce_state(current_state)
        if command not in allowed_retractor_commands(state):
            return self._rejected(f"command_not_allowed_in_{state.value}")
        if command == RetractionCommand.ADJUST_RETRACTION:
            try:
                target_side = RetractionTargetSide(
                    str(payload.get("target_side", "")).strip()
                )
            except ValueError:
                return self._rejected("text_vlm_target_side_invalid")
            try:
                distance_m = float(payload.get("distance_m"))
            except (TypeError, ValueError):
                return self._rejected("text_vlm_distance_invalid")
            if target_side not in {
                RetractionTargetSide.LEFT,
                RetractionTargetSide.RIGHT,
            }:
                return self._rejected("text_vlm_adjustment_side_invalid")
            if (
                not math.isfinite(distance_m)
                or distance_m <= 0.0
                or distance_m > _MAX_ADJUSTMENT_DISTANCE_M
            ):
                return self._rejected("text_vlm_adjustment_distance_invalid")
            model_normalized = NormalizedRetractionCommand(
                command=command,
                target_side=target_side,
                distance_m=distance_m,
                confidence=0.80,
                reason="normalized_text_vlm",
            )
        else:
            # NInfer's text response may use JSON null for parameters that do
            # not apply.  Canonicalize only those two non-adjustment nulls.
            # Any other value remains a schema error.
            raw_target_side = payload.get("target_side")
            if raw_target_side is None:
                target_side = RetractionTargetSide.NONE
            else:
                try:
                    target_side = RetractionTargetSide(
                        str(raw_target_side).strip()
                    )
                except ValueError:
                    return self._rejected("text_vlm_target_side_invalid")
            raw_distance = payload.get("distance_m")
            if raw_distance is None:
                distance_m = 0.0
            else:
                try:
                    distance_m = float(raw_distance)
                except (TypeError, ValueError):
                    return self._rejected("text_vlm_distance_invalid")
            if target_side != RetractionTargetSide.NONE or distance_m != 0.0:
                return self._rejected("text_vlm_non_adjustment_parameters_invalid")
            model_normalized = NormalizedRetractionCommand(
                command=command,
                target_side=target_side,
                distance_m=0.0,
                confidence=0.80,
                reason="normalized_text_vlm",
            )

        # Prefer an exact deterministic match whenever it exists.  It detects
        # explicit contradictions/ambiguity before the fuzzier semantic gate.
        grounded = normalize_retractor_command(transcript, state)
        if grounded.command is not None and grounded.command != command:
            return self._rejected("text_vlm_command_conflicts_with_transcript")
        if grounded.reason == "ambiguous_command":
            return self._rejected(
                f"text_vlm_transcript_conflict:{grounded.reason}"
            )

        if command == RetractionCommand.ADJUST_RETRACTION:
            # Physical parameters are parsed exclusively from raw STT.  The
            # VLM may classify a paraphrase as adjustment but cannot fill in,
            # change, or widen either parameter.
            raw_adjustment = normalize_retractor_adjustment_parameters(transcript)
            if raw_adjustment.command is None:
                return self._rejected(
                    f"text_vlm_transcript_not_grounded:{raw_adjustment.reason}"
                )
            if raw_adjustment.target_side != model_normalized.target_side:
                return self._rejected("text_vlm_side_conflicts_with_transcript")
            if (
                abs(raw_adjustment.distance_m - model_normalized.distance_m)
                > 1e-9
            ):
                return self._rejected("text_vlm_distance_conflicts_with_transcript")
            if not _has_command_family_evidence(transcript, command):
                return self._rejected("text_vlm_command_family_not_grounded")
            grounded = raw_adjustment
        elif grounded.command is None:
            # This is the intentional fuzzy path: the model resolves a natural
            # paraphrase, while command-specific raw words prove it belongs to
            # the retractor domain.  State restriction above still applies.
            if not _has_command_family_evidence(transcript, command):
                return self._rejected("text_vlm_command_family_not_grounded")
            grounded = model_normalized

        return NormalizedRetractionCommand(
            command=grounded.command,
            target_side=grounded.target_side,
            distance_m=grounded.distance_m,
            confidence=model_normalized.confidence,
            reason="normalized_text_vlm_grounded",
        )

    def _request_completion(self, body: dict[str, Any]) -> object:
        """Use NInfer's supported text mode, then retry without the hint.

        Some OpenAI-compatible endpoints reject even ``type=text`` although
        they still return plain chat content.  Only 400/422 compatibility
        responses trigger the one-shot retry; transport/server failures use the
        deterministic fallback without multiplying requests.
        """

        url = _chat_completions_url(self._base_url)
        try:
            return self._request_json(
                url,
                body,
                self._timeout_sec,
                self._headers(),
            )
        except HTTPError as exc:
            if exc.code not in {400, 422} or "response_format" not in body:
                raise
            compatible_body = dict(body)
            compatible_body.pop("response_format", None)
            return self._request_json(
                url,
                compatible_body,
                self._timeout_sec,
                self._headers(),
            )

    def interpret(
        self,
        transcript: str,
        current_state: RetractionState | str,
    ) -> RetractionVoiceInterpretation:
        """Prefer a real model call and preserve a safe deterministic fallback."""

        fallback = normalize_retractor_command(transcript, current_state)
        if not self._base_url or not self._model_id:
            return RetractionVoiceInterpretation(
                normalized=fallback,
                interpreter_source="deterministic_fallback",
                vlm_invoked=False,
                detail="text_vlm_not_configured",
            )

        state = self._coerce_state(current_state)
        body = {
            "model": self._model_id,
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": 120,
            # qwen3.6-35b-a3b on local NInfer explicitly supports text rather
            # than json_object.  JSON is still required and strictly parsed
            # from the returned text below.
            "response_format": {"type": "text"},
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "transcript": str(transcript or ""),
                            "current_state": state.value,
                            "allowed_commands": sorted(
                                command.value
                                for command in allowed_retractor_commands(state)
                            ),
                            "default_adjustment_distance_m": _MAX_ADJUSTMENT_DISTANCE_M,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
        }
        try:
            payload = self._request_completion(body)
            normalized = self._validate_model_response(
                _response_text(payload),
                transcript,
                state,
            )
        except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            return RetractionVoiceInterpretation(
                normalized=fallback,
                interpreter_source="deterministic_fallback",
                vlm_invoked=True,
                detail=f"text_vlm_unavailable:{type(exc).__name__}",
            )
        if normalized.command is None:
            return RetractionVoiceInterpretation(
                normalized=fallback,
                interpreter_source="deterministic_fallback",
                vlm_invoked=True,
                detail=normalized.reason,
            )
        return RetractionVoiceInterpretation(
            normalized=normalized,
            interpreter_source="text_vlm",
            vlm_invoked=True,
            detail="text_vlm_normalized",
        )
