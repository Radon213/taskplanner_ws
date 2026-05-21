from setuptools import find_packages, setup

package_name = "procedure_spec"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (
            f"share/{package_name}/specs",
            [
                "procedure_spec/specs/display_catalog.yaml",
            ],
        ),
        (
            f"share/{package_name}/specs/thyroidectomy",
            [
                "procedure_spec/specs/thyroidectomy/procedure.yaml",
                "procedure_spec/specs/thyroidectomy/instruments.yaml",
                "procedure_spec/specs/thyroidectomy/mock_perception.yaml",
                "procedure_spec/specs/thyroidectomy/mock_surgeon.yaml",
                "procedure_spec/specs/thyroidectomy/scene_layout.yaml",
                "procedure_spec/specs/thyroidectomy/simulation_layout.yaml",
                "procedure_spec/specs/thyroidectomy/policy.yaml",
            ],
        ),
        (
            f"share/{package_name}/specs/nephrectomy",
            [
                "procedure_spec/specs/nephrectomy/procedure.yaml",
                "procedure_spec/specs/nephrectomy/instruments.yaml",
                "procedure_spec/specs/nephrectomy/mock_perception.yaml",
                "procedure_spec/specs/nephrectomy/mock_surgeon.yaml",
                "procedure_spec/specs/nephrectomy/scene_layout.yaml",
                "procedure_spec/specs/nephrectomy/simulation_layout.yaml",
                "procedure_spec/specs/nephrectomy/policy.yaml",
            ],
        ),
    ],
    install_requires=["setuptools", "PyYAML"],
    zip_safe=True,
    maintainer="Codex",
    maintainer_email="codex@example.com",
    description="Static surgical procedure specification loader and validator.",
    license="Apache-2.0",
)
