from glob import glob
import os

from setuptools import find_packages, setup


package_name = "retraction_control"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            [f"resource/{package_name}"],
        ),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/config", glob(os.path.join("config", "*.yaml"))),
        (f"share/{package_name}/launch", glob(os.path.join("launch", "*.launch.py"))),
    ],
    install_requires=["setuptools", "PyYAML>=6"],
    extras_require={"test": ["pytest"]},
    zip_safe=True,
    maintainer="Radon213",
    maintainer_email="Radon213@users.noreply.github.com",
    description=(
        "Native ROS 2 retraction controller with durable admission and "
        "hardware-injectable execution."
    ),
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "command_server_node = retraction_control.command_server_node:main",
        ],
    },
)
