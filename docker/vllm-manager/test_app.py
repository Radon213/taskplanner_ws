from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


APP_PATH = Path(__file__).with_name("app.py")
SPEC = importlib.util.spec_from_file_location("taskplanner_vllm_manager", APP_PATH)
assert SPEC is not None and SPEC.loader is not None
app_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app_module
SPEC.loader.exec_module(app_module)


class VllmManagerTest(unittest.TestCase):
    def _profile(
        self,
        model_id: str,
        *,
        reasoning_parser: str = "",
    ):
        return app_module.ModelProfile(
            source_model_id=model_id,
            served_model_name=model_id,
            display_name=f"Display {model_id}",
            capabilities=("text", "image"),
            max_model_len=4096,
            max_num_seqs=1,
            gpu_memory_utilization=0.7,
            generation_config="vllm",
            reasoning_parser=reasoning_parser,
            extra_args=("--no-enable-log-requests",),
            local_only=True,
        )

    def _settings(
        self,
        cache_root: Path,
        profiles,
    ):
        return app_module.Settings(
            bind_host="127.0.0.1",
            manager_port=8001,
            worker_host="127.0.0.1",
            worker_port=8002,
            profiles=tuple(profiles),
            default_model_id=profiles[0].served_model_name,
            api_key="",
            startup_timeout_sec=1.0,
            shutdown_timeout_sec=1.0,
            request_timeout_sec=1.0,
            sleep_enabled=True,
            auto_start=False,
            log_path=cache_root / "worker.log",
            catalog_path=cache_root / "models.json",
            hf_cache_root=cache_root,
        )

    def _cache(self, root: Path, model_id: str) -> None:
        config_path = (
            root
            / "hub"
            / f"models--{model_id.replace('/', '--')}"
            / "snapshots"
            / "test"
            / "config.json"
        )
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{}", encoding="utf-8")

    def test_catalog_parses_multiple_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "models.json"
            catalog.write_text(
                json.dumps(
                    {
                        "default_model_id": "org/vision-a",
                        "models": [
                            {
                                "source_model_id": "org/vision-a",
                                "display_name": "Vision A",
                            },
                            {
                                "source_model_id": "org/vision-b",
                                "reasoning_parser": "qwen3",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            profiles, default_id = app_module.Settings._load_profiles(catalog)

        self.assertEqual(default_id, "org/vision-a")
        self.assertEqual(
            [profile.source_model_id for profile in profiles],
            ["org/vision-a", "org/vision-b"],
        )
        self.assertEqual(profiles[1].reasoning_parser, "qwen3")

    def test_catalog_rows_expose_each_cached_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = [
                self._profile("org/vision-a"),
                self._profile("org/vision-b"),
            ]
            for profile in profiles:
                self._cache(root, profile.source_model_id)
            manager = app_module.RuntimeManager(
                self._settings(root, profiles)
            )

            rows = manager.model_rows()

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["selectable"] for row in rows))
        self.assertTrue(all(row["cached"] for row in rows))
        self.assertEqual([row["load_state"] for row in rows], ["unloaded"] * 2)

    def test_catalog_can_advertise_lora_client_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self._profile("org/vision-base")
            profile = app_module.ModelProfile(
                **{
                    **profile.__dict__,
                    "client_model_id": "instrument-lora",
                }
            )
            self._cache(root, profile.source_model_id)
            manager = app_module.RuntimeManager(
                self._settings(root, [profile])
            )

            row = manager.model_rows()[0]
            resolved = manager._resolve_profile("instrument-lora")

        self.assertEqual(row["id"], "instrument-lora")
        self.assertEqual(row["loaded_instances"], [])
        self.assertIs(resolved, profile)

    def test_missing_local_model_is_visible_but_not_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self._profile("org/missing")
            manager = app_module.RuntimeManager(
                self._settings(root, [profile])
            )

            row = manager.model_rows()[0]

        self.assertFalse(row["selectable"])
        self.assertEqual(row["available_actions"], [])
        self.assertIn("not present", row["detail"])

    def test_model_selection_uses_profile_and_forces_thinking_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._profile("org/vision-a", reasoning_parser="gemma4")
            second = self._profile("org/vision-b")
            self._cache(root, first.source_model_id)
            self._cache(root, second.source_model_id)
            manager = app_module.RuntimeManager(
                self._settings(root, [first, second])
            )

            with mock.patch.object(app_module.threading, "Thread") as thread:
                snapshot = manager.request_load(second.served_model_name)

            command = manager._worker_command(second)

        self.assertEqual(snapshot["model_id"], second.served_model_name)
        self.assertEqual(snapshot["state"], "loading")
        thread.return_value.start.assert_called_once()
        self.assertIn(second.source_model_id, command)
        self.assertNotIn("--reasoning-parser", command)
        self.assertEqual(
            command[-2:],
            ["--default-chat-template-kwargs", '{"enable_thinking": false}'],
        )

    def test_non_active_unload_does_not_stop_active_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._profile("org/vision-a")
            second = self._profile("org/vision-b")
            self._cache(root, first.source_model_id)
            self._cache(root, second.source_model_id)
            manager = app_module.RuntimeManager(
                self._settings(root, [first, second])
            )
            manager._state = "loaded"

            snapshot = manager.request_unload(second.served_model_name)

        self.assertEqual(snapshot["model_id"], second.served_model_name)
        self.assertEqual(snapshot["state"], "unloaded")
        self.assertEqual(manager.snapshot()["state"], "loaded")

    def test_custom_default_model_keeps_legacy_environment_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_catalog = Path(directory) / "missing.json"
            with mock.patch.dict(
                os.environ,
                {
                    "VLLM_MANAGER_CATALOG_PATH": str(missing_catalog),
                    "VLLM_MANAGER_MODEL_ID": "local/custom-vlm",
                    "VLLM_MANAGER_REASONING_PARSER": "",
                },
                clear=False,
            ):
                settings = app_module.Settings.from_environment()

        self.assertEqual(len(settings.profiles), 1)
        self.assertEqual(
            settings.profiles[0].source_model_id,
            "local/custom-vlm",
        )


if __name__ == "__main__":
    unittest.main()
