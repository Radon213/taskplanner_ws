from setuptools import find_packages, setup


package_name = "voice_command"


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
    maintainer="Taskplanner maintainers",
    maintainer_email="codex@example.invalid",
    description="Proposal-only natural-language voice intent resolution for Taskplanner.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "voice_intent_resolver = voice_command.node:main",
        ],
    },
)
