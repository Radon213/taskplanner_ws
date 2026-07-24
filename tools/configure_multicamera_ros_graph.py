#!/usr/bin/env python3
"""Create four uniquely named USD cameras and ROS 2 camera Action Graphs."""

from __future__ import annotations

import argparse
from pathlib import Path

from pxr import Sdf, Usd, UsdGeom


CAMERA_ROOT = Sdf.Path(
    "/World/Surgical_Room_Layout/Surgical_Room_Layout/Camera_Pos"
)
SOURCE_GRAPH = Sdf.Path("/Graph/ROS_Camera")

CAMERAS = (
    {
        "rig": "IntelRealsense_D435",
        "name": "field_camera_01",
        "graph": "ROS_FieldCamera01",
        "base_topic": "/surgery/images/field",
    },
    {
        "rig": "IntelRealsense_D435_01",
        "name": "field_camera_02",
        "graph": "ROS_FieldCamera02",
        "base_topic": "/surgery/images/field/camera_02",
    },
    {
        "rig": "IntelRealsense_D435_02",
        "name": "field_camera_03",
        "graph": "ROS_FieldCamera03",
        "base_topic": "/surgery/images/field/camera_03",
    },
    {
        "rig": "IntelRealsense_D435_03",
        "name": "field_camera_04",
        "graph": "ROS_FieldCamera04",
        "base_topic": "/surgery/images/field/camera_04",
    },
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--secondary-width", type=int)
    parser.add_argument("--secondary-height", type=int)
    parser.add_argument("--enable-depth", action="store_true")
    parser.add_argument("--enable-pointcloud", action="store_true")
    parser.add_argument("--rgb-frame-skip-count", type=int, default=0)
    parser.add_argument("--depth-frame-skip-count", type=int, default=0)
    parser.add_argument("--pointcloud-frame-skip-count", type=int, default=5)
    return parser.parse_args()


def replace_connection_prefix(prim: Usd.Prim, old: Sdf.Path, new: Sdf.Path) -> None:
    for attr in prim.GetAttributes():
        connections = attr.GetConnections()
        if connections:
            attr.SetConnections([path.ReplacePrefix(old, new) for path in connections])


def set_attr(prim: Usd.Prim, name: str, value) -> None:
    attr = prim.GetAttribute(name)
    if not attr:
        raise RuntimeError(f"Missing {name} on {prim.GetPath()}")
    attr.Set(value)


def main() -> None:
    args = parse_args()
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if source == output:
        raise SystemExit("Refusing to overwrite the source stage")

    source_layer = Sdf.Layer.FindOrOpen(str(source))
    if source_layer is None:
        raise SystemExit(f"Unable to open source layer: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not source_layer.Export(str(output)):
        raise SystemExit(f"Unable to export stage copy: {output}")

    layer = Sdf.Layer.FindOrOpen(str(output))
    if layer is None or layer.GetPrimAtPath(SOURCE_GRAPH) is None:
        raise SystemExit(f"Source ROS camera graph not found: {SOURCE_GRAPH}")

    old_camera_paths = []
    new_camera_paths = []
    new_graph_paths = []
    for item in CAMERAS:
        rig_path = CAMERA_ROOT.AppendChild(item["rig"])
        old_camera = rig_path.AppendChild("Camera")
        new_camera = rig_path.AppendChild(item["name"])
        new_graph = Sdf.Path("/Graph").AppendChild(item["graph"])
        if layer.GetPrimAtPath(old_camera) is None:
            raise SystemExit(f"Camera spec not found: {old_camera}")
        if not Sdf.CopySpec(layer, old_camera, layer, new_camera):
            raise SystemExit(f"Unable to copy camera to {new_camera}")
        if not Sdf.CopySpec(layer, SOURCE_GRAPH, layer, new_graph):
            raise SystemExit(f"Unable to copy graph to {new_graph}")
        old_camera_paths.append(old_camera)
        new_camera_paths.append(new_camera)
        new_graph_paths.append(new_graph)
    layer.Save()

    stage = Usd.Stage.Open(str(output))
    if stage is None:
        raise SystemExit(f"Unable to open output stage: {output}")

    for camera_index, (item, camera_path, graph_path) in enumerate(
        zip(CAMERAS, new_camera_paths, new_graph_paths)
    ):
        camera_prim = stage.GetPrimAtPath(camera_path)
        if not camera_prim or not camera_prim.IsA(UsdGeom.Camera):
            raise RuntimeError(f"Invalid copied camera: {camera_path}")
        camera_prim.SetDisplayName(item["name"])

        graph_prim = stage.GetPrimAtPath(graph_path)
        if not graph_prim:
            raise RuntimeError(f"Invalid copied graph: {graph_path}")
        for prim in Usd.PrimRange.AllPrims(graph_prim):
            replace_connection_prefix(prim, SOURCE_GRAPH, graph_path)

        render = stage.GetPrimAtPath(graph_path.AppendChild("RenderProduct"))
        render.GetRelationship("inputs:cameraPrim").SetTargets([camera_path])
        width = (
            args.secondary_width
            if camera_index > 0 and args.secondary_width is not None
            else args.width
        )
        height = (
            args.secondary_height
            if camera_index > 0 and args.secondary_height is not None
            else args.height
        )
        set_attr(render, "inputs:width", width)
        set_attr(render, "inputs:height", height)

        frame_id = f'{item["name"]}_optical_frame'
        rgb = stage.GetPrimAtPath(graph_path.AppendChild("RGBPublish"))
        info = stage.GetPrimAtPath(graph_path.AppendChild("CameraInfoPublish"))
        depth = stage.GetPrimAtPath(graph_path.AppendChild("DepthPublish"))
        pcl = stage.GetPrimAtPath(graph_path.AppendChild("DepthPclPublish"))

        for publisher in (rgb, info, depth, pcl):
            set_attr(publisher, "inputs:frameId", frame_id)
        set_attr(rgb, "inputs:topicName", f'{item["base_topic"]}/image_raw')
        set_attr(info, "inputs:topicName", f'{item["base_topic"]}/camera_info')
        set_attr(depth, "inputs:topicName", f'{item["base_topic"]}/depth')
        set_attr(pcl, "inputs:topicName", f'{item["base_topic"]}/depth/points')

        # Keep each modality independently rate-controlled so the real-time
        # stream can remain stable under laptop GPU and DDS bandwidth limits.
        set_attr(rgb, "inputs:enabled", True)
        set_attr(info, "inputs:enabled", True)
        set_attr(depth, "inputs:enabled", args.enable_depth)
        set_attr(pcl, "inputs:enabled", args.enable_pointcloud)
        set_attr(rgb, "inputs:frameSkipCount", args.rgb_frame_skip_count)
        set_attr(info, "inputs:frameSkipCount", args.rgb_frame_skip_count)
        set_attr(depth, "inputs:frameSkipCount", args.depth_frame_skip_count)
        set_attr(
            pcl, "inputs:frameSkipCount", args.pointcloud_frame_skip_count
        )

    for old_camera in old_camera_paths:
        stage.RemovePrim(old_camera)
    stage.RemovePrim(SOURCE_GRAPH)
    stage.GetRootLayer().Save()

    reopened = Usd.Stage.Open(str(output))
    print(f"output={output}")
    print(f"resolution={args.width}x{args.height}")
    for item, camera_path, graph_path in zip(
        CAMERAS, new_camera_paths, new_graph_paths
    ):
        render = reopened.GetPrimAtPath(graph_path.AppendChild("RenderProduct"))
        rgb = reopened.GetPrimAtPath(graph_path.AppendChild("RGBPublish"))
        info = reopened.GetPrimAtPath(graph_path.AppendChild("CameraInfoPublish"))
        depth = reopened.GetPrimAtPath(graph_path.AppendChild("DepthPublish"))
        print(f"camera={camera_path}")
        print(f"  graph={graph_path}")
        print(f"  target={render.GetRelationship('inputs:cameraPrim').GetTargets()}")
        print(f"  rgb={rgb.GetAttribute('inputs:topicName').Get()}")
        print(f"  info={info.GetAttribute('inputs:topicName').Get()}")
        print(f"  frame_id={rgb.GetAttribute('inputs:frameId').Get()}")
        print(
            f"  resolution={render.GetAttribute('inputs:width').Get()}x"
            f"{render.GetAttribute('inputs:height').Get()}"
        )
        print(f"  depth_enabled={depth.GetAttribute('inputs:enabled').Get()}")
        print(f"  pointcloud_enabled={pcl.GetAttribute('inputs:enabled').Get()}")
        print(
            "  frame_skip="
            f"rgb:{rgb.GetAttribute('inputs:frameSkipCount').Get()},"
            f"depth:{depth.GetAttribute('inputs:frameSkipCount').Get()},"
            f"pcl:{pcl.GetAttribute('inputs:frameSkipCount').Get()}"
        )


if __name__ == "__main__":
    main()
