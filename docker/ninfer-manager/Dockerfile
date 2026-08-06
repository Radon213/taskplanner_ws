ARG NINFER_BASE_IMAGE=ubuntu:26.04@sha256:3131b4cc82a783df6c9df078f86e01819a13594b865c2cad47bd1bca2b7063bb
FROM ${NINFER_BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      libavcodec62 \
      libavformat62 \
      libavutil60 \
      libcurl4t64 \
      libswscale9 \
      python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/taskplanner-ninfer-manager

COPY scripts/ninfer_runtime_manager.py /opt/taskplanner-ninfer-manager/ninfer_runtime_manager.py
COPY docker/ninfer-manager/start_manager.sh /usr/local/bin/taskplanner-ninfer-manager
COPY docker/ninfer-manager/run_ninfer_worker.sh /usr/local/bin/taskplanner-ninfer-worker

RUN chmod +x \
      /usr/local/bin/taskplanner-ninfer-manager \
      /usr/local/bin/taskplanner-ninfer-worker

ENTRYPOINT ["/usr/local/bin/taskplanner-ninfer-manager"]
