from glob import glob
import os

from setuptools import find_packages, setup


package_name = "integration_debug"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/config", glob(os.path.join("config", "*.yaml"))),
        (f"share/{package_name}/launch", glob(os.path.join("launch", "*.launch.py"))),
    ],
    install_requires=["setuptools"],
    tests_require=["pytest"],
    zip_safe=True,
    maintainer="Radon213",
    maintainer_email="Radon213@users.noreply.github.com",
    description="Scenario-free ROS integration Debug Mode for Taskplanner.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "integration_debug_node = integration_debug.node:main",
            "operational_asr_node = integration_debug.operational_asr_node:main",
            "taskplanner_asr_node = integration_debug.operational_asr_node:main",
            "secure_debug_rosbridge = integration_debug.secure_rosbridge:main",
            "secure_operational_debug_rosbridge = integration_debug.secure_rosbridge:operational_main",
            "secure_multicam_rosbridge = integration_debug.secure_multicam_rosbridge:main",
        ],
    },
)
