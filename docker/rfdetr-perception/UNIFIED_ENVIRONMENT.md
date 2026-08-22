# Unified Hand / Tool / Blood compatibility profile

This opt-in image preserves Taskplanner's digest-pinned PyTorch 2.8, CUDA 12.9,
torchvision 0.23 and RF-DETR 1.9 baseline.  It does **not** install the upstream
CUDA 11.8 environment or Hand's optional torch 2.11 / Transformers mono-depth
profile.

The two intentional changes from the existing image are:

- NumPy 2.3.2 -> 1.26.4 for MediaPipe 0.10.18.
- the two overlapping OpenCV 5 distributions -> one
  `opencv-contrib-python==4.11.0.86` distribution.

`supervision` declares `opencv-python` rather than accepting the contrib wheel
as a virtual provider.  Pip therefore reports one known metadata warning even
though the contrib wheel is an API superset.  The build gate allows exactly
that warning, rejects any other `pip check` error, and exercises the OpenCV and
Supervision APIs used by Taskplanner.

Build without changing the existing production image:

```bash
docker build \
  --file docker/rfdetr-perception/Dockerfile.unified \
  --tag taskplanner-rfdetr-perception:unified-compat \
  .
```

The image deliberately does not copy third-party source or model weights.  To
verify the pinned upstream core against a read-only checkout:

```bash
docker run --rm \
  --volume /path/to/hand-blood-tools:/opt/hand-blood-tools:ro \
  --entrypoint python \
  taskplanner-rfdetr-perception:unified-compat \
  /opt/taskplanner/compatibility_check.py \
  --import-taskplanner-service \
  --upstream-root /opt/hand-blood-tools \
  --expected-upstream-commit 0f9e93115b8cc1d470398c92e010e3fc6ef1de5d
```

Checkpoint loading can be gated on CPU without inference.  Mount the model
directory read-only and add one argument per model:

```text
--checkpoint taskplanner-flir:seg:/models/flir.pth
--checkpoint taskplanner-cam4:small:/models/cam4.pth
--checkpoint tool:seg:/models/tool.pth
--checkpoint blood:seg:/models/blood.pth
```

Passing the environment/import gate does not prove checkpoint output parity,
GPU load, latency, VRAM use, or metric 3-D accuracy.  Those remain runtime
acceptance gates after the real checkpoints and aligned depth/calibration
topics are mounted.
