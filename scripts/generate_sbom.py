#!/usr/bin/env python3
"""Generate a deterministic SPDX inventory for the release image and webapp."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def spdx_id(kind: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]+", "-", value).strip("-") or "unknown"
    return f"SPDXRef-{kind}-{safe}"


def package(name: str, version: str, supplier: str, kind: str) -> dict[str, Any]:
    return {
        "SPDXID": spdx_id(kind, f"{name}-{version}"),
        "name": name,
        "versionInfo": version or "unknown",
        "supplier": f"Organization: {supplier}",
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "NOASSERTION",
        "copyrightText": "NOASSERTION",
        "primaryPackagePurpose": "LIBRARY",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="taskplanner-ws:dev")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    image_id = run("docker", "image", "inspect", args.image, "--format", "{{.Id}}").strip()
    os_rows = run(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "bash",
        args.image,
        "-lc",
        "dpkg-query -W -f='${Package}\\t${Version}\\n' | LC_ALL=C sort",
    ).splitlines()
    python_rows = run(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "python3",
        args.image,
        "-c",
        (
            "import importlib.metadata as m; "
            "print('\\n'.join(sorted(f'{d.metadata[\"Name\"]}\\t{d.version}' "
            "for d in m.distributions() if d.metadata.get(\"Name\"))))"
        ),
    ).splitlines()
    lock = json.loads((ROOT / "webapp" / "package-lock.json").read_text(encoding="utf-8"))

    packages: list[dict[str, Any]] = [
        {
            **package("taskplanner-container", image_id, "Taskplanner", "Container"),
            "primaryPackagePurpose": "CONTAINER",
            "checksums": [{"algorithm": "SHA256", "checksumValue": image_id.removeprefix("sha256:")}],
        }
    ]
    seen = {packages[0]["SPDXID"]}

    def append_rows(rows: list[str], supplier: str, kind: str) -> None:
        for row in rows:
            name, separator, version = row.partition("\t")
            if not separator:
                continue
            item = package(name, version, supplier, kind)
            if item["SPDXID"] not in seen:
                seen.add(item["SPDXID"])
                packages.append(item)

    append_rows(os_rows, "Ubuntu", "Deb")
    append_rows(python_rows, "Python Packaging Authority", "Python")
    for relative, metadata in sorted(lock.get("packages", {}).items()):
        if not relative or not isinstance(metadata, dict):
            continue
        name = str(metadata.get("name") or relative.rsplit("node_modules/", 1)[-1])
        item = package(name, str(metadata.get("version", "")), "npm", "Npm")
        if item["SPDXID"] not in seen:
            seen.add(item["SPDXID"])
            packages.append(item)

    namespace_material = f"{image_id}:{hashlib.sha256((ROOT / 'webapp/package-lock.json').read_bytes()).hexdigest()}"
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "taskplanner-release-sbom",
        "documentNamespace": (
            "https://taskplanner.local/spdx/"
            + hashlib.sha256(namespace_material.encode()).hexdigest()
        ),
        "creationInfo": {
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: taskplanner-generate-sbom"],
        },
        "packages": packages,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": packages[0]["SPDXID"],
            },
            *[
                {
                    "spdxElementId": packages[0]["SPDXID"],
                    "relationshipType": "CONTAINS",
                    "relatedSpdxElement": item["SPDXID"],
                }
                for item in packages[1:]
            ],
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
