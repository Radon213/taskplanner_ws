#!/usr/bin/env python3
"""Task-specific prompt and wire contract for next-instrument forecasting.

This module is deliberately independent of ``real_vlm.py``.  It defines an
offline experiment contract only; no output from this module is a robot command
or a production VLM message.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "taskplanner.next_tool_forecast_prompt.v1"
MODEL_ID = "qwen3.6-35b-a3b"
HORIZON_SEC = (2.0, 8.0)
LOOKBACK_SEC = 6.0

# ``optimized_v3`` is intentionally a calibration-only hypothesis.  Its
# diagnostic sibling has one additional field, but its task semantics stay the
# same so the evaluator can score the usual four fields without ever feeding
# labels back into a request.
STRICT_VARIANTS = frozenset(
    {
        "baseline_v0",
        "baseline_v0_timestamped_asr",
        "optimized_v1",
        "optimized_v2_prior",
        "optimized_v3",
    }
)
DIAGNOSTIC_VARIANTS = frozenset({"optimized_v3_diagnostic"})
PROMPT_VARIANTS = tuple(sorted(STRICT_VARIANTS | DIAGNOSTIC_VARIANTS))
CALIBRATION_ONLY_VARIANTS = frozenset(
    {"baseline_v0_timestamped_asr", "optimized_v3", "optimized_v3_diagnostic"}
)
TIMESTAMPED_ASR_VARIANTS = frozenset(
    {"baseline_v0_timestamped_asr", "optimized_v3", "optimized_v3_diagnostic"}
)
DIAGNOSTIC_EVIDENCE_TYPES = frozenset(
    {"fresh_asr_visual", "visual_only", "none_or_ambiguous"}
)

# Canonical observable-tool IDs.  ``retractor_bundle_unresolved`` is excluded:
# it is an explicitly unresolved observation rather than a forecastable tool.
TOOL_IDS = (
    "scalpel",
    "adson_forceps",
    "allis_forceps",
    "bovie",
    "army_navy_retractor",
    "bipolar_forceps",
    "mosquito_forceps",
    "kocher_retractor",
    "senn_miller_retractor",
    "harmonic_shears",
    "yankauer_suction",
)
TOOL_ID_SET = frozenset(TOOL_IDS)

TOOL_LEGEND = ", ".join(TOOL_IDS)
OUTPUT_EXAMPLE = (
    '{"decision":"handover","tool_id":"adson_forceps",'
    '"confidence":0.82,"uncertainty":0.18}'
)
NONE_EXAMPLE = (
    '{"decision":"none","tool_id":"",'
    '"confidence":0.78,"uncertainty":0.22}'
)
UNCERTAIN_EXAMPLE = (
    '{"decision":"uncertain","tool_id":"",'
    '"confidence":0.42,"uncertainty":0.76}'
)


def _base_system_prompt() -> str:
    return (
        "You are evaluating one task only: the next additional surgical "
        "instrument handover. The last image is the causal cutoff. Predict "
        "the first new scrub-nurse-to-surgeon handover that will happen 2 to "
        "8 seconds after that cutoff. This is a forecast, not an inventory, "
        "phase label, current-tool label, speech-intent label, hand-gesture "
        "label, or a command. Use only the supplied chronological images and "
        "the supplied public ASR text. Never infer from case identity, absolute "
        "clock time, annotations, ground truth, replay metadata, private plans, "
        "or knowledge of a recording. Allowed tool_id values are: "
        f"{TOOL_LEGEND}."
    )


BASELINE_SYSTEM_PROMPT = _base_system_prompt()
BASELINE_DEVELOPER_PROMPT = (
    "Return exactly one JSON object and no markdown or explanation. "
    f"Use this shape: {OUTPUT_EXAMPLE}. "
    "decision is one of handover, none, uncertain. tool_id must be an allowed "
    "ID only for handover and must otherwise be an empty string. confidence and "
    "uncertainty are finite numbers from 0 to 1."
)

OPTIMIZED_SYSTEM_PROMPT = _base_system_prompt()
OPTIMIZED_DEVELOPER_PROMPT = (
    "Return exactly one JSON object and no markdown, rationale, or extra keys. "
    "Valid forms are "
    f"{OUTPUT_EXAMPLE}, {NONE_EXAMPLE}, or {UNCERTAIN_EXAMPLE}. "
    "decision is exactly one of handover, none, uncertain. tool_id is exactly "
    "one allowed canonical ID only when decision is handover; otherwise it is "
    "the empty string. confidence and uncertainty are finite numbers in [0,1].\n\n"
    "Perform the following checks silently before deciding. (1) Compare the "
    "FLIR frames oldest-to-newest for a concrete operative trajectory and the "
    "CAM4 frames oldest-to-newest for a distinct handover/Mayo trajectory. "
    "(2) Separate an instrument already held, visible, being used, or mentioned "
    "in a current request from an additional future handover. Do not copy that "
    "current instrument as the forecast merely because it is visible or spoken. "
    "(3) Require convergent, time-local evidence for a specific additional tool; "
    "do not select a generic procedure prior. (4) If there is no specific "
    "evidence for a handover in the 2-to-8-second window, output decision none. "
    "Use uncertain only when the supplied evidence is materially unusable or "
    "conflicting; do not turn uncertainty into a guessed tool. Confidence is the "
    "probability of the declared decision being correct, and uncertainty is the "
    "residual ambiguity of this forecast."
)

# This is a compact, case-agnostic subset of the checked-in thyroidectomy
# procedure specification.  It intentionally contains functional cues rather
# than an exact demonstrated tool sequence, case identity, or timestamp.
PROCEDURE_PRIOR = (
    "Optional weak thyroidectomy prior: a visible change from broad exposure to "
    "stable wound-edge retraction can support army_navy_retractor; controlled "
    "tissue traction can precede focal bipolar_forceps or bovie treatment; an "
    "isolated small focal point can precede mosquito_forceps; superficial edge "
    "control can support adson_forceps. These are alternatives, never a required "
    "order. Visible current activity outranks this prior, and the prior alone "
    "must never turn none into a tool forecast."
)
OPTIMIZED_V2_SYSTEM_PROMPT = OPTIMIZED_SYSTEM_PROMPT + " " + PROCEDURE_PRIOR
OPTIMIZED_V2_DEVELOPER_PROMPT = OPTIMIZED_DEVELOPER_PROMPT

# Failure review of the calibration lock showed that a current instrument,
# fulfilled request, or residual field activity was often copied forward as a
# future handover.  These rules deliberately ask for *unfulfilled* and
# time-local evidence, rather than a procedure prior or a replayed target.
OPTIMIZED_V3_SYSTEM_PROMPT = _base_system_prompt()
OPTIMIZED_V3_EVIDENCE_RULES = (
    "Before deciding, reason silently from the last chronological evidence. "
    "A handover means one additional, not-yet-fulfilled scrub-nurse-to-surgeon "
    "transfer after the cutoff, not an instrument already in use. Treat a named "
    "ASR tool request as forecast evidence when it is the latest relevant, "
    "unfulfilled explicit request close to the causal cutoff. The relative "
    "arrival offset makes recency observable: values nearer 0 are newer; all "
    "values are before the cutoff. A fresh unfulfilled explicit request may "
    "support a handover even if a CAM4 receiving cue is not yet visible. Use "
    "CAM4 to check whether that request already appears fulfilled; an arrival "
    "cue can corroborate but is not required. FLIR field activity alone, a "
    "generic hand motion, or a tool merely lying/being held/being used is "
    "insufficient. Do not carry a "
    "current or fulfilled tool, a stale ASR request, or a past trajectory into "
    "the future prediction. Visual-only handover support requires a concrete "
    "new CAM4 handover/arrival trajectory with identifiable tool evidence. If "
    "that evidence is absent or ambiguous, output none; do not use a procedure "
    "prior to guess a tool."
)
OPTIMIZED_V3_DEVELOPER_PROMPT = (
    "Return exactly one JSON object and no markdown, rationale, or extra keys. "
    "Valid forms are "
    f"{OUTPUT_EXAMPLE}, {NONE_EXAMPLE}, or {UNCERTAIN_EXAMPLE}. "
    "decision is exactly one of handover, none, uncertain. tool_id is exactly "
    "one allowed canonical ID only when decision is handover; otherwise it is "
    "the empty string. confidence and uncertainty are finite numbers in [0,1].\n\n"
    + OPTIMIZED_V3_EVIDENCE_RULES
)
OPTIMIZED_V3_DIAGNOSTIC_DEVELOPER_PROMPT = (
    "Return exactly one JSON object and no markdown, rationale, or extra keys. "
    "Use exactly these five keys: decision, tool_id, confidence, uncertainty, "
    "evidence_type. decision is handover, none, or uncertain. tool_id is one "
    "allowed canonical ID only for handover and otherwise the empty string. "
    "confidence and uncertainty are finite numbers in [0,1]. evidence_type is "
    "exactly one of fresh_asr_visual, visual_only, none_or_ambiguous. Use "
    "fresh_asr_visual only for a fresh unfulfilled explicit ASR request after "
    "checking CAM4 does not already show it fulfilled; a receiving/arrival cue "
    "can corroborate but is not required. Use visual_only only for a concrete "
    "new visible transfer/arrival with no qualifying fresh ASR; use "
    "none_or_ambiguous for none or uncertain.\n\n"
    + OPTIMIZED_V3_EVIDENCE_RULES
)


def prompts(variant: str) -> tuple[str, str]:
    """Return the exact system/developer prompt pair for a named experiment."""

    if variant == "baseline_v0":
        return BASELINE_SYSTEM_PROMPT, BASELINE_DEVELOPER_PROMPT
    if variant == "baseline_v0_timestamped_asr":
        return BASELINE_SYSTEM_PROMPT, BASELINE_DEVELOPER_PROMPT
    if variant == "optimized_v1":
        return OPTIMIZED_SYSTEM_PROMPT, OPTIMIZED_DEVELOPER_PROMPT
    if variant == "optimized_v2_prior":
        return OPTIMIZED_V2_SYSTEM_PROMPT, OPTIMIZED_V2_DEVELOPER_PROMPT
    if variant == "optimized_v3":
        return OPTIMIZED_V3_SYSTEM_PROMPT, OPTIMIZED_V3_DEVELOPER_PROMPT
    if variant == "optimized_v3_diagnostic":
        return OPTIMIZED_V3_SYSTEM_PROMPT, OPTIMIZED_V3_DIAGNOSTIC_DEVELOPER_PROMPT
    raise ValueError(f"unknown prompt variant: {variant}")


def output_contract_name(variant: str) -> str:
    """Return the externally comparable output-shape class for a variant."""

    if variant in STRICT_VARIANTS:
        return "deployable_four_key"
    if variant in DIAGNOSTIC_VARIANTS:
        return "diagnostic_five_key"
    raise ValueError(f"unknown prompt variant: {variant}")


def asr_input_contract_name(variant: str) -> str:
    """Return whether the request exposes plain text or relative ASR arrival."""

    if variant in TIMESTAMPED_ASR_VARIANTS:
        return "timestamped_relative_asr"
    if variant in STRICT_VARIANTS | DIAGNOSTIC_VARIANTS:
        return "plain_asr"
    raise ValueError(f"unknown prompt variant: {variant}")


def public_user_text(
    frame_offsets_sec: Iterable[float],
    public_asr: Iterable[str | Mapping[str, Any]],
    *,
    asr_input_contract: str = "plain_asr",
) -> str:
    """Render the only text passed with the image sequence.

    The caller supplies offsets relative to the cutoff, never absolute timestamps
    or case IDs.  ASR has already been causally filtered by availability time.
    """

    offsets = [float(value) for value in frame_offsets_sec]
    if not offsets or offsets[-1] != 0.0:
        raise ValueError("frame offsets must end at the causal cutoff 0.0")
    if any(not math.isfinite(value) or value > 0.0 for value in offsets):
        raise ValueError("frame offsets must be finite and no later than cutoff")
    if offsets != sorted(offsets):
        raise ValueError("frame offsets must be chronological")
    asr_values = list(public_asr)
    if asr_input_contract == "plain_asr":
        if any(isinstance(value, Mapping) for value in asr_values):
            raise ValueError("plain ASR contract requires text strings")
        speech: list[Any] = [str(value).strip() for value in asr_values if str(value).strip()]
        speech_heading = "Causally available public ASR in the lookback window: "
    elif asr_input_contract == "timestamped_relative_asr":
        speech = []
        previous_offset = -math.inf
        for value in asr_values:
            if not isinstance(value, Mapping) or set(value) != {"text", "available_offset_sec"}:
                raise ValueError("timestamped ASR requires text and available_offset_sec only")
            item_text = value.get("text")
            offset = value.get("available_offset_sec")
            if not isinstance(item_text, str) or not item_text.strip():
                raise ValueError("timestamped ASR text must be nonempty")
            try:
                item_offset = float(offset)
            except (TypeError, ValueError) as exc:
                raise ValueError("timestamped ASR offset must be numeric") from exc
            if (
                not math.isfinite(item_offset)
                or item_offset > 0.0
                or item_offset < -8.0
                or item_offset < previous_offset
            ):
                raise ValueError("timestamped ASR offsets must be chronological and in [-8, 0]")
            previous_offset = item_offset
            speech.append(
                {
                    "text": item_text.strip(),
                    "available_offset_sec": round(item_offset, 6),
                }
            )
        speech_heading = (
            "Causally available public ASR in the lookback window. Each "
            "available_offset_sec is relative to the cutoff, never absolute; "
            "negative values were available before it and values nearer 0 are newer: "
        )
    else:
        raise ValueError(f"unknown ASR input contract: {asr_input_contract}")
    parts = [
        "The images are chronological evidence pairs. For each offset, the FLIR "
        "operative-field image appears first and the CAM4 hand/Mayo image appears "
        "second. The final CAM4 image is the cutoff; do not use any future evidence.",
        "Relative frame offsets in seconds: "
        + json.dumps(offsets, ensure_ascii=False, separators=(",", ":")),
        speech_heading + json.dumps(speech, ensure_ascii=False, separators=(",", ":")),
    ]
    return "\n".join(parts)


def build_messages(
    *,
    variant: str,
    frame_offsets_sec: Iterable[float],
    public_asr: Iterable[str | Mapping[str, Any]],
    images: Iterable[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Build an OpenAI-compatible request without labels or source identifiers.

    ``images`` contains ``(view, data_uri)`` pairs in chronological order.  The
    only permitted views are FLIR and CAM4, and each image remains local to the
    request as a data URI rather than exposing a source filename.
    """

    system, developer = prompts(variant)
    # NInfer's current multimodal chat template accepts a system message only
    # as the first/sole instruction role.  A separate OpenAI ``developer``
    # role is rejected before inference, so preserve the same text and order
    # by compiling it into that system message for this server-specific wire
    # contract.
    wire_system = system + "\n\nOUTPUT CONTRACT:\n" + developer
    image_rows = list(images)
    if not image_rows or len(image_rows) % 2:
        raise ValueError("expected paired FLIR/CAM4 images")
    expected_views = ["flir", "cam4"] * (len(image_rows) // 2)
    actual_views = [view for view, _data_uri in image_rows]
    if actual_views != expected_views:
        raise ValueError("image order must be FLIR then CAM4 for each time point")
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": public_user_text(
                frame_offsets_sec,
                public_asr,
                asr_input_contract=asr_input_contract_name(variant),
            ),
        }
    ]
    for view, data_uri in image_rows:
        if not data_uri.startswith("data:image/"):
            raise ValueError("images must be image data URIs")
        content.append({"type": "text", "text": f"VIEW: {view.upper()}"})
        content.append({"type": "image_url", "image_url": {"url": data_uri}})
    messages = [
        {"role": "system", "content": wire_system},
        {"role": "user", "content": content},
    ]
    rendered = json.dumps(messages, ensure_ascii=False)
    forbidden = ("ground_truth", "target_event", "annotation_manifest", "case_id")
    if any(token in rendered for token in forbidden):
        raise AssertionError("non-public label/provenance leaked into model request")
    return messages


def extract_json_object(raw_text: str) -> tuple[dict[str, Any] | None, str]:
    """Extract one JSON object from a model response without accepting junk shape."""

    text = re.sub(r"<think>.*?</think>", "", str(raw_text), flags=re.DOTALL).strip()
    start = text.find("{")
    if start < 0:
        return None, "no_json_object"
    try:
        value, _consumed = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        return None, f"json_decode:{exc.msg}"
    if not isinstance(value, dict):
        return None, "json_not_object"
    return value, ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and 0.0 <= parsed <= 1.0 else None


def validate_prediction(
    value: Mapping[str, Any], *, variant: str = "baseline_v0"
) -> tuple[dict[str, Any] | None, str]:
    """Validate a variant's task output exactly; do not silently repair values."""

    diagnostic = variant in DIAGNOSTIC_VARIANTS
    if variant not in STRICT_VARIANTS | DIAGNOSTIC_VARIANTS:
        return None, "variant"
    required = {"decision", "tool_id", "confidence", "uncertainty"}
    if diagnostic:
        required.add("evidence_type")
    if set(value) != required:
        return None, "keys"
    decision = value.get("decision")
    tool_id = value.get("tool_id")
    confidence = _number(value.get("confidence"))
    uncertainty = _number(value.get("uncertainty"))
    if decision not in {"handover", "none", "uncertain"}:
        return None, "decision"
    if not isinstance(tool_id, str):
        return None, "tool_id_type"
    if confidence is None or uncertainty is None:
        return None, "confidence_or_uncertainty"
    if decision == "handover" and tool_id not in TOOL_ID_SET:
        return None, "handover_tool_id"
    if decision != "handover" and tool_id != "":
        return None, "non_handover_tool_id"
    validated = {
        "decision": decision,
        "tool_id": tool_id,
        "confidence": confidence,
        "uncertainty": uncertainty,
    }
    if diagnostic:
        evidence_type = value.get("evidence_type")
        if evidence_type not in DIAGNOSTIC_EVIDENCE_TYPES:
            return None, "evidence_type"
        if decision == "handover" and evidence_type not in {
            "fresh_asr_visual",
            "visual_only",
        }:
            return None, "handover_evidence_type"
        if decision != "handover" and evidence_type != "none_or_ambiguous":
            return None, "non_handover_evidence_type"
        validated["evidence_type"] = evidence_type
    return validated, ""


def thresholded_decision(prediction: Mapping[str, Any], threshold: float) -> str:
    """Return the scored binary handover decision at a locked threshold."""

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be in [0, 1]")
    if (
        prediction.get("decision") == "handover"
        and float(prediction.get("confidence", -1.0)) >= threshold
    ):
        return "handover"
    return "none"
