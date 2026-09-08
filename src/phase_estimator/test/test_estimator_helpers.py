import json
from pathlib import Path
from types import SimpleNamespace

from procedure_spec import compute_bundle_config_revision, scenario_config_payload
from phase_estimator.estimator import _stamp_to_sec
from phase_estimator.node import PhaseEstimatorNode


class _Stamp:
    sec = 12
    nanosec = 345_000_000


def test_stamp_to_sec_preserves_fractional_seconds():
    assert _stamp_to_sec(_Stamp()) == 12.345


def test_reset_is_repeatable_and_reopens_the_next_lifecycle_edge() -> None:
    node = PhaseEstimatorNode.__new__(PhaseEstimatorNode)
    node._last_lifecycle_control_signature = None
    node._spec_dir = "/test/spec"
    load_calls: list[str] = []
    node._load_spec = load_calls.append

    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="start"))

    assert load_calls == ["/test/spec", "/test/spec"]
    assert node._last_lifecycle_control_signature == ("start", "")


def _scenario_node() -> tuple[PhaseEstimatorNode, Path, list[str], list[str]]:
    spec_root = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    initial = spec_root / "thyroidectomy"
    node = PhaseEstimatorNode.__new__(PhaseEstimatorNode)
    node._spec_dir = str(initial.resolve())
    node._scenario_config_root = spec_root
    node._scenario_config_revision = ""
    node._pending_scenario_config = None
    node._scenario_state_received = False
    node._scenario_running = False
    node._scenario_execution_state = ""
    node._scenario_initial_idle = True
    node._last_lifecycle_control_signature = None
    applied: list[str] = []
    warnings: list[str] = []
    node.get_logger = lambda: SimpleNamespace(
        warning=warnings.append,
        info=lambda _message: None,
    )

    def set_parameters(parameters):
        candidate = str(parameters[0].value)
        node._spec_dir = candidate
        applied.append(candidate)
        return SimpleNamespace(successful=True, reason="")

    node.set_parameters_atomically = set_parameters
    return node, spec_root, applied, warnings


def _scenario_message(spec_dir: Path, *, revision: str | None = None):
    resolved = spec_dir.resolve()
    return SimpleNamespace(
        data=json.dumps(
            scenario_config_payload(
                bundle_name=resolved.name,
                spec_dir=str(resolved),
                revision=revision or compute_bundle_config_revision(resolved),
            )
        )
    )


def test_scenario_store_revision_rehydrates_phase_estimator_at_initial_idle() -> None:
    node, spec_root, applied, warnings = _scenario_node()
    candidate = spec_root / "nephrectomy"

    node._on_scenario_config(_scenario_message(candidate))

    assert applied == [str(candidate.resolve())]
    assert node._spec_dir == str(candidate.resolve())
    assert node._scenario_config_revision == compute_bundle_config_revision(candidate)
    assert node._pending_scenario_config is None
    assert warnings == []


def test_phase_estimator_defers_scenario_revision_until_paused() -> None:
    node, spec_root, applied, _warnings = _scenario_node()
    candidate = spec_root / "nephrectomy"
    node._scenario_state_received = True
    node._scenario_running = True
    node._scenario_execution_state = "running"

    node._on_scenario_config(_scenario_message(candidate))

    assert applied == []
    assert node._pending_scenario_config is not None

    node._on_simulation_state(
        SimpleNamespace(running=True, execution_state="paused")
    )

    assert applied == [str(candidate.resolve())]
    assert node._pending_scenario_config is None


def test_phase_estimator_keeps_last_good_bundle_for_external_scenario_path() -> None:
    node, _spec_root, applied, warnings = _scenario_node()

    node._on_scenario_config(
        SimpleNamespace(
            data=json.dumps(
                scenario_config_payload(
                    bundle_name="outside",
                    spec_dir="/tmp/outside",
                    revision="sha256:outside",
                )
            )
        )
    )

    assert applied == []
    assert node._scenario_config_revision == ""
    assert node._pending_scenario_config is None
    assert warnings and "outside the fixed root" in warnings[-1]
