from setuptools import find_packages, setup

package_name = "bt_orchestrator"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools"],
    tests_require=["pytest"],
    zip_safe=True,
    maintainer="Codex",
    maintainer_email="codex@example.com",
    description="Decision bridge package for taskplanner v1.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "bed_robot_arm_group_orchestrator = bt_orchestrator.bed_robot_arm_group_orchestrator:main",
            "decision_bridge = bt_orchestrator.node:main",
        ],
    },
)
