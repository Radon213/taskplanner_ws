#!/usr/bin/env python3
"""Compute scoped ROS build plans.

The launcher owns transition authority.  This helper is deliberately limited
to the slow ABI/IDL build path; Python source and configuration reload through
their owning process and must not turn a warm restart into a workspace census.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET


MODES = frozenset({"live", "llm-surgeon", "replay", "debug"})
PACKAGE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "install",
        "log",
        "node_modules",
        "test",
        "tests",
        "venv",
    }
)
BUILD_CONTRACT_NAMES = frozenset(
    {"CMakeLists.txt", "package.xml", "setup.cfg", "setup.py"}
)
BUILD_CONTRACT_SUFFIXES = frozenset(
    {
        ".action",
        ".c",
        ".cc",
        ".cmake",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
        ".idl",
        ".msg",
        ".srv",
    }
)


def _installed_artifact_is_file(path: Path, *, root: Path) -> bool:
    """Accept host files and container-mount symlinks, but reject broken links.

    The Docker overlay is bind-mounted at ``/workspaces/taskplanner_ws``.
    ``colcon --symlink-install`` therefore writes absolute links that are valid
    in the builder/runtime container but look broken from the host launcher.
    Resolve only that reviewed mount prefix back into the current checkout;
    arbitrary dangling links remain invalid.
    """

    current = path
    visited: set[Path] = set()
    mount_prefix = "/workspaces/taskplanner_ws/"
    while True:
        if current.is_file():
            return True
        if not current.is_symlink() or current in visited:
            return False
        visited.add(current)
        target = os.readlink(current)
        if target.startswith(mount_prefix):
            current = root / target[len(mount_prefix) :]
        elif os.path.isabs(target):
            # Keep real system-owned links usable, while still rejecting a
            # dangling target instead of treating the link itself as proof.
            current = Path(target)
        else:
            current = current.parent / target


def _has_colcon_ignore_ancestor(path: Path, *, source_root: Path) -> bool:
    """Return whether ``path`` is under a COLCON_IGNORE-marked directory.

    A ROS checkout commonly places vendored or optional packages below a
    directory with ``COLCON_IGNORE``.  Checking only the pathname lets those
    descendants leak into the local dependency graph and needlessly widens a
    selected build.  Walk every ancestor up to ``src`` instead.
    """

    current = path
    while True:
        if (current / "COLCON_IGNORE").is_file():
            return True
        if current == source_root:
            return False
        if source_root not in current.parents:
            return False
        current = current.parent


def _is_ignored_source_path(path: Path, *, source_root: Path) -> bool:
    relative = path.relative_to(source_root)
    return (
        any(part in IGNORED_DIRECTORIES for part in relative.parts)
        or _has_colcon_ignore_ancestor(path, source_root=source_root)
    )


@dataclass(frozen=True)
class PackageGraph:
    roots: dict[str, Path]
    # ``dependencies`` is the complete local runtime closure.  Keeping it
    # separate from ``build_dependencies`` is important: a Python node can
    # import an interface at runtime without becoming a CMake/IDL rebuild
    # consumer of that interface.
    dependencies: dict[str, frozenset[str]]
    build_dependencies: dict[str, frozenset[str]]

    @classmethod
    def discover(cls, root: Path) -> "PackageGraph":
        packages: dict[str, Path] = {}
        dependencies: dict[str, frozenset[str]] = {}
        build_dependencies: dict[str, frozenset[str]] = {}
        source_root = root / "src"
        for manifest in sorted(source_root.rglob("package.xml")):
            if _is_ignored_source_path(manifest.parent, source_root=source_root):
                continue
            document = ET.parse(manifest).getroot()
            package_name = (document.findtext("name") or "").strip()
            if not PACKAGE_NAME_PATTERN.fullmatch(package_name):
                raise ValueError(
                    f"invalid local ROS package name in {manifest}: {package_name!r}"
                )
            if package_name in packages:
                raise ValueError(f"duplicate local ROS package: {package_name}")
            dependency_names: set[str] = set()
            build_dependency_names: set[str] = set()
            for element in document:
                tag = element.tag.rsplit("}", 1)[-1]
                if tag in {"test_depend", "doc_depend"}:
                    continue
                dependency = (element.text or "").strip()
                if not dependency:
                    continue
                if tag in {
                    "depend",
                    "build_depend",
                    "build_export_depend",
                    "buildtool_depend",
                    "buildtool_export_depend",
                }:
                    # ``depend`` is the ROS shorthand for a build and runtime
                    # dependency.  The other tags affect the compiled/package
                    # ABI closure but plain ``exec_depend`` deliberately does
                    # not.
                    build_dependency_names.add(dependency)
                    dependency_names.add(dependency)
                elif tag == "exec_depend":
                    dependency_names.add(dependency)
            packages[package_name] = manifest.parent
            dependencies[package_name] = frozenset(dependency_names)
            build_dependencies[package_name] = frozenset(build_dependency_names)
        return cls(
            roots=packages,
            dependencies=dependencies,
            build_dependencies=build_dependencies,
        )

    def closure(self, requested_roots: list[str], mode: str) -> list[str]:
        unknown = [name for name in requested_roots if name not in self.roots]
        if unknown:
            raise ValueError(
                f"required {mode} source package is missing: {', '.join(unknown)}"
            )
        ordered: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(package_name: str) -> None:
            if package_name in visited:
                return
            if package_name in visiting:
                raise ValueError(
                    f"local package dependency cycle includes {package_name}"
                )
            visiting.add(package_name)
            for dependency in sorted(self.dependencies[package_name]):
                if dependency in self.roots:
                    visit(dependency)
            visiting.remove(package_name)
            visited.add(package_name)
            ordered.append(package_name)

        for package_name in requested_roots:
            visit(package_name)
        return ordered

    def reverse_build_closure(self, package_names: set[str]) -> set[str]:
        """Return every local compiled consumer of the changed package set.

        ``closure`` starts from the owners selected by a runtime profile, which
        is the right scope for normal installs.  It is not sufficient after an
        IDL/C++/CMake change, though: a local compiled consumer can live under
        a different owner and still load the old generated symbols from the
        shared ``install/docker`` tree.  Traverse the complete discovered
        graph only for these infrequent ABI-contract changes.  Python-only
        source remains outside the fingerprint and therefore never widens a
        warm restart.
        """

        reverse: dict[str, set[str]] = {}
        for consumer, dependencies in self.build_dependencies.items():
            for dependency in dependencies:
                if dependency in self.roots:
                    reverse.setdefault(dependency, set()).add(consumer)

        selected = set(package_names)
        pending = list(package_names)
        while pending:
            dependency = pending.pop()
            for consumer in sorted(reverse.get(dependency, ())):
                if consumer not in selected:
                    selected.add(consumer)
                    pending.append(consumer)
        return selected


def _hash_files(
    *,
    root: Path,
    source_roots: tuple[Path, ...],
    namespace: bytes,
    names: frozenset[str],
    suffixes: frozenset[str],
) -> str:
    digest = hashlib.sha256(namespace + b"\0")
    matched = False
    for source_root in source_roots:
        if not source_root.is_dir():
            raise ValueError(f"required source package is missing: {source_root}")
        for path in sorted(source_root.rglob("*")):
            if not path.is_file():
                continue
            if _is_ignored_source_path(path.parent, source_root=source_root):
                continue
            # Source-mounted Python is read directly by the owning process.
            # It is deliberately outside the colcon contract: restart that
            # process instead of rebuilding a package (or its dependents).
            if (
                path.suffix == ".py"
                and path.name not in names
                and not path.name.endswith(".launch.py")
            ):
                continue
            if (
                path.name not in names
                and path.suffix not in suffixes
                and not path.name.endswith(".launch.py")
            ):
                continue
            matched = True
            relative = path.relative_to(root).as_posix().encode("utf-8")
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    if not matched:
        raise ValueError("source contract has no inputs")
    return digest.hexdigest()


def package_fingerprint(root: Path, package_root: Path) -> str:
    return _hash_files(
        root=root,
        source_roots=(package_root,),
        namespace=b"taskplanner-package-build-contract-v2",
        names=BUILD_CONTRACT_NAMES,
        suffixes=BUILD_CONTRACT_SUFFIXES,
    )


@dataclass(frozen=True)
class BuildPlan:
    ordered_packages: list[str]
    fingerprints: dict[str, str]
    generation: str
    changed_packages: list[str]
    build_packages: list[str]
    baseline_packages: list[str]
    missing_stamp: bool
    mismatched_stamp: bool
    missing_artifact: bool


def build_plan(
    *,
    root: Path,
    install_root: Path,
    mode: str,
    requested_roots: list[str],
    explicit_rebuild: bool,
) -> BuildPlan:
    if mode not in MODES:
        raise ValueError(f"unsupported package-contract mode: {mode}")
    graph = PackageGraph.discover(root)
    # First discover stale inputs inside the selected owner closure.  Only an
    # ABI-contract change may widen the plan beyond that closure.
    initially_ordered = graph.closure(requested_roots, mode)
    fingerprints = {
        package_name: package_fingerprint(root, graph.roots[package_name])
        for package_name in initially_ordered
    }
    # Package source fingerprints are independent of the selected runtime
    # mode.  v2 used a mode subdirectory, which made one valid installed
    # package appear stale whenever a different mode was used.  Read that
    # layout once for migration, then write one per-package baseline.
    contract_root = install_root / ".taskplanner-package-contracts-v2"
    package_contract_root = contract_root / "packages"
    legacy_mode_contract_root = contract_root / mode
    stale: set[str] = set()
    baseline: set[str] = set()
    missing_stamp = False
    mismatched_stamp = False
    missing_artifact = not (install_root / "setup.bash").is_file()
    for package_name in initially_ordered:
        stamp = package_contract_root / f"{package_name}.sha256"
        legacy_stamp = legacy_mode_contract_root / f"{package_name}.sha256"
        installed_manifest = (
            install_root / package_name / "share" / package_name / "package.xml"
        )
        installed_index = (
            install_root
            / package_name
            / "share"
            / "ament_index"
            / "resource_index"
            / "packages"
            / package_name
        )
        if explicit_rebuild:
            stale.add(package_name)
        if not (
            _installed_artifact_is_file(installed_manifest, root=root)
            and _installed_artifact_is_file(installed_index, root=root)
        ):
            stale.add(package_name)
            missing_artifact = True
        try:
            recorded = stamp.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            missing_stamp = True
            try:
                recorded = legacy_stamp.read_text(encoding="ascii").strip()
            except FileNotFoundError:
                # A usable installed package with no v2 record is a migration
                # candidate, not proof that every selected-mode package needs
                # rebuilding.  The launcher verifies the selected runtime
                # imports before atomically recording this baseline.
                if package_name not in stale:
                    baseline.add(package_name)
                continue
        else:
            pass
        if recorded != fingerprints[package_name]:
            stale.add(package_name)
            mismatched_stamp = True

    # Compiled consumers are selected from the whole local source graph, not
    # just the mode's root closure.  The shared install tree is process-wide;
    # leaving a consumer of a changed IDL/C++ package out here can otherwise
    # produce a mixed generated-interface ABI after an apparently scoped
    # build.  This path is reached only for an explicit or detected build
    # contract change, never for ordinary Python/config edits.
    reverse_consumers = graph.reverse_build_closure(stale)
    ordered = graph.closure(
        [*requested_roots, *sorted(reverse_consumers)],
        mode,
    )
    for package_name in ordered:
        if package_name in fingerprints:
            continue
        fingerprints[package_name] = package_fingerprint(
            root,
            graph.roots[package_name],
        )
        stamp = package_contract_root / f"{package_name}.sha256"
        legacy_stamp = legacy_mode_contract_root / f"{package_name}.sha256"
        installed_manifest = (
            install_root / package_name / "share" / package_name / "package.xml"
        )
        installed_index = (
            install_root
            / package_name
            / "share"
            / "ament_index"
            / "resource_index"
            / "packages"
            / package_name
        )
        if explicit_rebuild or not (
            _installed_artifact_is_file(installed_manifest, root=root)
            and _installed_artifact_is_file(installed_index, root=root)
        ):
            stale.add(package_name)
            if not (
                _installed_artifact_is_file(installed_manifest, root=root)
                and _installed_artifact_is_file(installed_index, root=root)
            ):
                missing_artifact = True
            continue
        try:
            recorded = stamp.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            missing_stamp = True
            try:
                recorded = legacy_stamp.read_text(encoding="ascii").strip()
            except FileNotFoundError:
                baseline.add(package_name)
                continue
        if recorded != fingerprints[package_name]:
            stale.add(package_name)
            mismatched_stamp = True

    generation_digest = hashlib.sha256(b"taskplanner-mode-build-plan-v2\0")
    for package_name in ordered:
        generation_digest.update(package_name.encode("utf-8"))
        generation_digest.update(b"\0")
        generation_digest.update(fingerprints[package_name].encode("ascii"))
        generation_digest.update(b"\0")
    generation = generation_digest.hexdigest()

    selected = set(stale)
    mode_set = set(ordered)
    changed = True
    while changed:
        changed = False
        for package_name in ordered:
            if package_name in selected:
                continue
            if graph.build_dependencies[package_name].intersection(selected, mode_set):
                selected.add(package_name)
                changed = True
    return BuildPlan(
        ordered_packages=ordered,
        fingerprints=fingerprints,
        generation=generation,
        changed_packages=[name for name in ordered if name in stale],
        build_packages=[name for name in ordered if name in selected],
        baseline_packages=[name for name in ordered if name in baseline],
        missing_stamp=missing_stamp,
        mismatched_stamp=mismatched_stamp,
        missing_artifact=missing_artifact,
    )


def print_build_plan(plan: BuildPlan) -> None:
    print(f"GENERATION\t{plan.generation}")
    for package_name in plan.ordered_packages:
        print(f"MODE\t{package_name}")
    for package_name in plan.changed_packages:
        print(f"CHANGED\t{package_name}")
    for package_name in plan.build_packages:
        print(f"BUILD\t{package_name}")
    for package_name in plan.baseline_packages:
        print(f"BASELINE\t{package_name}")
    if plan.missing_stamp:
        print("MISSING_STAMP\ttrue")
    if plan.mismatched_stamp:
        print("MISMATCHED_STAMP\ttrue")
    if plan.missing_artifact:
        print("MISSING_ARTIFACT\ttrue")


def record_build_plan(
    *, install_root: Path, mode: str, plan: BuildPlan, expected_generation: str
) -> None:
    if not expected_generation or expected_generation != plan.generation:
        raise ValueError(
            f"{mode} package build inputs changed while the install was being verified"
        )
    if plan.missing_artifact:
        raise ValueError(
            f"cannot record the {mode} package contract with missing install artifacts"
        )
    contract_root = install_root / ".taskplanner-package-contracts-v2" / "packages"
    contract_root.mkdir(parents=True, exist_ok=True)
    for package_name in plan.ordered_packages:
        destination = contract_root / f"{package_name}.sha256"
        descriptor, temporary_raw = tempfile.mkstemp(
            prefix=f".{package_name}.", dir=contract_root
        )
        temporary = Path(temporary_raw)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(plan.fingerprints[package_name] + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "record"):
        command = subparsers.add_parser(action)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--install-root", type=Path, required=True)
        command.add_argument("--mode", choices=sorted(MODES), required=True)
        command.add_argument("--explicit-rebuild", action="store_true")
        command.add_argument("--expected-generation", default="")
        command.add_argument("roots", nargs="+")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    try:
        if args.action in {"plan", "record"}:
            plan = build_plan(
                root=root,
                install_root=args.install_root,
                mode=args.mode,
                requested_roots=args.roots,
                explicit_rebuild=args.explicit_rebuild,
            )
            if args.action == "plan":
                print_build_plan(plan)
            else:
                record_build_plan(
                    install_root=args.install_root,
                    mode=args.mode,
                    plan=plan,
                    expected_generation=args.expected_generation,
                )
    except (OSError, ValueError, ET.ParseError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
