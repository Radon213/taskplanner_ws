from __future__ import annotations

import unittest
from pathlib import Path

from tools.real_surgery_annotation.audit_clinical_draft_batch import (
    DEFAULT_CASES,
    audit_batch,
)


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ARTIFACTS = {
    "0704_7": {
        "candidate_count": 16,
        "candidate_sha256": (
            "dbc9dd34f8f9efeb4d30c52e46f8e738a79b368b26d1e130381ed99625b15124"
        ),
        "manifest_sha256": (
            "756478214ad8413fca34dbfd174905c5babdd392125052e9f95b28a51d765fee"
        ),
    },
    "0704_8": {
        "candidate_count": 16,
        "candidate_sha256": (
            "721b82d4b906a348feffdad60f939ceeb7454d4c10d6c2a94105c0f54df9ebf5"
        ),
        "manifest_sha256": (
            "dff4716d4cef4fabb46fa57104a761745c7c77b8f10f670519fc86be869a838c"
        ),
    },
    "0704_9": {
        "candidate_count": 15,
        "candidate_sha256": (
            "c1c7b1b4b05abb36b1ab0888d8ced3d5e80580386c0fa045ae71fd160c067e56"
        ),
        "manifest_sha256": (
            "ba3cf52f001107dc1acb70db9df713d2c3f321891a8d8fc3a61c882ab3f4caa8"
        ),
    },
    "0704_10": {
        "candidate_count": 15,
        "candidate_sha256": (
            "267c4bf09ca844c8ede95ad3d7e25b3450ff2a37f199b974f39b596843ae1ccb"
        ),
        "manifest_sha256": (
            "679aa012bf322380e3a922f7068307efb6a4c71b4aeadbbcb3b13db5915cae0d"
        ),
    },
    "0704_11": {
        "candidate_count": 16,
        "candidate_sha256": (
            "7e983cf169c82332d8ca8018730c0eed3ba9e07023c3190f31dbb826ed4a25af"
        ),
        "manifest_sha256": (
            "7f111e22e0eac8722701784380348380c599e1c13fceea0243697e6bd5975a08"
        ),
    },
    "0704_12": {
        "candidate_count": 17,
        "candidate_sha256": (
            "35e6e11f7bd3230e02447d92eecb8c450a30dc07601c6216dec92c22881d4c75"
        ),
        "manifest_sha256": (
            "6265d9f446ae1ea6f02131c7f625776e2539ae212a3c95443ed3a98f147bce07"
        ),
    },
    "0704_13": {
        "candidate_count": 16,
        "candidate_sha256": (
            "b9522f0837fb1b84b44f10d140797411844d62df7f3437c18d4a590820738033"
        ),
        "manifest_sha256": (
            "3483daf6fb91f6aa8a5a4851f8b99f9e020098d5e8a662f54d78837d70d003a6"
        ),
    },
    "0704_14": {
        "candidate_count": 19,
        "candidate_sha256": (
            "58169daaf67f7ea05aa87499a8e4b8b8f5dfa004896fb3071357ef0d340551ca"
        ),
        "manifest_sha256": (
            "9501ed086159dc864acf0677723aed8e298e4c9961786d004baa34811d2b020d"
        ),
    },
    "0704_15": {
        "candidate_count": 18,
        "candidate_sha256": (
            "a02bfa6fedda9cf91b4278b71bbf071101f0019df5249daeac3deb04c720697c"
        ),
        "manifest_sha256": (
            "d0f6a53d186b52c26f6824808c594f49e7b72122d407530561379ffb8eb88866"
        ),
    },
    "0704_16": {
        "candidate_count": 16,
        "candidate_sha256": (
            "82f576277dd59af1adc2819347712a0a37b39e461035ba9888b720356ea76e17"
        ),
        "manifest_sha256": (
            "bd789af53518dc7ad7d8106a7b7af6e154659f0d4ece1a8e1ebbc735793bd240"
        ),
    },
    "0704_17": {
        "candidate_count": 17,
        "candidate_sha256": (
            "4c416e9687c41ec0ad49112dd95c7e8015a1f6b64bb13af77aab2b76e9581285"
        ),
        "manifest_sha256": (
            "2ded49bfbe402659dc311cf68ff6f3845ee257b822f271eb8e5eef06a91a4f8e"
        ),
    },
}


class Clinical07047To17ArtifactTest(unittest.TestCase):
    @staticmethod
    def _expected_media_error(case_id: str) -> str:
        return (
            "review media가 없습니다: "
            f"/home/arl/.cache/taskplanner_annotation/{case_id}"
            "/review_corrected.mp4"
        )

    def test_batch_repository_artifacts_are_hash_anchored(self) -> None:
        report = audit_batch(ROOT, DEFAULT_CASES)

        self.assertEqual(
            (11, 181),
            (
                report["counts"]["case_count"],
                report["counts"]["candidate_count"],
            ),
        )
        self.assertEqual(
            list(DEFAULT_CASES),
            [case["case_id"] for case in report["cases"]],
        )

        for case in report["cases"]:
            case_id = case["case_id"]
            expected = EXPECTED_ARTIFACTS[case_id]
            with self.subTest(case_id=case_id):
                self.assertEqual(
                    [],
                    [
                        error
                        for error in case["errors"]
                        if error != self._expected_media_error(case_id)
                    ],
                )
                self.assertEqual([], case["warnings"])
                self.assertEqual(
                    expected["candidate_count"],
                    case["candidate_count"],
                )
                self.assertEqual(
                    expected["candidate_sha256"],
                    case["candidate_sha256"],
                )
                self.assertEqual(
                    expected["manifest_sha256"],
                    case["manifest_sha256"],
                )
                self.assertEqual(
                    {"P03", "P04", "P05", "P06"},
                    set(case["phase_candidate_counts"]),
                )
                self.assertTrue(
                    all(
                        count > 0
                        for count in case["phase_candidate_counts"].values()
                    )
                )

    def test_batch_is_current_complete_and_hash_anchored(self) -> None:
        report = audit_batch(ROOT, DEFAULT_CASES)
        external_media_only = all(
            case["errors"] == [self._expected_media_error(case["case_id"])]
            for case in report["cases"]
        )
        if not report["ok"] and external_media_only:
            self.skipTest(
                "external review-media cache is not mounted; all repository "
                "artifacts, hashes, counts, and phase coverage were validated"
            )

        self.assertTrue(report["ok"])
        self.assertEqual(
            {
                "case_count": 11,
                "passed_case_count": 11,
                "failed_case_count": 0,
                "candidate_count": 181,
            },
            report["counts"],
        )
        for case in report["cases"]:
            with self.subTest(case_id=case["case_id"]):
                self.assertTrue(case["ok"])
                self.assertEqual([], case["errors"])


if __name__ == "__main__":
    unittest.main()
