from setuptools import find_packages, setup

package_name = "vlm_node"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools", "requests", "Pillow"],
    zip_safe=True,
    maintainer="Codex",
    maintainer_email="codex@example.com",
    description="Mock VLM node for taskplanner v1.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "mock_vlm = vlm_node.node:main",
            "real_vlm = vlm_node.real_vlm:main",
            "synthetic_scene_camera = vlm_node.synthetic_scene_camera:main",
            "snapshot_bridge = vlm_node.snapshot_bridge:main",
        ],
    },
)
