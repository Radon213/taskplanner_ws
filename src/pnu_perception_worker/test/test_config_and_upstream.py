from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pnu_perception_worker.adapters import (
    AdapterLoadError,
    read_git_revision,
    verify_source_manifest,
)
from pnu_perception_worker.config import ConfigError, WorkerConfig


def test_reads_packed_git_revision_without_git_executable(tmp_path: Path) -> None:
    root = tmp_path / "source"
    git = root / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    revision = "0123456789abcdef0123456789abcdef01234567"
    (git / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled\n{revision} refs/heads/main\n",
        encoding="ascii",
    )
    assert read_git_revision(root) == revision


def test_rejects_checkout_without_revision_metadata(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    with pytest.raises(AdapterLoadError, match="no readable .git"):
        read_git_revision(root)


def test_upstream_manifest_rejects_modified_or_unmanifested_executable_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "upstream"
    source_root = root / "code"
    source_root.mkdir(parents=True)
    source = source_root / "core.py"
    source.write_text("PINNED = True\n", encoding="utf-8")
    manifest = {"code/core.py": hashlib.sha256(source.read_bytes()).hexdigest()}
    verify_source_manifest(root, manifest, ("code",))

    source.write_text("PINNED = False\n", encoding="utf-8")
    with pytest.raises(AdapterLoadError, match="digest does not match"):
        verify_source_manifest(root, manifest, ("code",))

    source.write_text("PINNED = True\n", encoding="utf-8")
    injected = source_root / "untracked.py"
    injected.write_text("INJECTED = True\n", encoding="utf-8")
    with pytest.raises(AdapterLoadError, match="unmanifested artifact"):
        verify_source_manifest(root, manifest, ("code",))
    injected.unlink()

    bytecode = source_root / "__pycache__" / "core.cpython-311.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"forged")
    with pytest.raises(AdapterLoadError, match="forbidden bytecode cache"):
        verify_source_manifest(root, manifest, ("code",))


def test_api_token_file_validation(config, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("valid-token\n", encoding="utf-8")
    with_token = type(config)(**{**config.__dict__, "api_token_file": token_file})
    assert with_token.read_api_token() == "valid-token"
    token_file.write_text("has whitespace", encoding="utf-8")
    with pytest.raises(ConfigError, match="whitespace"):
        with_token.read_api_token()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.50", "worker.local"])
def test_remote_or_wildcard_bind_requires_bearer_token(config, host) -> None:
    with pytest.raises(ConfigError, match="required for wildcard or non-loopback"):
        config.validate_bind_auth(host, None)
    config.validate_bind_auth(host, "secret")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_bind_can_remain_unauthenticated(config, host) -> None:
    config.validate_bind_auth(host, None)


def test_support_plane_configuration_is_explicit_and_fail_closed_by_default(
    monkeypatch,
) -> None:
    for name in (
        "PNU_TOOL_SUPPORT_PLANE_NORMAL",
        "PNU_TOOL_SUPPORT_PLANE_OFFSET_M",
        "PNU_TOOL_SUPPORT_PLANE_CONFIG_VERSION",
        "PNU_TOOL_SUPPORT_PLANE_INLIER_RATIO",
        "PNU_TOOL_SUPPORT_PLANE_RESIDUAL_P95_M",
        "PNU_TOOL_SUPPORT_PLANE_VALIDATED",
    ):
        monkeypatch.delenv(name, raising=False)
    config = WorkerConfig.from_env()
    assert config.tool_support_plane_config_version.endswith("_provisional")
    assert config.tool_support_plane_validated is False
    assert len(config.tool_support_plane_normal) == 3
    assert config.tool_support_plane_residual_p95_m > 0.0


def test_support_plane_environment_is_validated(monkeypatch) -> None:
    monkeypatch.setenv("PNU_TOOL_SUPPORT_PLANE_NORMAL", "0,0,0")
    with pytest.raises(ConfigError, match="zero vector"):
        WorkerConfig.from_env()
    monkeypatch.setenv("PNU_TOOL_SUPPORT_PLANE_NORMAL", "0,0,-1")
    monkeypatch.setenv("PNU_TOOL_SUPPORT_PLANE_RESIDUAL_P95_M", "-0.01")
    with pytest.raises(ConfigError, match="non-negative"):
        WorkerConfig.from_env()


def test_response_and_rle_limits_cannot_exceed_bridge_contract(monkeypatch) -> None:
    monkeypatch.setenv("PNU_MAX_TOTAL_RLE_COUNTS", "1000001")
    with pytest.raises(ConfigError, match="1000000-run"):
        WorkerConfig.from_env()
    monkeypatch.setenv("PNU_MAX_TOTAL_RLE_COUNTS", "1000000")
    monkeypatch.setenv("PNU_MAX_RESPONSE_JSON_BYTES", str(16 * 1024 * 1024 + 1))
    with pytest.raises(ConfigError, match="16 MiB"):
        WorkerConfig.from_env()

    monkeypatch.setenv("PNU_MAX_RESPONSE_JSON_BYTES", str(16 * 1024 * 1024))
    monkeypatch.setenv("PNU_MAX_INGRESS_READ_SEC", "0.049")
    with pytest.raises(ConfigError, match="INGRESS_READ_SEC"):
        WorkerConfig.from_env()
    monkeypatch.setenv("PNU_MAX_INGRESS_READ_SEC", "1.0")
    config = WorkerConfig.from_env()
    assert config.max_total_rle_counts == 1_000_000
    assert config.max_response_json_bytes == 16 * 1024 * 1024
    assert config.max_ingress_read_sec == 1.0


def test_support_plane_artifact_deployment_pins_are_bounded(monkeypatch) -> None:
    monkeypatch.setenv("PNU_TOOL_SUPPORT_PLANE_ARTIFACT", "/config/cam4-plane.json")
    monkeypatch.setenv("PNU_TOOL_SUPPORT_PLANE_ARTIFACT_SHA256", "a" * 64)
    monkeypatch.setenv("PNU_TOOL_SUPPORT_PLANE_CAMERA_SERIAL", "146222251000")
    monkeypatch.setenv(
        "PNU_TOOL_SUPPORT_PLANE_CAMERA_PROFILE",
        "RGB 1280x720x15; depth 1280x720x15",
    )
    monkeypatch.setenv("PNU_TOOL_SUPPORT_PLANE_FIRMWARE_VERSION", "5.15.0.2")
    monkeypatch.setenv("PNU_TOOL_SUPPORT_PLANE_MAX_AGE_DAYS", "30")
    config = WorkerConfig.from_env()
    assert config.tool_support_plane_artifact == Path("/config/cam4-plane.json")
    assert config.tool_support_plane_artifact_sha256 == "a" * 64
    assert config.tool_support_plane_camera_serial == "146222251000"
    assert config.tool_support_plane_camera_profile.startswith("RGB 1280x720")
    assert config.tool_support_plane_firmware_version == "5.15.0.2"
    assert config.tool_support_plane_max_age_days == 30

    monkeypatch.setenv("PNU_TOOL_SUPPORT_PLANE_ARTIFACT_SHA256", "short")
    with pytest.raises(ConfigError, match="full SHA-256"):
        WorkerConfig.from_env()
