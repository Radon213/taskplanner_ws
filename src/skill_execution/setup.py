from setuptools import find_packages, setup

package_name = "skill_execution"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Codex",
    maintainer_email="codex@example.com",
    description="Action bridge and mock skill server for taskplanner v1.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "skill_action_bridge = skill_execution.bridge:main",
            "mock_skill_server = skill_execution.mock_server:main",
        ],
    },
)
