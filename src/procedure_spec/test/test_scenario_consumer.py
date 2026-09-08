from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from procedure_spec.scenario_config import ScenarioConfigSnapshot, scenario_config_payload
from procedure_spec.scenario_consumer import (
    ScenarioConfigConsumerBinding,
    load_scenario_consumer_bundle,
    scenario_config_apply_is_safe,
)
from procedure_spec.scenario_revision import compute_bundle_config_revision


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


def test_consumer_bundle_loader_accepts_selected_direct_child() -> None:
    root = _spec_root()
    candidate = root / "thyroidectomy_demo"

    loaded = load_scenario_consumer_bundle(
        ScenarioConfigSnapshot(
            bundle_name="thyroidectomy_demo",
            spec_dir=str(candidate),
            revision=compute_bundle_config_revision(candidate),
        ),
        fixed_spec_root=root,
    )

    assert loaded.spec_dir == str(candidate.resolve())
    assert loaded.procedure_spec.procedure_id == "thyroidectomy_demo"


def test_consumer_binding_commits_only_the_newest_staged_revision(tmp_path) -> None:
    root = tmp_path / "specs"
    shutil.copytree(_spec_root(), root)
    candidate = root / "thyroidectomy_demo"
    binding = ScenarioConfigConsumerBinding.from_spec_dir(root / "thyroidectomy")
    first_payload = scenario_config_payload(
        bundle_name=candidate.name,
        spec_dir=str(candidate.resolve()),
        revision=compute_bundle_config_revision(candidate),
    )
    assert binding.stage(first_payload)
    first = binding.resolve_pending()
    assert first is not None
    first_snapshot, first_bundle = first

    (candidate / "vlm_procedure_prompt.yaml").write_text(
        (candidate / "vlm_procedure_prompt.yaml").read_text(encoding="utf-8")
        + "\n# second revision\n",
        encoding="utf-8",
    )
    second_payload = {
        **first_payload,
        "revision": compute_bundle_config_revision(candidate),
    }

    assert binding.stage(second_payload)

    assert binding.commit(first_snapshot, first_bundle) is False
    pending = binding.pending_snapshot()
    assert pending is not None
    assert pending.revision == second_payload["revision"]

    second = binding.resolve_pending()
    assert second is not None
    second_snapshot, second_bundle = second
    assert binding.commit(second_snapshot, second_bundle) is True
    revision, applied_spec_dir, pending = binding.status()
    assert revision == second_payload["revision"]
    assert applied_spec_dir == str(candidate.resolve())
    assert pending is None
    assert binding.stage(second_payload) is False

    binding.note_local_spec_dir(root / "nephrectomy")
    assert binding.stage(second_payload) is True


def test_consumer_bundle_loader_rejects_stale_revision_after_authored_edit(tmp_path) -> None:
    root = tmp_path / "specs"
    shutil.copytree(_spec_root(), root)
    candidate = root / "thyroidectomy_demo"
    stale_revision = compute_bundle_config_revision(candidate)
    (candidate / "vlm_procedure_prompt.yaml").write_text(
        (candidate / "vlm_procedure_prompt.yaml").read_text(encoding="utf-8")
        + "\n# authored edit after ScenarioStore publish\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="revision does not match"):
        load_scenario_consumer_bundle(
            ScenarioConfigSnapshot(
                bundle_name=candidate.name,
                spec_dir=str(candidate),
                revision=stale_revision,
            ),
            fixed_spec_root=root,
        )


def test_consumer_binding_rejects_stale_revision_at_deferred_apply(tmp_path) -> None:
    """A paused owner must not apply bytes edited after it staged the notice."""

    root = tmp_path / "specs"
    shutil.copytree(_spec_root(), root)
    candidate = root / "thyroidectomy_demo"
    binding = ScenarioConfigConsumerBinding.from_spec_dir(root / "thyroidectomy")
    payload = scenario_config_payload(
        bundle_name=candidate.name,
        spec_dir=str(candidate.resolve()),
        revision=compute_bundle_config_revision(candidate),
    )
    assert binding.stage(payload) is True

    (candidate / "vlm_procedure_prompt.yaml").write_text(
        (candidate / "vlm_procedure_prompt.yaml").read_text(encoding="utf-8")
        + "\n# edited while owner was waiting for pause\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="revision does not match"):
        binding.revalidate_pending()
    assert binding.pending_snapshot() is not None
    assert binding.status()[0] == ""


@pytest.mark.parametrize(
    "snapshot",
    [
        ScenarioConfigSnapshot("outside", "/tmp/outside", "sha256:outside"),
        ScenarioConfigSnapshot(
            "thyroidectomy",
            str(_spec_root() / "thyroidectomy" / "nested"),
            "sha256:nested",
        ),
        ScenarioConfigSnapshot(
            "nephrectomy",
            str(_spec_root() / "thyroidectomy"),
            "sha256:mismatch",
        ),
        ScenarioConfigSnapshot(
            "thyroidectomy",
            str(_spec_root() / "thyroidectomy"),
            "",
        ),
    ],
)
def test_consumer_bundle_loader_rejects_untrusted_or_mismatched_paths(snapshot) -> None:
    with pytest.raises(ValueError):
        load_scenario_consumer_bundle(snapshot, fixed_spec_root=_spec_root())


@pytest.mark.parametrize(
    ("state_received", "running", "state", "initial_idle", "busy", "expected"),
    [
        (False, False, "", True, False, True),
        (False, False, "", False, False, False),
        (False, False, "", True, True, False),
        (True, True, "running", True, False, False),
        (True, True, "paused", False, False, True),
        (True, True, "paused", False, True, False),
        (True, False, "stopped", False, False, True),
        (True, False, "completed", False, False, True),
        (True, True, "stopped", False, False, False),
        (True, False, "unknown", False, False, False),
    ],
)
def test_consumer_quiescent_predicate_keeps_owner_specific_busy_guard(
    state_received,
    running,
    state,
    initial_idle,
    busy,
    expected,
) -> None:
    assert (
        scenario_config_apply_is_safe(
            state_received=state_received,
            scenario_running=running,
            execution_state=state,
            initial_idle=initial_idle,
            local_busy=busy,
        )
        is expected
    )
