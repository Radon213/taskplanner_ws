# syntax=docker/dockerfile:1.7
# Keep the ONNX/Supertonic dependency set outside the shared ROS Python
# environment. The ROS node uses the base image interpreter and starts this
# venv only as a resident synthesis subprocess.
FROM taskplanner-ws:dev

SHELL ["/bin/bash", "-c"]
USER root

RUN set -e; \
    for attempt in 1 2 3 4; do \
      rm -rf /var/lib/apt/lists/*; \
      if apt-get -o Acquire::Retries=5 update; then break; fi; \
      if [ "${attempt}" -eq 4 ]; then exit 1; fi; \
      sleep "$((attempt * 5))"; \
    done; \
    apt-get install -y --no-install-recommends python3-venv; \
    rm -rf /var/lib/apt/lists/*

COPY docker/tts-runtime/requirements.txt /tmp/taskplanner-tts-requirements.txt
RUN python3 -m venv /opt/taskplanner-tts-venv \
    && /opt/taskplanner-tts-venv/bin/python -m pip install \
       --no-cache-dir --requirement /tmp/taskplanner-tts-requirements.txt \
    && /opt/taskplanner-tts-venv/bin/python -c \
       "import numpy, onnxruntime, soundfile, supertonic; print(supertonic.__version__)" \
    && rm -f /tmp/taskplanner-tts-requirements.txt

ENV TASKPLANNER_TTS_SYNTH_PYTHON=/opt/taskplanner-tts-venv/bin/python
