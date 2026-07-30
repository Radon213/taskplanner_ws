"""Observable surgical-tool event annotation utilities.

This package is deliberately isolated from the Taskplanner runtime.  In
particular, it does not publish or subscribe to VLM, reducer, BT, or skill
topics.  Ground-truth messages are written only into derived bags.
"""

SCHEMA_ID = "taskplanner.observable_tool_event.v1"
MANIFEST_TOPIC = "/evaluation/ground_truth/annotation_manifest"
EVENT_TOPIC = "/evaluation/ground_truth/tool_events"
