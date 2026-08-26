ARG TASKPLANNER_BASE_IMAGE=taskplanner-ws:dev
FROM ${TASKPLANNER_BASE_IMAGE}

USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
