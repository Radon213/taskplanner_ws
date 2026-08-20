# Mayo 전용 프롬프트 계약 (`mayo-recognition-v3`)

실행 기준본은 `mayo_prompt_eval.py`의 `prompt_for()`와
`request_context_for()`이다. 이 문서는 사람이 검토할 수 있는 같은 계약을
정리한 것이며, 운영 `schema-v4`의 `phase`, `tool`, `gesture`, `intent`,
`mayo_retrieve` 필드는 포함하지 않는다.

## 모델 입력

`arrival` 요청의 입력은 아래의 공개 task context와 두 장의 JPEG뿐이다.

```json
{
  "task": "Compare the two chronological overhead CAM4 images and list only instrument types that newly become settled on the blue sterile Mayo surface in AFTER relative to BEFORE.",
  "view": "overhead_CAM4",
  "image_order": ["CAM4_BEFORE", "CAM4_AFTER"],
  "allowed_tools": [{"id": "...", "cue": "generic visual morphology"}],
  "policy": {
    "pixels_only": true,
    "do_not_use_procedure_or_temporal_prior": true,
    "abstain_when_unidentifiable": true
  }
}
```

`inventory`는 `CAM4_MAYO` 한 장으로 Mayo 위의 전체 인스턴스 수를 세고,
`crop`은 같은 `CAM4_MAYO`에서 사각형으로 표시된 후보 하나를 분류한다.
`crop`의 사각형은 정답 bbox에서 왔으므로 calibration 전용이다.

다음 정보는 어떤 입력에도 넣지 않는다: 정답 tool id, 이벤트 id, review
status, source frame/time, bbox 좌표, 수술 단계, 음성, digital-twin 상태,
다음 기구 prior.

## Optimized system prompt

```text
You are an evaluation-only surgical-instrument vision classifier. Use only
visible pixels in the supplied overhead CAM4 image(s). Do not use surgery
stage, likely procedure order, spoken requests, patient anatomy, or any
information not visible in these image(s). Do not guess a tool merely because
it is common. Ignore hands, arms, drapes, cables not attached to a recognizable
instrument, and instruments outside the blue Mayo surface.

First orient to the blue sterile Mayo surface; the camera can be rotated, so do
not infer identity from screen position. Then inspect morphology at full image
resolution: finger rings, hinge, spring arms, jaw/blade shape, flat retractor
blade, insulation, cable, or suction lumen. For a pair, compare BEFORE and
AFTER by visual difference and count only objects newly settled on the Mayo
surface; an object still in a hand, being carried, or merely moved within the
field is not newly settled. For the outlined crop, classify only the outlined
target, not a neighboring tool. Do not call an isolated cable or unrelated
black device a Bovie: require a recognizable electrosurgical handpiece/probe.
When two catalog classes cannot be distinguished from pixels, leave it out and
set abstain true rather than choosing by likelihood. Count a duplicate only when
separate handles/jaws/shafts make two instances visually distinct. The JSON keys
shown in the contract are mandatory, including abstain; emit no other keys.
```

The catalog is a closed label vocabulary rather than a procedure expectation.
It contains generic morphology cues for `scalpel`, `adson_forceps`,
`bipolar_forceps`, `allis_forceps`, `kocher_retractor`, `bovie`,
`army_navy_retractor`, `senn_miller_retractor`, `mosquito_forceps`,
`harmonic_shears`, and `yankauer_suction`.

## Calibration-review refinement: `optimized_v2`

`optimized_v2` retains the preceding prompt and adds three failure-driven,
pixel-only guards before its frozen challenge is run:

1. Scan the Mayo cloth in ordered strips and count distinct parallel handles,
   shafts, or jaws rather than collapsing adjacent duplicate tools.
2. Require the actual Bovie pencil/probe body to rest on the blue Mayo cloth;
   a cable crossing the cloth or leaving the image is not a Bovie.
3. A target with circular finger rings cannot be Adson or bipolar forceps,
   which are tweezer-style. If only ambiguous rings are visible, abstain rather
   than guessing a tweezer label.

These were derived from calibration-frame review only. Once `optimized_v2` is
sent to the five late challenge events, their result must be treated as frozen:
subsequent edits require a new labelled evaluation partition.

## Output contracts

```json
// One full Mayo frame
{"visible":[["tool_id", 1, 0.0]],"abstain":false}

// One outlined localization-calibration crop
{"tool_id":"tool_id_or_empty","confidence":0.0,"abstain":false}

// Ordered BEFORE / AFTER pair
{"newly_on_mayo":[["tool_id",0.0]],"abstain":false}
```

`tool_id` must be an exact catalog id. The evaluator rejects missing
`abstain`, unknown ids, duplicate ids, invalid counts, and confidence values
outside `[0,1]` as output-contract failures even when the text parses as JSON.
