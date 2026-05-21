# syntax=docker/dockerfile:1.7
FROM ros:jazzy-ros-base

SHELL ["/bin/bash", "-c"]

ARG BTOPS_REF="main"
ARG BTOPS_LOCAL_CONTEXT="../btops_ws"
ARG AUTO_APMS_REPO_URL="https://github.com/AutoAPMS/auto-apms.git"
ARG AUTO_APMS_REF="1.5.1"
ARG DEBIAN_FRONTEND=noninteractive

ENV ROS_DOMAIN_ID=0
ENV ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
ENV TASKPLANNER_WS=/workspaces/taskplanner_ws
ENV BTOPS_WS=/opt/btops_ws

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash-completion \
    build-essential \
    ca-certificates \
    curl \
    git \
    npm \
    openssh-client \
    python3-colcon-common-extensions \
    python3-pip \
    python3-rosdep \
    python3-yaml \
    ros-jazzy-rosbridge-suite \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p "${BTOPS_WS}/src" \
    && git clone --depth 1 --branch "${AUTO_APMS_REF}" "${AUTO_APMS_REPO_URL}" "${BTOPS_WS}/src/auto_apms" \
    && true

COPY --from=btops_ws . ${BTOPS_WS}/src/btops_ws_src

RUN source /opt/ros/jazzy/setup.bash \
    && cd "${BTOPS_WS}" \
    && (rosdep update || true) \
    && apt-get update \
    && rosdep install --from-paths src --ignore-src -r -y --skip-keys "ament_python" \
    && rm -rf /var/lib/apt/lists/* \
    && colcon build --symlink-install --cmake-args -DBUILD_TESTING=OFF

WORKDIR ${TASKPLANNER_WS}

COPY docker/entrypoint.sh /usr/local/bin/taskplanner-entrypoint
RUN chmod +x /usr/local/bin/taskplanner-entrypoint

ENTRYPOINT ["/usr/local/bin/taskplanner-entrypoint"]
CMD ["bash"]
