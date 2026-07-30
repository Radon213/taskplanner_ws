from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]


class AnnotationGuiEntrypointTest(unittest.TestCase):
    def test_hidden_feedback_state_cannot_override_hidden_attribute(self):
        stylesheet = (
            REPO_ROOT / "tools/real_surgery_annotation/web/styles.css"
        ).read_text(encoding="utf-8")
        self.assertIn("[hidden]", stylesheet)
        self.assertIn("display: none !important;", stylesheet)

    def test_ros_setup_runs_before_nounset(self):
        script = (
            REPO_ROOT / "tools/real_surgery_annotation/run_0704_5_gui.sh"
        ).read_text(encoding="utf-8")
        source_position = script.index("source /opt/ros/lyrical/setup.bash")
        nounset_position = script.index("set -u")
        self.assertLess(source_position, nounset_position)


if __name__ == "__main__":
    unittest.main()
