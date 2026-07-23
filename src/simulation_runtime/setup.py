from setuptools import find_packages, setup

package_name = "simulation_runtime"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools", "requests"],
    zip_safe=True,
    maintainer="Codex",
    maintainer_email="codex@example.com",
    description="Simulation manager and mock surgeon runtime for taskplanner v1.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "simulation_manager = simulation_runtime.simulation_manager:main",
            "mock_surgeon = simulation_runtime.mock_surgeon:main",
            "surgeon_actor = simulation_runtime.surgeon_actor:main",
            "llm_surgeon_actor = simulation_runtime.llm_surgeon_actor:main",
            "llm_retrieval_choice_selector = simulation_runtime.llm_retrieval_choice_selector:main",
        ],
    },
)
