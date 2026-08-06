from setuptools import find_packages, setup


package_name = "surgical_interop_execution"


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
    description="Adapter from internal commands to focused public robot capabilities.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "surgical_interop_execution_bridge = surgical_interop_execution.bridge:main",
            "fault_action_emulator = surgical_interop_execution.fault_action_emulator:main",
        ],
    },
)
