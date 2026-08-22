from pathlib import Path

import pytest
import yaml

from retraction_control.command_models import ErrorCode, ProfileValidationError
from retraction_control.profile_loader import DraftProfile, ExecutionProfile, load_profile
from retraction_control.runtime_config import load_runtime_config


CONFIG_ROOT = Path(__file__).resolve().parents[1] / "config"


def test_synthetic_profile_is_checksum_bound_and_fake_only():
    profile = load_profile(CONFIG_ROOT / "fake.yaml", require_approved=True)
    assert isinstance(profile, ExecutionProfile)
    assert profile.name == "synthetic_fake"
    assert profile.calibration_revision == "synthetic-test-only"
    assert profile.expected_checksum == profile.computed_checksum
    assert profile.robot.controller_ip == "127.0.0.1"
    assert profile.public_procedure_type == "nephrectomy"
    assert profile.sensor.transport == "synthetic"
    assert profile.control.impedance_tolerance_n == 1.0


@pytest.mark.parametrize("filename", ["throat.yaml", "hernia.yaml"])
def test_partner_profiles_remain_non_executable_drafts(filename):
    profile = load_profile(CONFIG_ROOT / filename)
    assert isinstance(profile, DraftProfile)
    assert not profile.calibration_approved
    with pytest.raises(ProfileValidationError) as raised:
        load_profile(CONFIG_ROOT / filename, require_approved=True)
    assert raised.value.code is ErrorCode.PROFILE_NOT_APPROVED


def test_hernia_draft_has_no_invented_public_procedure_mapping():
    profile = load_profile(CONFIG_ROOT / "hernia.yaml")
    assert isinstance(profile, DraftProfile)
    assert profile.procedure_type == "hernia"
    assert profile.public_procedure_type is None


def test_logging_directory_is_absolute_and_no_secret_value_is_packaged():
    payload = yaml.safe_load((CONFIG_ROOT / "logging.yaml").read_text(encoding="utf-8"))
    settings = load_runtime_config(payload)
    assert settings.data_directory.is_absolute()
    assert settings.atomic_fsync
    assert settings.shadow_trace_directory_name == "shadow_traces"
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(CONFIG_ROOT.glob("*.yaml"))
    ).casefold()
    assert "license_key" not in combined
    assert "api_key" not in combined
    assert "password:" not in combined
