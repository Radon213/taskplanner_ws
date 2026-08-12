# syntax=docker/dockerfile:1.7
FROM ros:jazzy-ros-base@sha256:eac11a5285beeb1e1884e71f7091c610e08452e823bfb3f43afaa334375325f6

SHELL ["/bin/bash", "-c"]

ARG BTOPS_REF="main"
ARG BTOPS_LOCAL_CONTEXT="../btops_ws"
ARG AUTO_APMS_REPO_URL="https://github.com/AutoAPMS/auto-apms.git"
ARG AUTO_APMS_REF="19ac8d558e35f657b8464694c5ddc524c6c31861"
ARG TASKPLANNER_BUILD_VERSION="0.1.0-dev"
ARG TASKPLANNER_BUILD_SHA="unknown"
ARG TASKPLANNER_SHADOW_CONTRACT_VERSION="shadow-rfdetr-preflight-v1"
ARG DEBIAN_FRONTEND=noninteractive

ENV ROS_DOMAIN_ID=0
ENV ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
ENV TASKPLANNER_WS=/workspaces/taskplanner_ws
ENV BTOPS_WS=/opt/btops_ws

RUN set -e; \
    for attempt in 1 2 3 4; do \
      rm -rf /var/lib/apt/lists/*; \
      if apt-get -o Acquire::Retries=5 update; then break; fi; \
      if [ "${attempt}" -eq 4 ]; then exit 1; fi; \
      sleep "$((attempt * 5))"; \
    done; \
    apt-get install -y --no-install-recommends \
    alsa-utils \
    bash-completion \
    build-essential \
    ca-certificates \
    curl \
    git \
    npm \
    libportaudio2 \
    pipewire-alsa \
    python3-matplotlib \
    python3-numpy \
    python3-opencv \
    openssh-client \
    python3-colcon-common-extensions \
    python3-jsonschema \
    python3-pil \
    python3-pip \
    python3-pytest \
    python3-requests \
    python3-rosdep \
    python3-twisted \
    python3-websockets \
    python3-yaml \
    python3-zmq \
    ros-jazzy-ament-cmake-mypy \
    ros-jazzy-rosbridge-suite \
    wireplumber; \
    rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04 does not publish python3-sounddevice. Pin the small ctypes
# wrapper while keeping PortAudio itself under apt lifecycle management.
RUN python3 -m pip install --break-system-packages --no-cache-dir \
    sounddevice==0.5.3

RUN mkdir -p "${BTOPS_WS}/src" \
    && git clone --filter=blob:none --no-checkout "${AUTO_APMS_REPO_URL}" "${BTOPS_WS}/src/auto_apms" \
    && git -C "${BTOPS_WS}/src/auto_apms" fetch --depth 1 origin "${AUTO_APMS_REF}" \
    && git -C "${BTOPS_WS}/src/auto_apms" checkout --detach FETCH_HEAD

COPY --from=btops_ws . ${BTOPS_WS}/src/btops_ws_src

RUN source /opt/ros/jazzy/setup.bash \
    && cd "${BTOPS_WS}" \
    && (rosdep update || true) \
    && apt-get update \
    && rosdep install --from-paths src --ignore-src -r -y --skip-keys "ament_python" \
    && rm -rf /var/lib/apt/lists/* \
    && colcon build --symlink-install --cmake-args -DBUILD_TESTING=OFF

ENV TASKPLANNER_IMAGE_VERSION=${TASKPLANNER_BUILD_VERSION}
ENV TASKPLANNER_IMAGE_GIT_SHA=${TASKPLANNER_BUILD_SHA}
ENV TASKPLANNER_SHADOW_CONTRACT_VERSION=${TASKPLANNER_SHADOW_CONTRACT_VERSION}

LABEL org.opencontainers.image.version="${TASKPLANNER_BUILD_VERSION}" \
      org.opencontainers.image.revision="${TASKPLANNER_BUILD_SHA}" \
      io.taskplanner.shadow.contract="${TASKPLANNER_SHADOW_CONTRACT_VERSION}"

RUN printf '%s\n' \
    "{\"image_version\":\"${TASKPLANNER_BUILD_VERSION}\",\"git_sha\":\"${TASKPLANNER_BUILD_SHA}\",\"shadow_contract\":\"${TASKPLANNER_SHADOW_CONTRACT_VERSION}\"}" \
    > /etc/taskplanner-build.json

WORKDIR ${TASKPLANNER_WS}

COPY docker/entrypoint.sh /usr/local/bin/taskplanner-entrypoint
RUN chmod +x /usr/local/bin/taskplanner-entrypoint

ENTRYPOINT ["/usr/local/bin/taskplanner-entrypoint"]
CMD ["bash"]
