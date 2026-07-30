from setuptools import find_packages, setup


package_name = "shadow_evaluation"

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
    description="Strict shadow replay recorders, adapters, and evaluation-only sinks.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "recorded_transcript_adapter = shadow_evaluation.recorded_transcript_adapter:main",
            "interactive_replay_controller = shadow_evaluation.interactive_replay_controller:main",
            "reference_reconciler = shadow_evaluation.reference_reconciler:main",
            "shadow_skill_sink = shadow_evaluation.shadow_skill_sink:main",
            "shadow_trace_recorder = shadow_evaluation.trace_recorder:main",
        ],
    },
)
