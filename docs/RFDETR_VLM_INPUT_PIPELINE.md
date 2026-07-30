# RF-DETR VLM Input Pipeline

The VLM receives two public evidence channels. They are intentionally
asymmetric:

1. **Visual channel:** the FLIR frame after `RFDETRSegSmall` instance
   segmentation and class-aware ByteTrack overlay.
2. **Structured channel:** CAM4 (Mayo view) `RFDETRSmall` detections reduced to
   tool names/counts and a conservative hand-request state.

CAM4 pixels, annotated CAM4 frames, bounding boxes, and detector internals are
not sent to the VLM. Missing hand detections produce `uncertain`, never
`not_request`.

## Runtime

Start the GPU preprocessing service in the installed RF-DETR environment:

```bash
scripts/run_rfdetr_perception_service.sh
```

For a quick startup without CUDA graph/compile optimization:

```bash
scripts/run_rfdetr_perception_service.sh --no-optimize
```

The shadow launch starts the lightweight ROS bridge by default and expects the
service at `http://127.0.0.1:8010`.

## ROS Topics

| Topic | Meaning | VLM access |
|---|---|---|
| `/surgery/images/flir/compressed` | raw FLIR source | no |
| `/surgery/images/cam4/compressed` | raw Mayo-view source | no |
| `/surgery/images/flir/segmented/compressed` | segmented FLIR | image input |
| `/surgery/perception/cam4/semantics/json` | tool counts and request state | text context |
| `/surgery/perception/rfdetr/diagnostics/json` | boxes and detailed detector rows | no |
| `/surgery/images/vlm/composite/compressed` | exact bounded model-ready FLIR frame | dashboard/trace |

## VLM Clinical Analysis

Schema v4 keeps the wire key `sum` for backward compatibility. Its meaning is
now a one- or two-sentence clinical analysis suitable for a later
operative-record draft. Internal reducer markers, candidate stabilization
labels, confidence commentary, and prompt/schema details are excluded.
