#!/usr/bin/env bash
set -e

source /opt/ros/jazzy/setup.bash

if [ -f /opt/btops_ws/install/setup.bash ]; then
  source /opt/btops_ws/install/setup.bash
fi

if [[ "${TASKPLANNER_SKIP_WORKSPACE_SETUP:-false}" != "true" \
    && -f /workspaces/taskplanner_ws/install/docker/setup.bash ]]; then
  source /workspaces/taskplanner_ws/install/docker/setup.bash
fi

exec "$@"
