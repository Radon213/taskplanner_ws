"""Pinned executable-source manifest for hand-blood-tools commit 0f9e9311."""

from __future__ import annotations

UPSTREAM_MANIFEST_COMMIT = "0f9e93115b8cc1d470398c92e010e3fc6ef1de5d"

UPSTREAM_SOURCE_MANIFEST = {
    "components/blood_detection/offline_blood_segmentation.py": "839b8878710be9f15af495d52530b9208a753a5168276a34ebb5fe996c439a09",
    "components/hand_keypoints_ros/ros2_ws/src/hand_keypoint_ros/hand_keypoint_ros/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "components/hand_keypoints_ros/ros2_ws/src/hand_keypoint_ros/hand_keypoint_ros/core.py": "7e3363969856e7295a1e733beba75d69ff5605dd6bd27a6778c8c0aaf01d054b",
    "components/hand_keypoints_ros/ros2_ws/src/hand_keypoint_ros/hand_keypoint_ros/fake_camera_publisher.py": "643c94f07d1f7dd9923eb3d4a5210e835442fdd2e54029efdc65c1a42e18151b",
    "components/hand_keypoints_ros/ros2_ws/src/hand_keypoint_ros/hand_keypoint_ros/hand_detection_node.py": "adfc03f285fd6e6150c5c145ed1340de047a01058a9a87fe7002418ac36a2def",
    "components/tool_runtime_v1_6/algorithm/model/ontology.json": "d714dba3ca4911623a71127fca9619a21a230cbbf53e30d63564c2b9f6287a81",
    "components/tool_runtime_v1_6/algorithm/src/pnu_surgical_tool/__init__.py": "1057598729802b8774a48c55b30e62ca794f31127b3b89d11e490fe092c1fc4f",
    "components/tool_runtime_v1_6/algorithm/src/pnu_surgical_tool/api.py": "8f23a1f77ce804c8f88ade8218f8bf7962de1a18dc8b241f9a2e10abb1cf3c06",
    "components/tool_runtime_v1_6/algorithm/src/pnu_surgical_tool/depth_registration.py": "c8ed0178981c663c9662756623f871478afada11ea3461d16ff570dccda47282",
    "components/tool_runtime_v1_6/algorithm/src/pnu_surgical_tool/planar_pose.py": "8fbcbcfd8c5acc7db410939479810dcbbf85f7d5292d9de322d677efb66fe04f",
    "components/tool_runtime_v1_6/algorithm/src/pnu_surgical_tool/rfdetr_inference.py": "1b3da81467d57af5737c88a69e7145d62343aa0e66834597626df7574962679b",
    "components/tool_runtime_v1_6/algorithm/src/pnu_surgical_tool/rle.py": "d7a0d57552df213c4a69ea353290d042a734f0756cf20ba1b2f0bef41ee3097d",
    "components/tool_runtime_v1_6/algorithm/src/pnu_surgical_tool/types.py": "0f5e96dd98c0ac0119bd1b9a81e3f78b13c8709b5f96b1aac1ddc7daa5f7594b",
    "components/tool_runtime_v1_6/algorithm/src/pnu_surgical_tool/visualization.py": "6accbfa679a53454805506cf46c4900c0c46b3a10772ae6c2b2690f4b7143794",
}

UPSTREAM_EXECUTABLE_ROOTS = (
    "components/blood_detection",
    "components/hand_keypoints_ros/ros2_ws/src/hand_keypoint_ros/hand_keypoint_ros",
    "components/tool_runtime_v1_6/algorithm/src/pnu_surgical_tool",
)
