#!/usr/bin/env python3
"""Create a stable static-layout copy of an imported Onshape USD stage."""

from __future__ import annotations

import argparse
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics


DEFAULT_CAMERA_PATH = (
    "/World/Surgical_Room_Layout/Surgical_Room_Layout/Camera_Pos/"
    "IntelRealsense_D435/Camera"
)
DEFAULT_CAMERA_REFERENCE_PART = (
    "/World/Surgical_Room_Layout/Surgical_Room_Layout/Camera_Pos/"
    "IntelRealsense_D435/Part_1"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--camera-path", default=DEFAULT_CAMERA_PATH)
    parser.add_argument(
        "--camera-reference-part", default=DEFAULT_CAMERA_REFERENCE_PART
    )
    parser.add_argument(
        "--camera-target",
        type=float,
        nargs=3,
        metavar=("X", "Y", "Z"),
        default=(-0.436, -0.181, 0.95),
        help="World-space point that the USD camera should look at.",
    )
    return parser.parse_args()


def set_xform_op(xformable: UsdGeom.Xformable, op_type, value) -> None:
    for op in xformable.GetOrderedXformOps():
        if op.GetOpType() == op_type:
            op.Set(value)
            return
    if op_type == UsdGeom.XformOp.TypeTranslate:
        xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble).Set(value)
    elif op_type == UsdGeom.XformOp.TypeOrient:
        xformable.AddOrientOp(UsdGeom.XformOp.PrecisionDouble).Set(value)
    elif op_type == UsdGeom.XformOp.TypeScale:
        xformable.AddScaleOp(UsdGeom.XformOp.PrecisionDouble).Set(value)
    else:
        raise RuntimeError(f"Unsupported transform op {op_type}")


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

    stage = Usd.Stage.Open(str(output))
    if stage is None:
        raise SystemExit(f"Unable to open stage copy: {output}")

    rigid_body_count = 0
    collision_count = 0
    mass_count = 0
    joint_count = 0
    articulation_count = 0
    layout_path = Sdf.Path(
        "/World/Surgical_Room_Layout/Surgical_Room_Layout"
    )
    for prim in list(stage.TraverseAll()):
        if not prim.GetPath().HasPrefix(layout_path):
            continue
        if prim.HasAPI(UsdPhysics.RigidBodyAPI):
            prim.RemoveAPI(UsdPhysics.RigidBodyAPI)
            rigid_body_count += 1
        if prim.HasAPI(UsdPhysics.CollisionAPI):
            prim.RemoveAPI(UsdPhysics.CollisionAPI)
            collision_count += 1
        if prim.HasAPI(UsdPhysics.MassAPI):
            prim.RemoveAPI(UsdPhysics.MassAPI)
            mass_count += 1
        if prim.HasAPI(UsdPhysics.ArticulationRootAPI):
            prim.RemoveAPI(UsdPhysics.ArticulationRootAPI)
            articulation_count += 1
        if prim.IsA(UsdPhysics.Joint):
            prim.SetActive(False)
            joint_count += 1

    primary_camera_prim = stage.GetPrimAtPath(args.camera_path)
    if not primary_camera_prim or not primary_camera_prim.IsA(UsdGeom.Camera):
        raise SystemExit(f"Camera prim not found: {args.camera_path}")
    primary_camera = UsdGeom.Camera(primary_camera_prim)
    camera_pos_root = primary_camera_prim.GetParent().GetParent()
    camera_target = Gf.Vec3d(*args.camera_target)
    camera_results = []
    for rig_prim in camera_pos_root.GetChildren():
        if not rig_prim.GetName().startswith("IntelRealsense_D435"):
            continue
        reference_prim = rig_prim.GetChild("Part_1")
        if not reference_prim or not UsdGeom.Xformable(reference_prim):
            continue

        camera_path = rig_prim.GetPath().AppendChild("Camera")
        camera = UsdGeom.Camera.Define(stage, camera_path)
        camera.GetFocalLengthAttr().Set(primary_camera.GetFocalLengthAttr().Get())
        camera.GetHorizontalApertureAttr().Set(
            primary_camera.GetHorizontalApertureAttr().Get()
        )
        camera.GetVerticalApertureAttr().Set(
            primary_camera.GetVerticalApertureAttr().Get()
        )
        camera.GetClippingRangeAttr().Set(primary_camera.GetClippingRangeAttr().Get())

        # A USD camera looks along local -Z with local +Y as up. CAD camera-part
        # axes do not guarantee that optical convention, so aim each sensor
        # explicitly at the shared surgical field.
        reference_world = UsdGeom.Xformable(
            reference_prim
        ).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        camera_position = reference_world.ExtractTranslation()
        if (camera_target - camera_position).GetLength() < 1e-6:
            raise SystemExit("Camera position and target must differ")
        camera_world = Gf.Matrix4d().SetLookAt(
            camera_position, camera_target, Gf.Vec3d(0, 0, 1)
        ).GetInverse()
        parent_world = UsdGeom.Xformable(rig_prim).ComputeLocalToWorldTransform(
            Usd.TimeCode.Default()
        )
        camera_local = camera_world * parent_world.GetInverse()

        camera_xform = UsdGeom.Xformable(camera.GetPrim())
        set_xform_op(
            camera_xform,
            UsdGeom.XformOp.TypeTranslate,
            camera_local.ExtractTranslation(),
        )
        set_xform_op(
            camera_xform,
            UsdGeom.XformOp.TypeOrient,
            camera_local.ExtractRotationQuat(),
        )
        set_xform_op(
            camera_xform, UsdGeom.XformOp.TypeScale, Gf.Vec3d(1.0, 1.0, 1.0)
        )
        camera_results.append((camera_path, camera_xform))

    stage.GetRootLayer().Save()

    print(f"output={output}")
    print(f"removed_rigid_body_apis={rigid_body_count}")
    print(f"removed_collision_apis={collision_count}")
    print(f"removed_mass_apis={mass_count}")
    print(f"disabled_joints={joint_count}")
    print(f"removed_articulation_roots={articulation_count}")
    print(f"configured_cameras={len(camera_results)}")
    for camera_path, camera_xform in camera_results:
        world_transform = camera_xform.ComputeLocalToWorldTransform(
            Usd.TimeCode.Default()
        )
        forward = world_transform.TransformDir(Gf.Vec3d(0, 0, -1)).GetNormalized()
        print(f"camera={camera_path}")
        print(f"  world_position={world_transform.ExtractTranslation()}")
        print(f"  forward_world={forward}")


if __name__ == "__main__":
    main()
