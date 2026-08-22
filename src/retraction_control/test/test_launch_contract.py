from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_launch_is_native_ros_and_fail_safe_by_default():
    source = (PACKAGE_ROOT / "launch" / "retraction_control.launch.py").read_text(
        encoding="utf-8"
    )
    assert 'default_value="0"' in source
    assert 'default_value="rmw_cyclonedds_cpp"' in source
    assert 'default_value="fake"' in source
    assert '"config", "fake.yaml"' in source
    assert '"config", "logging.yaml"' in source
    assert '"runtime_config_path": runtime_config_path' in source
    assert 'DeclareLaunchArgument("allow_motion", default_value="false")' in source
    assert "rosbridge" not in source.casefold()
    assert "jupyter" not in source.casefold()
    assert "roslibpy" not in source.casefold()


def test_package_installs_launch_and_profile_files():
    setup_source = (PACKAGE_ROOT / "setup.py").read_text(encoding="utf-8")
    assert 'os.path.join("config", "*.yaml")' in setup_source
    assert 'os.path.join("launch", "*.launch.py")' in setup_source
    assert "retraction_control.command_server_node:main" in setup_source
