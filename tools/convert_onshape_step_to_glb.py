#!/usr/bin/env python3
"""Convert the Onshape humanoid/tray STEP assembly to a tag1-anchored GLB.

The source STEP is imported through OpenCascade XCAF so assembly names and
display colours survive the conversion.  The GLB origin is the centre of the
tag face.  Its source coordinate basis follows world_anchor_node.py:

* +X: right when viewing the tag
* +Y: up when viewing the tag
* +Z: out of the tag toward the viewer

OpenCascade first converts the CAD Z-up basis to glTF's standard Y-up basis.
The generated scene root then rotates and translates the complete assembly
into the ROS tag1 basis, so a renderer can attach it directly to tag1.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import time
from pathlib import Path

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.Message import Message_ProgressRange
from OCP.RWGltf import RWGltf_CafWriter, RWGltf_WriterTrsfFormat
from OCP.RWMesh import RWMesh_CoordinateSystem, RWMesh_CoordinateSystemConverter
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TColStd import TColStd_IndexedDataMapOfStringString
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--linear-deflection-mm",
        type=float,
        default=1.25,
        help="absolute tessellation tolerance in OpenCascade millimetres",
    )
    parser.add_argument(
        "--angular-deflection-rad",
        type=float,
        default=0.35,
        help="angular tessellation tolerance in radians",
    )
    return parser.parse_args()


def label_name(label: TDF_Label) -> str:
    name = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), name):
        return name.Get().ToExtString()
    return ""


def shape_bounds(label: TDF_Label) -> tuple[float, float, float, float, float, float]:
    shape = XCAFDoc_ShapeTool.GetShape_s(label)
    bounds = Bnd_Box()
    BRepBndLib.Add_s(shape, bounds, False)
    if bounds.IsVoid():
        raise RuntimeError(f"shape has no finite bounds: {label_name(label)!r}")
    return bounds.Get()


def referred_label(label: TDF_Label) -> TDF_Label:
    if not XCAFDoc_ShapeTool.IsComponent_s(label):
        return label
    referred = TDF_Label()
    if XCAFDoc_ShapeTool.GetReferredShape_s(label, referred):
        return referred
    return label


def find_named_component(root: TDF_Label, wanted: str) -> TDF_Label:
    pending = [root]
    while pending:
        label = pending.pop()
        referred = referred_label(label)
        names = {label_name(label).casefold(), label_name(referred).casefold()}
        if wanted.casefold() in names:
            return label
        components = TDF_LabelSequence()
        if XCAFDoc_ShapeTool.GetComponents_s(referred, components, False):
            pending.extend(components.Value(index) for index in range(1, components.Length() + 1))
    raise RuntimeError(f"could not find {wanted!r} in the STEP assembly")


def centre(bounds: tuple[float, float, float, float, float, float]) -> tuple[float, float, float]:
    xmin, ymin, zmin, xmax, ymax, zmax = bounds
    return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0, (zmin + zmax) / 2.0)


def apply_root_transform_to_binary_gltf(
    path: Path,
    translation: tuple[float, float, float],
    rotation: tuple[float, float, float, float],
) -> None:
    """Put the exported CAD assembly into the ROS tag1 coordinate system.

    OCCT exports CAD Z-up coordinates to glTF as ``(cad_x, cad_z, -cad_y)``.
    A -90 degree Y rotation therefore produces ``(cad_y, cad_z, cad_x)``,
    exactly the tag convention used by ``world_anchor_node.py``.  The root
    translation then moves the measured centre of the printed tag face to the
    origin.  Geometry remains in metres; no compensating scale is required.
    """
    payload = path.read_bytes()
    if len(payload) < 20:
        raise RuntimeError("writer produced a truncated GLB")
    magic, version, _ = struct.unpack_from("<4sII", payload, 0)
    if magic != b"glTF" or version != 2:
        raise RuntimeError("writer did not produce a GLB v2 file")
    offset = 12
    chunks: list[tuple[int, bytes]] = []
    while offset < len(payload):
        length, chunk_type = struct.unpack_from("<II", payload, offset)
        offset += 8
        chunks.append((chunk_type, payload[offset : offset + length]))
        offset += length
    if not chunks or chunks[0][0] != 0x4E4F534A:
        raise RuntimeError("GLB has no leading JSON chunk")
    document = json.loads(chunks[0][1].rstrip(b" \x00"))
    scene_index = int(document.get("scene", 0))
    root_nodes = document.get("scenes", [])[scene_index].get("nodes", [])
    if len(root_nodes) != 1:
        raise RuntimeError(f"expected one glTF scene root, found {len(root_nodes)}")
    root = document["nodes"][int(root_nodes[0])]
    if any(key in root for key in ("matrix", "translation", "rotation", "scale")):
        raise RuntimeError("cannot safely replace a transformed glTF scene root")
    root["translation"] = list(translation)
    root["rotation"] = list(rotation)
    json_chunk = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    rewritten = [(0x4E4F534A, json_chunk), *chunks[1:]]
    total = 12 + sum(8 + len(chunk) for _, chunk in rewritten)
    output = bytearray(struct.pack("<4sII", b"glTF", 2, total))
    for chunk_type, chunk in rewritten:
        output.extend(struct.pack("<II", len(chunk), chunk_type))
        output.extend(chunk)
    path.write_bytes(output)


def main() -> None:
    args = parse_args()
    if args.linear_deflection_mm <= 0 or args.angular_deflection_rad <= 0:
        raise SystemExit("tessellation deflections must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    application = XCAFApp_Application.GetApplication_s()
    document = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    application.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), document)

    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    reader.SetLayerMode(True)
    reader.SetPropsMode(True)
    if not reader.Perform(str(args.source), document):
        raise RuntimeError(f"failed to import STEP: {args.source}")

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    roots = TDF_LabelSequence()
    shape_tool.GetFreeShapes(roots)
    if roots.Length() != 1:
        raise RuntimeError(f"expected one assembly root, found {roots.Length()}")
    root = roots.Value(1)

    tag_white = find_named_component(root, "tag_white")
    tag_black = find_named_component(root, "tag_black")
    white_bounds = shape_bounds(tag_white)
    black_bounds = shape_bounds(tag_black)
    black_sizes = (
        black_bounds[3] - black_bounds[0],
        black_bounds[4] - black_bounds[1],
        black_bounds[5] - black_bounds[2],
    )
    normal_axis = min(range(3), key=black_sizes.__getitem__)
    if normal_axis != 0:
        raise RuntimeError(
            "this assembly no longer has its tag normal along CAD +X; "
            f"measured black-tag extents are {black_sizes} mm"
        )
    if not math.isclose(white_bounds[3], black_bounds[3], abs_tol=0.05):
        raise RuntimeError("the visible tag face is no longer the shared CAD +X face")

    _, tag_centre_y, tag_centre_z = centre(white_bounds)
    tag_face_x = max(white_bounds[3], black_bounds[3])
    tag_centre_mm = (tag_face_x, tag_centre_y, tag_centre_z)

    root_shape = XCAFDoc_ShapeTool.GetShape_s(root)
    mesh = BRepMesh_IncrementalMesh(
        root_shape,
        args.linear_deflection_mm,
        False,
        args.angular_deflection_rad,
        True,
    )
    if not mesh.IsDone():
        raise RuntimeError(f"OpenCascade tessellation failed, flags={mesh.GetStatusFlags()}")

    writer = RWGltf_CafWriter(TCollection_AsciiString(str(args.output)), True)
    writer.SetParallel(True)
    writer.SetMergeFaces(True)
    writer.SetToEmbedTexturesInGlb(True)
    writer.SetTransformationFormat(RWGltf_WriterTrsfFormat.RWGltf_WriterTrsfFormat_TRS)

    # Export the CAD assembly in metres using OCCT's standard Z-up -> glTF
    # conversion.  The tag-relative rigid transform is applied to the scene
    # root after export so translations and tessellated vertices share units.
    converter = RWMesh_CoordinateSystemConverter()
    converter.SetInputCoordinateSystem(RWMesh_CoordinateSystem.RWMesh_CoordinateSystem_Zup)
    converter.SetOutputCoordinateSystem(RWMesh_CoordinateSystem.RWMesh_CoordinateSystem_glTF)
    converter.SetInputLengthUnit(0.001)  # OCCT stores imported STEP geometry in mm.
    converter.SetOutputLengthUnit(1.0)  # glTF coordinates are metres.
    writer.SetCoordinateSystemConverter(converter)

    metadata = TColStd_IndexedDataMapOfStringString()
    metadata.Add(
        TCollection_AsciiString("Source"),
        TCollection_AsciiString("Onshape ARPA-H / humanoid+tray+tag1"),
    )
    metadata.Add(
        TCollection_AsciiString("Anchor"),
        TCollection_AsciiString("tag1 centre; x right, y up, z toward viewer"),
    )
    if not writer.Perform(document, metadata, Message_ProgressRange()):
        raise RuntimeError(f"failed to write GLB: {args.output}")
    tag_centre_m = tuple(value / 1000.0 for value in tag_centre_mm)
    root_translation = (-tag_centre_m[1], -tag_centre_m[2], -tag_centre_m[0])
    half_turn = math.sqrt(0.5)
    root_rotation = (0.0, -half_turn, 0.0, half_turn)  # -90 degrees about glTF +Y
    apply_root_transform_to_binary_gltf(args.output, root_translation, root_rotation)

    report = {
        "source": str(args.source),
        "output": str(args.output),
        "root": label_name(root),
        "sourceUnits": "millimetres in OpenCascade (STEP declares metres)",
        "outputUnits": "metres",
        "tagCentreCadMm": tag_centre_mm,
        "tagBlackBoundsCadMm": black_bounds,
        "tagWhiteBoundsCadMm": white_bounds,
        "tagBasisInCad": {
            "x": [0.0, 1.0, 0.0],
            "y": [0.0, 0.0, 1.0],
            "z": [1.0, 0.0, 0.0],
        },
        "glTfStorageCoordinateSystem": "standard Y-up before scene-root transform",
        "sceneCoordinateSystem": "ROS tag1: x right, y up, z toward viewer",
        "sceneRootTranslationM": root_translation,
        "sceneRootRotationXyzw": root_rotation,
        "linearDeflectionMm": args.linear_deflection_mm,
        "angularDeflectionRad": args.angular_deflection_rad,
        "elapsedSeconds": round(time.monotonic() - started, 3),
    }
    report_path = args.output.with_suffix(".anchor.json")
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
