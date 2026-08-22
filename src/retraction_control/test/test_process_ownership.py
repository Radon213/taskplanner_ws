import os
from pathlib import Path
import subprocess
import sys

from retraction_control.adapters import SingleOwnerGuard


def test_second_process_cannot_acquire_hardware_authority(tmp_path):
    lock_path = (tmp_path / "controller.lock").resolve()
    package_root = Path(__file__).resolve().parents[1]
    code = """
import sys
from retraction_control.adapters import OwnershipError, SingleOwnerGuard
try:
    SingleOwnerGuard(sys.argv[1]).acquire()
except OwnershipError:
    raise SystemExit(23)
raise SystemExit(0)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(package_root)
    with SingleOwnerGuard(lock_path):
        result = subprocess.run(
            [sys.executable, "-c", code, str(lock_path)],
            check=False,
            env=environment,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    assert result.returncode == 23, result.stderr
