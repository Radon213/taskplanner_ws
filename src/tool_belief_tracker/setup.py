from glob import glob
from setuptools import find_packages, setup


package_name = "tool_belief_tracker"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml", "README.md"]),
        (f"share/{package_name}/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools", "PyYAML"],
    tests_require=["pytest"],
    zip_safe=True,
    maintainer="ARPA-H Taskplanner Team",
    maintainer_email="Radon213@users.noreply.github.com",
    description="Scenario-bounded semantic tool-location belief tracker.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "tool_belief_tracker_node = tool_belief_tracker.node:main",
            "tool_belief_tune = tool_belief_tracker.tune:main",
        ],
    },
)
