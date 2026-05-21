#!/usr/bin/env bash
set -e

source /opt/ros/jazzy/setup.bash

if [ -f /opt/btops_ws/install/setup.bash ]; then
  source /opt/btops_ws/install/setup.bash
fi

if [ -f /workspaces/taskplanner_ws/install/setup.bash ]; then
  source /workspaces/taskplanner_ws/install/setup.bash
fi

exec "$@"
