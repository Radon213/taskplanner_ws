#!/usr/bin/env python3
"""Read-only runtime-owner registry and Docker state projection.

The TOML file is the single operational inventory.  This helper deliberately
does not start, stop, or restart containers; mutation remains in the launcher
where mode selection and the launcher lock already have one owner.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from typing import Callable, Mapping, Sequence


SCHEMA = "taskplanner.runtime-owners.v1"
VALID_MODES = frozenset({"live", "llm-surgeon", "replay", "debug"})
VALID_STRATEGIES = frozenset({"core", "asr", "dedicated", "sidecar"})
OWNER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
PACKAGE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class RegistryError(ValueError):
    """Raised when the small human-authored owner inventory is invalid."""


@dataclass(frozen=True, slots=True)
class Owner:
    name: str
    description: str
    modes: tuple[str, ...]
    restart_strategy: str
    service: str = ""
    services: Mapping[str, str] | None = None
    compose_profile: str = ""
    launch_file: str = ""
    note: str = ""
    aliases: tuple[str, ...] = ()
    restart_impact: str = "owner-only"
    affected_owners: tuple[str, ...] = ()
    build_roots: tuple[str, ...] = ()
    enabled_when_env: str = ""
    enabled_when_value: str = ""
    enabled_when_default: str = ""

    def service_for(self, mode: str) -> str:
        if mode not in self.modes:
            return ""
        if self.services is not None:
            return str(self.services.get(mode, ""))
        return self.service

    def is_enabled(self, environment: Mapping[str, str] | None = None) -> bool:
        """Return whether this optional owner is selected in the current mode.

        The enablement condition is intentionally declarative metadata, not a
        second launcher-side owner list.  Most owners have no condition and
        are always enabled whenever their mode is selected.
        """

        if not self.enabled_when_env:
            return True
        values = os.environ if environment is None else environment
        actual = values.get(self.enabled_when_env, self.enabled_when_default)
        return actual.strip().lower() == self.enabled_when_value.strip().lower()

    def disabled_detail(self) -> str:
        if not self.enabled_when_env:
            return ""
        return f"disabled by {self.enabled_when_env}"


@dataclass(frozen=True, slots=True)
class OwnerState:
    owner: str
    mode: str
    state: str
    service: str
    detail: str


def default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_registry_path(root: Path) -> Path:
    return root / "config" / "taskplanner_runtime_owners.toml"


def _text(value: object, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RegistryError(f"{field} must be a string")
    return value.strip()


def _string_list(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RegistryError(f"{field} must be a list")
    result = tuple(_text(item, field=field) for item in value)
    if len(result) != len(set(result)):
        raise RegistryError(f"{field} contains duplicate values")
    return result


def _owner_enablement(raw: Mapping[str, object], *, field: str) -> tuple[str, str, str]:
    """Parse the small optional-owner selection contract.

    `enabled_when_*` is deliberately exact-match rather than truthy parsing:
    the registry can support any finite Compose-selected capability without
    shell code growing a parallel list of owner names.
    """

    environment = _text(raw.get("enabled_when_env"), field=f"{field}.enabled_when_env")
    value_raw = raw.get("enabled_when_value")
    default_raw = raw.get("enabled_when_default")
    if not environment:
        if value_raw is not None or default_raw is not None:
            raise RegistryError(
                f"{field}.enabled_when_value/default require enabled_when_env"
            )
        return "", "", ""
    if not ENVIRONMENT_NAME_PATTERN.fullmatch(environment):
        raise RegistryError(f"{field}.enabled_when_env is not an environment variable name")
    value = _text(
        "true" if value_raw is None else value_raw,
        field=f"{field}.enabled_when_value",
    )
    default = _text(
        value if default_raw is None else default_raw,
        field=f"{field}.enabled_when_default",
    )
    if not value or not default:
        raise RegistryError(f"{field}.enabled_when_value/default must not be empty")
    return environment, value, default


def load_registry(path: Path) -> dict[str, Owner]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"cannot read owner registry {path}: {exc}") from exc
    if payload.get("schema") != SCHEMA:
        raise RegistryError(f"owner registry schema must be {SCHEMA!r}")
    raw_owners = payload.get("owners")
    if not isinstance(raw_owners, dict):
        raise RegistryError("owner registry requires an [owners] table")
    if not raw_owners:
        raise RegistryError("owner registry must declare at least one owner")
    aliases_raw = payload.get("aliases", {})
    if not isinstance(aliases_raw, dict):
        raise RegistryError("owner registry aliases must be a table")
    aliases_by_target: dict[str, list[str]] = {}
    for alias, target in aliases_raw.items():
        alias_name = _text(alias, field="aliases key")
        target_name = _text(target, field=f"aliases.{alias_name}")
        if not OWNER_NAME_PATTERN.fullmatch(alias_name):
            raise RegistryError(f"aliases.{alias_name} is not a valid owner name")
        if alias_name in raw_owners:
            raise RegistryError(f"alias {alias_name} shadows a canonical owner")
        aliases_by_target.setdefault(target_name, []).append(alias_name)

    result: dict[str, Owner] = {}
    for name, raw in raw_owners.items():
        if not OWNER_NAME_PATTERN.fullmatch(name):
            raise RegistryError(f"owners.{name} is not a valid owner name")
        if not isinstance(raw, dict):
            raise RegistryError(f"owners.{name} must be a table")
        modes_raw = raw.get("modes")
        if not isinstance(modes_raw, list) or not modes_raw:
            raise RegistryError(f"owners.{name}.modes must be a non-empty list")
        modes = tuple(_text(item, field=f"owners.{name}.modes") for item in modes_raw)
        if len(set(modes)) != len(modes) or not set(modes) <= VALID_MODES:
            raise RegistryError(f"owners.{name}.modes contains an invalid or duplicate mode")
        strategy = _text(
            raw.get("restart_strategy"), field=f"owners.{name}.restart_strategy"
        )
        if strategy not in VALID_STRATEGIES:
            raise RegistryError(f"owners.{name}.restart_strategy is invalid")
        services_raw = raw.get("services")
        services: dict[str, str] | None = None
        if services_raw is not None:
            if not isinstance(services_raw, dict):
                raise RegistryError(f"owners.{name}.services must be a table")
            services = {
                _text(mode, field=f"owners.{name}.services mode"): _text(
                    service, field=f"owners.{name}.services.{mode}"
                )
                for mode, service in services_raw.items()
            }
            if set(services) != set(modes) or not all(services.values()):
                raise RegistryError(f"owners.{name}.services must cover every owner mode")
        (
            enabled_when_env,
            enabled_when_value,
            enabled_when_default,
        ) = _owner_enablement(raw, field=f"owners.{name}")
        owner = Owner(
            name=name,
            description=_text(raw.get("description"), field=f"owners.{name}.description"),
            modes=modes,
            restart_strategy=strategy,
            service=_text(raw.get("service"), field=f"owners.{name}.service"),
            services=services,
            compose_profile=_text(
                raw.get("compose_profile"), field=f"owners.{name}.compose_profile"
            ),
            launch_file=_text(
                raw.get("launch_file"), field=f"owners.{name}.launch_file"
            ),
            note=_text(raw.get("note"), field=f"owners.{name}.note"),
            aliases=tuple(sorted(aliases_by_target.pop(name, []))),
            restart_impact=_text(
                raw.get("restart_impact", "owner-only"),
                field=f"owners.{name}.restart_impact",
            ),
            affected_owners=_string_list(
                raw.get("affected_owners"),
                field=f"owners.{name}.affected_owners",
            ),
            build_roots=_string_list(
                raw.get("build_roots"), field=f"owners.{name}.build_roots"
            ),
            enabled_when_env=enabled_when_env,
            enabled_when_value=enabled_when_value,
            enabled_when_default=enabled_when_default,
        )
        if strategy == "dedicated" and (
            not (owner.service or owner.services)
            or not owner.compose_profile
            or not owner.launch_file
        ):
            raise RegistryError(
                f"dedicated owner {name} requires service, compose_profile, and launch_file"
            )
        if strategy == "sidecar" and (
            not (owner.service or owner.services) or not owner.compose_profile
        ):
            raise RegistryError(
                f"sidecar owner {name} requires service and compose_profile"
            )
        result[name] = owner
    if aliases_by_target:
        unknown = ", ".join(sorted(aliases_by_target))
        raise RegistryError(f"owner alias targets do not exist: {unknown}")
    for owner in result.values():
        if not owner.restart_impact:
            raise RegistryError(f"owners.{owner.name}.restart_impact must not be empty")
        unknown_affected = set(owner.affected_owners) - set(result)
        if unknown_affected:
            raise RegistryError(
                f"owners.{owner.name}.affected_owners contains unknown owner(s): "
                + ", ".join(sorted(unknown_affected))
            )
        if owner.name in owner.affected_owners:
            raise RegistryError(
                f"owners.{owner.name}.affected_owners must not contain itself"
            )
        invalid_packages = [
            package for package in owner.build_roots
            if not PACKAGE_NAME_PATTERN.fullmatch(package)
        ]
        if invalid_packages:
            raise RegistryError(
                f"owners.{owner.name}.build_roots contains invalid package(s): "
                + ", ".join(invalid_packages)
            )
    return result


def resolve_owner(registry: Mapping[str, Owner], requested: str) -> Owner | None:
    """Resolve one canonical name or a temporary registry-declared alias."""

    direct = registry.get(requested)
    if direct is not None:
        return direct
    for owner in registry.values():
        if requested in owner.aliases:
            return owner
    return None


def load_runtime_mode_anchors(
    path: Path, registry: Mapping[str, Owner]
) -> dict[str, Owner]:
    """Return the TOML-declared owner that anchors each runtime mode.

    An anchor is deliberately a small launcher/controller observation point,
    not an implied dependency closure.  Keeping it in the owner registry
    avoids an easily-forgotten second map of mode -> Compose service.
    """

    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"cannot read owner registry {path}: {exc}") from exc
    raw_anchors = payload.get("runtime_mode_anchors")
    if not isinstance(raw_anchors, dict):
        raise RegistryError("owner registry requires a [runtime_mode_anchors] table")
    anchors: dict[str, Owner] = {}
    for raw_mode, raw_owner_name in raw_anchors.items():
        mode = _text(raw_mode, field="runtime_mode_anchors key")
        owner_name = _text(
            raw_owner_name, field=f"runtime_mode_anchors.{mode}"
        )
        if mode not in VALID_MODES:
            raise RegistryError(f"runtime_mode_anchors.{mode} has an invalid mode")
        owner = registry.get(owner_name)
        if owner is None:
            raise RegistryError(
                f"runtime_mode_anchors.{mode} references unknown owner {owner_name}"
            )
        if mode not in owner.modes or not owner.service_for(mode):
            raise RegistryError(
                f"runtime_mode_anchors.{mode} owner {owner_name} has no service in {mode}"
            )
        anchors[mode] = owner
    required_modes = set(VALID_MODES)
    if set(anchors) != required_modes:
        missing = ", ".join(sorted(required_modes - set(anchors)))
        extra = ", ".join(sorted(set(anchors) - required_modes))
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unknown {extra}")
        raise RegistryError("runtime_mode_anchors must cover every runtime mode (" + "; ".join(detail) + ")")
    return anchors


def _read_environment_file(path: Path) -> dict[str, str]:
    """Read the simple KEY=VALUE subset used by Taskplanner mode files.

    Compose remains the source of full interpolation.  The registry needs only
    capability-selector values so it can project optional owners before a
    `compose up`; accepting quoted values here keeps this small helper aligned
    with the checked-in `.env` files without turning it into a Compose parser.
    """

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RegistryError(f"cannot read owner environment file {path}: {exc}") from exc
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise RegistryError(
                f"invalid environment entry in {path}:{line_number}; expected KEY=VALUE"
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def effective_environment(environment_files: Sequence[Path]) -> dict[str, str]:
    """Merge mode files in Compose order, then explicit process overrides."""

    values: dict[str, str] = {}
    for path in environment_files:
        values.update(_read_environment_file(path))
    # Docker Compose gives an exported shell variable precedence over env files.
    values.update(os.environ)
    return values


def docker_service_state(
    root: Path,
    service: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, str]:
    if not service:
        return "", ""
    try:
        completed = run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project.working_dir={root.resolve()}",
                "--filter",
                f"label=com.docker.compose.service={service}",
                "--format",
                "{{.State}}\t{{.Status}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", "docker state unavailable"
    rows = [line.split("\t", 1) for line in completed.stdout.splitlines() if line]
    if completed.returncode != 0:
        return "unknown", "docker state unavailable"
    if not rows:
        return "", ""
    if len(rows) > 1:
        return "conflict", f"{len(rows)} containers"
    state = rows[0][0].strip()
    detail = rows[0][1].strip() if len(rows[0]) > 1 else state
    return state, detail


def docker_service_states(
    root: Path,
    services: Sequence[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, tuple[str, str]]:
    """Project several owner containers from one inexpensive Docker query.

    The owner panel is an observation surface, so it must not turn one status
    refresh into N separate ``docker ps`` calls.  The registry remains the
    source of the selected service names; this helper only batches their
    read-only projection.
    """

    selected = tuple(dict.fromkeys(service for service in services if service))
    if not selected:
        return {}
    try:
        completed = run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project.working_dir={root.resolve()}",
                "--format",
                '{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            service: ("unknown", "docker state unavailable")
            for service in selected
        }
    if completed.returncode != 0:
        return {
            service: ("unknown", "docker state unavailable")
            for service in selected
        }

    rows_by_service: dict[str, list[tuple[str, str]]] = {
        service: [] for service in selected
    }
    for raw_line in completed.stdout.splitlines():
        service, separator, remainder = raw_line.partition("\t")
        if not separator or service not in rows_by_service:
            continue
        state, separator, detail = remainder.partition("\t")
        if not separator:
            continue
        rows_by_service[service].append((state.strip(), detail.strip()))

    result: dict[str, tuple[str, str]] = {}
    for service, rows in rows_by_service.items():
        if not rows:
            result[service] = ("", "")
        elif len(rows) == 1:
            result[service] = rows[0]
        else:
            result[service] = ("conflict", f"{len(rows)} containers")
    return result


def project_owner_state(
    root: Path,
    registry: Mapping[str, Owner],
    owner: Owner,
    mode: str,
    *,
    environment: Mapping[str, str] | None = None,
    state_lookup: Callable[[Path, str], tuple[str, str]] = docker_service_state,
) -> OwnerState:
    if mode not in owner.modes:
        return OwnerState(owner.name, mode, "not-applicable", "", "mode not supported")
    service = owner.service_for(mode)
    if not owner.is_enabled(environment):
        return OwnerState(owner.name, mode, "disabled", service, owner.disabled_detail())
    service_state, service_detail = state_lookup(root, service)
    if service_state:
        return OwnerState(owner.name, mode, service_state, service, service_detail)

    if owner.launch_file and not (root / "src" / "bringup" / "launch" / owner.launch_file).is_file():
        return OwnerState(owner.name, mode, "unavailable", service, f"missing {owner.launch_file}")
    return OwnerState(owner.name, mode, "not-deployed", service, "no owner container")


def _resolve_line(
    owner: Owner, mode: str, environment: Mapping[str, str] | None = None
) -> str:
    supported = "true" if mode in owner.modes else "false"
    enabled = "true" if owner.is_enabled(environment) else "false"
    fields = (
        owner.name,
        owner.service_for(mode),
        owner.restart_strategy,
        owner.compose_profile,
        owner.launch_file,
        supported,
        owner.restart_impact,
        ",".join(owner.affected_owners),
        ",".join(owner.build_roots),
        ",".join(owner.aliases),
        enabled,
    )
    if any("|" in field or "\n" in field for field in fields):
        raise RegistryError("owner registry shell fields contain an invalid delimiter")
    return "|".join(fields)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--registry", type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        default=[],
        help="optional Compose-style environment file used for owner capability selection",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    list_parser = sub.add_parser("list")
    list_parser.add_argument("--format", choices=("table", "json"), default="table")
    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("owner")
    resolve_parser.add_argument("--mode", default="live")
    services_parser = sub.add_parser("services")
    services_parser.add_argument("--mode", default="live")
    build_roots_parser = sub.add_parser("build-roots")
    build_roots_parser.add_argument("owner")
    build_roots_parser.add_argument("--mode", default="live")
    impact_parser = sub.add_parser("impact")
    impact_parser.add_argument("owner")
    impact_parser.add_argument("--mode", default="live")
    anchor_parser = sub.add_parser("anchor")
    anchor_parser.add_argument("--mode", default="live")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("owner", nargs="?", default="all")
    status_parser.add_argument("--mode", default="live")
    status_parser.add_argument("--format", choices=("table", "json"), default="table")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    registry_path = args.registry or default_registry_path(root)
    try:
        registry = load_registry(registry_path)
        anchors = load_runtime_mode_anchors(registry_path, registry)
        environment = effective_environment(args.env_file)
        if args.command == "validate":
            print(f"owner registry valid: {len(registry)} owners")
            return 0
        if args.command == "list":
            if args.format == "json":
                print(json.dumps({name: asdict(owner) for name, owner in registry.items()}))
            else:
                print("OWNER\tMODES\tRESTART\tIMPACT\tSERVICE\tALIASES")
                for owner in registry.values():
                    services = sorted(
                        set(owner.services.values()) if owner.services else {owner.service}
                    )
                    print(
                        f"{owner.name}\t{','.join(owner.modes)}\t"
                        f"{owner.restart_strategy}\t{owner.restart_impact}\t"
                        f"{','.join(s for s in services if s) or '-'}\t"
                        f"{','.join(owner.aliases) or '-'}"
                    )
            return 0
        if args.mode not in VALID_MODES:
            raise RegistryError(f"invalid owner mode: {args.mode}")
        if args.command == "resolve":
            owner = resolve_owner(registry, args.owner)
            if owner is None:
                raise RegistryError(f"unknown runtime owner: {args.owner}")
            print(_resolve_line(owner, args.mode, environment))
            return 0
        if args.command == "services":
            services = [
                owner.service_for(args.mode)
                for owner in registry.values()
                if (
                    args.mode in owner.modes
                    and owner.restart_strategy not in {"asr", "sidecar"}
                    and owner.is_enabled(environment)
                )
            ]
            print("\n".join(service for service in services if service))
            return 0
        if args.command == "build-roots":
            owner = resolve_owner(registry, args.owner)
            if owner is None:
                raise RegistryError(f"unknown runtime owner: {args.owner}")
            if args.mode not in owner.modes:
                raise RegistryError(f"owner {owner.name} is not available in {args.mode} mode")
            print("\n".join(owner.build_roots))
            return 0
        if args.command == "impact":
            owner = resolve_owner(registry, args.owner)
            if owner is None:
                raise RegistryError(f"unknown runtime owner: {args.owner}")
            if args.mode not in owner.modes:
                raise RegistryError(f"owner {owner.name} is not available in {args.mode} mode")
            print(json.dumps({
                "owner": owner.name,
                "restart_impact": owner.restart_impact,
                "affected_owners": list(owner.affected_owners),
            }))
            return 0
        if args.command == "anchor":
            owner = anchors[args.mode]
            fields = (
                owner.name,
                owner.service_for(args.mode),
                owner.compose_profile,
                owner.restart_strategy,
            )
            if any("|" in field or "\n" in field for field in fields):
                raise RegistryError("owner registry shell fields contain an invalid delimiter")
            print("|".join(fields))
            return 0
        selected = list(registry.values()) if args.owner == "all" else [resolve_owner(registry, args.owner)]
        if selected == [None]:
            raise RegistryError(f"unknown runtime owner: {args.owner}")
        # Query all selected service states once.  A status read must stay
        # lightweight even when the split runtime has nine independently
        # restartable owners.
        service_states = docker_service_states(
            root,
            [
                owner.service_for(args.mode)
                for owner in selected
                if (
                    owner is not None
                    and args.mode in owner.modes
                    and owner.is_enabled(environment)
                )
            ],
        )
        states = [
            project_owner_state(
                root,
                registry,
                owner,
                args.mode,
                environment=environment,
                state_lookup=lambda _root, service: service_states.get(service, ("", "")),
            )
            for owner in selected
        ]
        if args.format == "json":
            print(json.dumps([asdict(state) for state in states], ensure_ascii=False))
        else:
            print("OWNER\tMODE\tSTATE\tSERVICE\tDETAIL")
            for state in states:
                print(
                    f"{state.owner}\t{state.mode}\t{state.state}\t"
                    f"{state.service or '-'}\t{state.detail}"
                )
        return 0
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
