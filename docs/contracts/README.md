# ROS graph validation snapshots

Files in this directory are review and regression-test snapshots. They are not
runtime configuration and must never be imported by a launch file or node.

The runtime authority remains the launch file together with the node that
creates each publisher, subscription, service, or action. A snapshot records
that implemented graph in a declarative form so an intentional interface
change has one obvious review diff and an accidental change fails a
source-level test.

Within `public_interfaces`, `owner` is the publishing node for a Topic and the
server node for a Service or Action. Source-level tests separately witness the
corresponding publisher/server direction and required browser/runtime consumers.

When the graph changes intentionally:

1. change the runtime owner first;
2. review public, trusted-input, and isolated-virtual boundaries;
3. update the snapshot in the same change; and
4. run the source-level snapshot regression tests.

Do not read these files from production code, generate launch actions from
them, or use them as an admission or safety policy. That would create a second
runtime owner and allow the declaration to diverge from actual ROS behavior.
