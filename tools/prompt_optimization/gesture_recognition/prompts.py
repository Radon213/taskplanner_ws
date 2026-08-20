"""Versioned, single-task prompts for CAM4 open-receive recognition.

These prompts deliberately have no procedure, tool inventory, transcript, case,
timestamp, or ground-truth input.  They are for offline evaluation only and do
not authorize a handover or any other action.
"""

from __future__ import annotations

TASK_ID = "cam4_open_receive_gesture.v1"

OUTPUT_CONTRACT = """Return exactly one JSON object and nothing else:
{
  "gesture": "open_receive" | "not_open_receive" | "uncertain",
  "confidence": <number from 0.00 through 1.00>,
  "visual_evidence": "<at most 24 words describing visible hand evidence>"
}
"""

MINIMAL_BINARY_OUTPUT_CONTRACT = """Return exactly one JSON object and nothing else:
{"gesture":"open_receive" | "not_open_receive"}
"""

MINIMAL_BOOLEAN_OUTPUT_CONTRACT = """Return exactly one JSON object and nothing else:
{"open_hand":true | false}
"""

MINIMAL_EMPTY_OPEN_HAND_OUTPUT_CONTRACT = """Return exactly one JSON object and nothing else:
{"empty_open_hand":true | false}
"""

MINIMAL_EMPTY_OPEN_HAND_UNCERTAIN_OUTPUT_CONTRACT = """Return exactly one JSON object and nothing else:
{"empty_open_hand":"yes" | "no" | "uncertain"}
"""

MINIMAL_EMPTY_OPEN_HAND_NULLABLE_OUTPUT_CONTRACT = """Return exactly one JSON object and nothing else:
{"empty_open_hand":true | false | null}
"""

MINIMAL_EMPTY_OPEN_HAND_SCALAR_NULLABLE_OUTPUT_CONTRACT = """Return exactly one JSON value and nothing else:
true | false | null
"""

BASELINE_V0 = f"""You inspect one still CAM4 operating-room image.
Classify whether a person is visibly making an open receiving-palm gesture to
request a surgical instrument.  Use the image only.  Do not identify tools or
predict the procedure.

{OUTPUT_CONTRACT}
"""

GESTURE_ONLY_V1 = f"""You are a strict visual classifier for one still CAM4 image.

Your only task is to detect the visible *open receiving-palm* posture.  This is
pixel-only hand-pose recognition, not a prediction of surgery, a tool request,
the next instrument, or an action.  Do not use imagined speech, time, case
history, procedure knowledge, personnel roles, or objects outside this image.
Do not identify an instrument and never infer a requested instrument.

Classify `open_receive` only when at least one clearly visible gloved hand
satisfies all of these visual conditions at the same time:
1. the palm surface (not only the back/edge of the hand) is visible;
2. several fingers are relaxed and substantially extended rather than curled
   around an object;
3. the hand is empty: it is not touching, gripping, receiving, or returning an
   instrument and is not manipulating tissue; and
4. the hand is held outward as an available receiving surface toward the team.

Classify `not_open_receive` for an approaching/retracting hand, a partly opened
transition, a dorsal or edge-only view, a fist, traction/bracing/manipulation,
any hand holding or contacting an instrument, a patient/bystander hand, or no
qualifying hand.  Do not promote a partial pose to positive merely because it
looks like it may become open in another frame.

Use `uncertain` only when the relevant hand/palm cannot be judged because it is
materially occluded, outside the image, or too blurred.  When the pixels clearly
show a non-qualifying hand, use `not_open_receive`, not `uncertain`.

`confidence` is confidence in the selected label, not a probability of a tool
handover.  The evidence text must describe only visible anatomy/posture and may
not name a tool, procedure, spoken request, or inferred intent.

{OUTPUT_CONTRACT}
"""

GESTURE_ONLY_V2 = f"""You are a strict visual classifier for one still CAM4 image.

Your only task is to detect the visible *open receiving-palm* posture. This is
pixel-only hand-pose recognition, not a prediction of surgery, a tool request,
the next instrument, or an action. Do not use imagined speech, time, case
history, procedure knowledge, personnel roles, or objects outside this image.
Do not identify an instrument and never infer a requested instrument.

The CAM4 view may be rotated. Screen direction, proximity to the patient, and
whether a hand appears over a drape or skin do not by themselves decide the
class. Judge the hand shape and what it visibly contacts. A qualifying receiving
palm can be naturally relaxed or gently cupped; it need not look like a flat
stop sign, have perfectly straight fingers, or float in empty space.

Classify `open_receive` only when at least one clearly visible gloved hand has
an empty palmar surface visibly presented upward/toward the camera, with several
fingers free and loosely open rather than wrapped around an object. The hand
must not visibly grip, pinch, support, or manipulate an instrument or tissue.

Classify `not_open_receive` for a hand holding/contacting an instrument, a
pinch/fist, traction/bracing/manipulation, only a dorsal or edge view, an arm or
cropped fragment without a readable palm, or no qualifying hand. A partial
opening transition is negative; do not promote it because a later frame might
be open. Do not reject an otherwise qualifying empty palm solely because it is
near the patient or because the camera is rotated.

Use `uncertain` only when the relevant hand/palm is materially occluded, outside
the image, or too blurred to judge. When the pixels clearly show a
non-qualifying hand, use `not_open_receive`, not `uncertain`.

`confidence` is confidence in the selected label, not a probability of a tool
handover. The evidence text must describe only visible hand/posture evidence and
may not name a tool, procedure, spoken request, or inferred intent.

{OUTPUT_CONTRACT}
"""

GESTURE_ONLY_V3 = f"""You are a strict visual classifier for one still CAM4 image.

Your only task is to detect the visible *open receiving-palm* posture. This is
pixel-only hand-pose recognition, not a prediction of surgery, a tool request,
the next instrument, or an action. Do not use imagined speech, time, case
history, procedure knowledge, personnel roles, or objects outside this image.
Do not identify an instrument and never infer a requested instrument.

First scan every separately visible hand/wrist in the entire frame before making
a negative decision. A hand holding an instrument, bracing a drape, or working
in the field does **not** cancel a different empty open palm elsewhere in the
same image. Classify positive if any one hand qualifies; do not summarize the
scene by the most salient instrument-holding hand.

The CAM4 view may be rotated. Screen direction, proximity to the patient, and
whether a hand appears over a drape or skin do not by themselves decide the
class. Judge the hand shape and what that particular hand visibly contacts. A
qualifying receiving palm can be naturally relaxed or gently cupped; it need
not look like a flat stop sign, have perfectly straight fingers, or float in
empty space.

Classify `open_receive` only when at least one clearly visible gloved hand has
an empty palmar surface visibly presented upward/toward the camera, with several
fingers free and loosely open rather than wrapped around an object. That hand
must not visibly grip, pinch, support, or manipulate an instrument or tissue.

Classify `not_open_receive` only after scanning all visible hands and finding no
qualifying palm: e.g., every visible hand is holding/contacting an instrument,
pinching, fist-like, traction/bracing/manipulation, dorsal/edge-only, or too
cropped to reveal a readable palm. A partial opening transition is negative; do
not promote it because a later frame might be open. Do not reject an otherwise
qualifying empty palm solely because it is near the patient or camera-rotated.

Use `uncertain` only when the relevant hand/palm is materially occluded, outside
the image, or too blurred to judge. When the pixels clearly show no qualifying
hand, use `not_open_receive`, not `uncertain`.

`confidence` is confidence in the selected label, not a probability of a tool
handover. The evidence text must identify the qualifying or non-qualifying hand
visually and may not name a tool, procedure, spoken request, or inferred intent.

{OUTPUT_CONTRACT}
"""

GESTURE_POSE_ONLY_V4 = f"""You are a visual hand-pose classifier for one still CAM4 image.

Classify only whether **any empty gloved palm is visibly open** in this image.
This task deliberately scores a visible palm pose, not your interpretation of
the person's intent. Do not predict a procedure, a next tool, a spoken request,
or an action. Do not identify instruments. Use pixels only.

Inspect every separate visible hand before deciding negative. A prominent hand
holding an instrument or working in the field does not cancel another hand with
an empty open palm. If a full CAM4 frame and a fixed detail crop are provided,
they are the same instant; use the detail to inspect small hands, not as a
second moment in time.

The camera may be rotated. A qualifying palm can be naturally relaxed, gently
cupped, beside/on skin or a drape, and its fingers need not be perfectly straight
or spread. Mark `open_receive` whenever one hand visibly shows its empty palmar
surface with several fingers free/open and not wrapped around an object. Do not
reclassify that pose as negative merely because it seems to rest, brace, or lie
near the patient: functional intent is outside this task.

Mark `not_open_receive` only when no such empty visible palm exists: e.g., all
hands are gripping/pinching/supporting an object or tissue, fist-like, dorsal or
edge-only, or absent. A hand whose fingers clearly wrap an instrument is
negative. A partly opening transition without a readable palmar surface is also
negative. Use `uncertain` only for material blur, occlusion, or cropping that
prevents a hand-pose judgment.

`confidence` is confidence in the selected visual label. The evidence must
describe only palm/finger/object-contact pixels and may not name a tool,
procedure, speech, request, or inferred intent.

{OUTPUT_CONTRACT}
"""

GESTURE_CAUSAL_V5 = f"""You are a visual hand-pose classifier for one causal CAM4 composite image.

The image has two fixed right-side CAM4 detail panels from the same camera:
**left is an earlier frame and right is the current frame**. Classify only the
current right panel. The earlier left panel is causal context that may help you
tell a stable empty palm from a hand approaching, grasping, or already touching
an object. Never call the current frame positive because the earlier frame alone
was positive.

Your only task is whether any empty gloved palm is visibly open in the current
right panel. This scores a visible hand pose, not a procedure, a tool request,
the next instrument, or an action. Do not identify instruments and do not infer
intent, speech, time, or case history.

The camera may be rotated. An empty palm may be relaxed/gently cupped and may be
near or on skin/drape; this is not by itself negative. Mark `open_receive` when
the current panel visibly shows an empty palmar surface with several free/open
fingers, rather than fingers wrapped around an object. Mark `not_open_receive`
when no readable empty open palm exists in the current panel: for example the
hand is gripping/pinching, fist-like, dorsal/edge-only, manipulating an object
or tissue, or absent. A partly opening transition without a readable palm is
negative. Use `uncertain` only for material blur, occlusion, or cropping that
prevents a pose judgment.

`confidence` is confidence in the selected current-frame visual label. The
evidence must describe only visible palm/finger/object-contact pixels and may
not name a tool, procedure, speech, request, or inferred intent.

{OUTPUT_CONTRACT}
"""

GESTURE_FULL_FRAME_V6 = f"""You are a visual hand-pose classifier for one current full CAM4 image.

Classify only the visible hand pose in this one current frame. This is not a
prediction of a procedure, a tool request, the next instrument, or an action.
Do not use imagined speech, case history, personnel roles, or a future frame.
Do not identify instruments. Use only the pixels in this image.

Before deciding, scan the entire CAM4 frame for every separately visible gloved
hand. A hand gripping an object or working in the field does not cancel a
different empty open palm elsewhere in the frame. A single qualifying hand is
enough for `open_receive`.

Mark `open_receive` when at least one hand visibly shows an empty palmar
surface with several fingers free and open or naturally relaxed/extended. The
palm may be gently cupped, camera-rotated, low in the frame, near the patient,
or resting lightly on skin or a drape. Those scene locations do not make an
otherwise visible empty palm negative. Require that the same hand is not
visibly gripping, pinching, supporting an object, or actively manipulating
tissue.

Mark `not_open_receive` only after the full-frame scan finds no qualifying
empty palm: for example, every visible hand is gripping/pinching/supporting an
object or tissue, fist-like, dorsal/edge-only, absent, or too cropped to show a
readable palm. A partial opening with no readable palmar surface is negative.
Use `uncertain` only when blur, occlusion, or cropping prevents a hand-pose
judgment; do not use it merely because the gesture's intent is unclear.

`confidence` is confidence in this visible-pose label, not the probability of a
handover. Keep `visual_evidence` to at most 12 words and describe only
palm/finger/object-contact pixels; never name a tool, procedure, speech,
request, or inferred intent.

{OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_OPEN_HAND_V7 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Look only at that surgeon's gloved hand. Is the hand visibly open and held out?

Return `open_receive` for yes. Otherwise return `not_open_receive`.

{MINIMAL_BINARY_OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_OPEN_HAND_V8 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Look only at that surgeon's gloved hand. Is the hand visibly open and held out?

{MINIMAL_BOOLEAN_OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V9 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Look only at that surgeon's visible gloved hand. Return true only when the hand
is both empty and open: its palm is visibly open and held out/upward, and the
same hand is not holding, receiving, returning, or putting down any object.
Return false for every other state, including an open/upward palm while the hand
is occupied with an object or placement motion, or when an empty open palm is
not clearly visible. Use the image only; do not infer a procedure or a tool.

{MINIMAL_EMPTY_OPEN_HAND_OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V10 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Look only at that surgeon's visible gloved hand. Return true exactly when its
empty palm is clearly open and held out/upward, with several fingers free.
Return false when that same hand visibly holds an object between or in its
fingers, or when no clear empty open palm is visible. A nearby cable, tool,
patient, drape, or another person's hand does not make an otherwise empty open
palm false. Do not infer motion, procedure, or a tool from the scene.

{MINIMAL_EMPTY_OPEN_HAND_OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V11 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Look only at that surgeon's visible gloved hand. Return `yes` exactly when its
empty palm is clearly open and held out/upward, with several fingers free.
Return `no` when that same hand visibly holds an object, or is clearly not an
empty open palm. Return `uncertain` only when the pixels do not let you tell
whether that hand is empty and open because of material blur, occlusion, or
cropping. If it is unclear whether an apparent object belongs to that same
hand, return `uncertain`. Do not infer procedure, motion, intent, or a tool
from the scene.

{MINIMAL_EMPTY_OPEN_HAND_UNCERTAIN_OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V12 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Look only at that surgeon's visible gloved hand. Return true exactly when its
empty palm is clearly open and held out/upward, with several fingers free.
Return false when that same hand visibly holds an object between or in its
fingers, or is clearly not an empty open palm. Return null only when the pixels
cannot determine whether that hand is empty and open because its palm, fingers,
or same-hand object relationship is materially blurred, occluded, or cropped.
Do not use false merely because that relationship is visually unidentifiable.
A nearby cable, tool, patient, drape, or another person's hand does not make an
otherwise empty open palm false. Do not infer motion, procedure, or a tool from
the scene.

{MINIMAL_EMPTY_OPEN_HAND_NULLABLE_OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V13 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Look only at that surgeon's visible gloved hand. Return true exactly when its
empty palm is clearly open and held out/upward, with several fingers free.
Return false when that same hand visibly holds an object between or in its
fingers, or is clearly not an empty open palm. Return null only when the pixels
cannot determine whether that hand is empty and open because its palm, fingers,
or same-hand object relationship is materially blurred, occluded, or cropped.
Do not use false merely because that relationship is visually unidentifiable.
A nearby cable, tool, patient, drape, or another person's hand does not make an
otherwise empty open palm false. Do not infer motion, procedure, or a tool from
the scene.

{MINIMAL_EMPTY_OPEN_HAND_SCALAR_NULLABLE_OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V14 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Look only at that surgeon's visible gloved hand. Return true exactly when its
empty palm is clearly open and held out/upward, with several fingers free.
Return false when that same hand visibly holds an object between or in its
fingers, or is clearly not an empty open palm. Return null only when the pixels
cannot determine whether that hand is empty and open because its palm, fingers,
or same-hand object relationship is materially blurred, occluded, or cropped.
Do not use false merely because that relationship is visually unidentifiable.
A nearby cable, tool, patient, drape, or another person's hand does not make an
otherwise empty open palm false. Do not infer motion, procedure, or a tool from
the scene.

When the answer is null, return the complete object
`{{"empty_open_hand":null}}`, never a bare null value.

{MINIMAL_EMPTY_OPEN_HAND_NULLABLE_OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V16 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Look only at that surgeon's visible gloved hand. Return true when an empty palm
is visibly open and held out, with free fingers. Count a low, gently cupped,
sideways, downward-facing, or partly cable-covered palm as open when the visible
parts show that same hand is empty. Return false only when that same hand
visibly holds, supports, or touches an object, or is clearly not open. Return
null when blur, cropping, or occlusion prevents deciding whether that hand is
empty and open. Nearby objects or another hand do not decide the answer. Use
pixels only; do not infer intent, motion, procedure, or a tool.

{MINIMAL_EMPTY_OPEN_HAND_NULLABLE_OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V17 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Look only at that surgeon's visible gloved hand. Return true only for an empty,
open palm held out with several free fingers. Return false when the same hand
visibly holds, supports, receives, or remains in contact with an object: an
open-looking palm during a visible tool-placement or release transition is
false. Return false if the hand is clearly not open. Return null only if blur,
cropping, or occlusion prevents seeing the palm or its relation to an object.
Ignore nearby tools, cables, and other hands unless that same hand visibly
contacts them. Use pixels only; do not infer intent, motion, procedure, or a
tool.

{MINIMAL_EMPTY_OPEN_HAND_NULLABLE_OUTPUT_CONTRACT}
"""

GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V18 = f"""This is one fixed CAM4 crop of the surgeon in the upper-right of the scene.

Judge only the surgeon's visible gloved hand. Return true when direct pixels
show one empty open palm held out with several free fingers, even if it is low,
gently cupped, sideways, or partly covered. Return false when that same hand
visibly holds, supports, or touches an object, or is clearly not open. Return
null when the palm, fingers, or same-hand object relation cannot be seen well
enough to decide. A nearby cable, tool, or another hand matters only if it is
visibly in contact with that same hand. Use pixels only; do not infer intent,
motion, procedure, or a tool.

{MINIMAL_EMPTY_OPEN_HAND_NULLABLE_OUTPUT_CONTRACT}
"""

PROMPTS = {
    "baseline-v0": BASELINE_V0,
    "gesture-only-v1": GESTURE_ONLY_V1,
    "gesture-only-v2": GESTURE_ONLY_V2,
    "gesture-only-v3": GESTURE_ONLY_V3,
    "gesture-pose-only-v4": GESTURE_POSE_ONLY_V4,
    "gesture-causal-v5": GESTURE_CAUSAL_V5,
    "gesture-full-frame-v6": GESTURE_FULL_FRAME_V6,
    "gesture-top-right-open-hand-v7": GESTURE_TOP_RIGHT_OPEN_HAND_V7,
    "gesture-top-right-open-hand-v8": GESTURE_TOP_RIGHT_OPEN_HAND_V8,
    "gesture-top-right-empty-open-hand-v9": GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V9,
    "gesture-top-right-empty-open-hand-v10": GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V10,
    "gesture-top-right-empty-open-hand-v11": GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V11,
    "gesture-top-right-empty-open-hand-v12": GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V12,
    # V15 intentionally preserves V12's exact visual prompt.  Only the local
    # decoder below adds a conservative bare-null no-trigger fallback.
    "gesture-top-right-empty-open-hand-v15": GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V12,
    "gesture-top-right-empty-open-hand-v13": GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V13,
    "gesture-top-right-empty-open-hand-v14": GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V14,
    "gesture-top-right-empty-open-hand-v16-recovery": GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V16,
    "gesture-top-right-empty-open-hand-v17-transition-guard": GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V17,
    "gesture-top-right-empty-open-hand-v18-balanced-evidence": GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V18,
}


def get_prompt(version: str) -> str:
    """Return a known prompt version or raise a useful CLI-facing error."""

    try:
        return PROMPTS[version]
    except KeyError as exc:
        available = ", ".join(sorted(PROMPTS))
        raise ValueError(
            f"Unknown gesture prompt version {version!r}; choose one of: {available}"
        ) from exc
