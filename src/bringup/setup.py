from setuptools import find_packages, setup

package_name = "bringup"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/launch", ["launch/taskplanner_mock.launch.py"]),
        (f"share/{package_name}/config", ["config/taskplanner.yaml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Codex",
    maintainer_email="codex@example.com",
    description="Bringup launch assets for taskplanner v1.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "taskplanner_smoke_test = bringup.smoke_test:main",
            "taskplanner_manual_probe = bringup.manual_probe:main",
            "taskplanner_bt_audit = bringup.bt_audit:main",
        ],
    },
)
