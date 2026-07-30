from pathlib import Path

from setuptools import find_packages, setup


package_name = "procedure_spec"


def spec_data_files() -> list[tuple[str, list[str]]]:
    specs_root = Path("procedure_spec/specs")
    grouped: dict[Path, list[str]] = {}
    for path in sorted(specs_root.rglob("*.yaml")):
        destination = Path("share") / package_name / path.parent.relative_to("procedure_spec")
        grouped.setdefault(destination, []).append(str(path))
    return [(str(destination), files) for destination, files in sorted(grouped.items())]


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        *spec_data_files(),
    ],
    install_requires=["setuptools", "PyYAML"],
    tests_require=["pytest"],
    zip_safe=True,
    maintainer="Codex",
    maintainer_email="codex@example.com",
    description="Static surgical procedure specification loader and validator.",
    license="Apache-2.0",
)
