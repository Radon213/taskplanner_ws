from setuptools import find_packages, setup


package_name = "tts_runtime"


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
    description="Durable one-shot Supertonic playback runtime for Taskplanner.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "tts_runtime_node = tts_runtime.node:main",
        ],
    },
)
