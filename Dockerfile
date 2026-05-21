# syntax=docker/dockerfile:1.7
FROM ros:jazzy-ros-base

SHELL ["/bin/bash", "-c"]

ARG BTOPS_REPO_URL=""
ARG BTOPS_REF="main"
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

RUN if [ -z "${BTOPS_REPO_URL}" ]; then \
      echo >&2 "ERROR: BTOPS_REPO_URL build arg is required. Example: docker compose build --ssh default --build-arg BTOPS_REPO_URL=<repo-url>"; \
      exit 2; \
    fi

RUN mkdir -p /root/.ssh \
    && ssh-keyscan github.com >> /root/.ssh/known_hosts

RUN --mount=type=ssh mkdir -p "${BTOPS_WS}/src" \
    && git clone --depth 1 --branch "${BTOPS_REF}" "${BTOPS_REPO_URL}" "${BTOPS_WS}/src/btops_ws_src"

RUN source /opt/ros/jazzy/setup.bash \
    && cd "${BTOPS_WS}" \
    && rosdep update || true \
    && rosdep install --from-paths src --ignore-src -r -y \
    && colcon build --symlink-install

WORKDIR ${TASKPLANNER_WS}

COPY docker/entrypoint.sh /usr/local/bin/taskplanner-entrypoint
RUN chmod +x /usr/local/bin/taskplanner-entrypoint

ENTRYPOINT ["/usr/local/bin/taskplanner-entrypoint"]
CMD ["bash"]
