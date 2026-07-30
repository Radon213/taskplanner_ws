#!/usr/bin/env python3
"""Generate a create-only Korean summary for a fully audited Policy02 batch."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .finalize_interaction_review import publish_create_only
from .interaction_review_gui import FinalReviewBundle
from .publish_assistant_case_reference import encode_json, load_json, sha256_file


EXPECTED_CASES = [f"0704_{index}" for index in range(7, 18)]


def _relative(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(root.resolve()))
    except ValueError:
        return str(resolved)


def _require_clean(report: dict[str, Any], *, label: str) -> None:
    if report.get("ok") is not True:
        raise ValueError(f"{label} is not clean")


def build_summary(
    *,
    repo_root: Path,
    batch_audit_path: Path,
    coverage_audit_path: Path,
    marlin_batch_path: Path,
) -> str:
    repo_root = repo_root.resolve()
    batch = load_json(batch_audit_path)
    coverage = load_json(coverage_audit_path)
    marlin = load_json(marlin_batch_path)
    _require_clean(batch, label="final batch audit")
    _require_clean(coverage, label="Marlin coverage audit")
    audited_case_ids = [str(item.get("case_id")) for item in batch["cases"]]
    covered_case_ids = [str(item.get("case_id")) for item in coverage["cases"]]
    if audited_case_ids != EXPECTED_CASES or covered_case_ids != EXPECTED_CASES:
        raise ValueError(
            "summary requires ordered complete case set 0704_7 through 0704_17"
        )
    if (
        marlin.get("status") != "completed"
        or marlin.get("counts", {}).get("failed_or_blocked_count") != 0
        or marlin.get("concurrency", {}).get("hard_limit") != 2
    ):
        raise ValueError("Marlin batch is incomplete or not concurrency=2")

    rows: list[dict[str, Any]] = []
    for audited in batch["cases"]:
        case_id = str(audited["case_id"])
        manifest_path = repo_root / str(audited["manifest"])
        audited_manifest_sha256 = str(audited.get("manifest_sha256", ""))
        current_manifest_sha256 = sha256_file(manifest_path)
        if (
            not audited_manifest_sha256
            or current_manifest_sha256 != audited_manifest_sha256
        ):
            raise ValueError(f"{case_id}: manifest SHA-256 mismatch")
        current_bundle = FinalReviewBundle(manifest_path=manifest_path)
        audited_bundle_revision = str(audited.get("bundle_revision", ""))
        if (
            not audited_bundle_revision
            or current_bundle.revision != audited_bundle_revision
        ):
            raise ValueError(f"{case_id}: bundle revision mismatch")
        if sha256_file(manifest_path) != audited_manifest_sha256:
            raise ValueError(f"{case_id}: manifest changed during summary")
        manifest = current_bundle.manifest
        reference = manifest["evaluation_reference"]
        observed_counts = reference["observed_reference"]["event_type_counts"]
        dt_counts = reference["dt_reference"]["event_type_counts"]
        action_targets = int(audited["counts"]["action_targets"])
        effective_action_targets = int(
            audited["counts"].get("effective_action_targets", -1)
        )
        if effective_action_targets != action_targets:
            raise ValueError(
                f"{case_id}: raw/effective action target count mismatch"
            )
        rows.append(
            {
                "case_id": case_id,
                "requests": int(
                    observed_counts.get("implicit_tool_request", 0)
                ),
                "observed_transfers": int(
                    observed_counts.get("tool_transfer", 0)
                ),
                "observed": int(audited["counts"]["observed"]),
                "dt_transfers": int(dt_counts.get("tool_transfer", 0)),
                "dt": int(audited["counts"]["dt_reference"]),
                "gesture_targets": int(
                    audited["counts"]["gesture_targets"]
                ),
                "action_targets": action_targets,
                "effective_action_targets": effective_action_targets,
                "phase": int(audited["counts"]["phase"]),
                "voice": int(audited["counts"]["voice"]),
                "bundle_revision": audited_bundle_revision,
                "manifest_sha256": audited_manifest_sha256,
                "phase_file": str(reference["phase_reference"]["file"]),
                "masks_file": str(reference["evaluation_masks"]["file"]),
            }
        )

    totals = {
        key: sum(row[key] for row in rows)
        for key in (
            "requests",
            "observed_transfers",
            "observed",
            "dt_transfers",
            "dt",
            "gesture_targets",
            "action_targets",
            "effective_action_targets",
            "phase",
            "voice",
        )
    }
    marlin_counts = marlin["counts"]
    coverage_counts = coverage["counts"]
    model = marlin["model"]
    generated_at = datetime.now(timezone.utc).astimezone().isoformat()

    lines = [
        "# 0704_07–0704_17 Policy02 최종 어노테이션 배치 보고서",
        "",
        f"생성 시각: `{generated_at}`",
        "",
        "## 결론",
        "",
        (
            f"11개 영상의 전체 관측 구간을 Marlin-2B 2동시 proposal pass와 "
            f"Codex 5.6-sol exact-frame 3-pass로 처리했다. 최종 원시 관측은 "
            f"{totals['observed']}건(엄격한 손 요청 {totals['requests']}구간, "
            f"도구 이동 {totals['observed_transfers']}점), DT 참조는 "
            f"{totals['dt']}건이다. 행동 정답은 {totals['action_targets']}건, "
            f"gesture 정답은 {totals['gesture_targets']}건이다."
        ),
        "",
        (
            "모든 케이스는 development/calibration이며 held-out이 아니다. "
            "P03–P06은 VLM용 ontology 최적화 대상인 ambiguous context로만 "
            "포함되고 Phase accuracy에는 사용하지 않는다."
        ),
        "",
        "## 케이스별 최종 상태",
        "",
        (
            "| Case | 요청 | 관측 이동 | 관측 합계 | DT 이동 | DT 합계 | "
            "Gesture target | Action target | Phase | Voice | Bundle revision |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| {case_id} | {requests} | {observed_transfers} | {observed} | "
            "{dt_transfers} | {dt} | {gesture_targets} | {action_targets} | "
            "{phase} | {voice} | `{bundle_revision}` |".format(**row)
        )
    lines.extend(
        [
            (
                f"| **합계** | **{totals['requests']}** | "
                f"**{totals['observed_transfers']}** | "
                f"**{totals['observed']}** | **{totals['dt_transfers']}** | "
                f"**{totals['dt']}** | **{totals['gesture_targets']}** | "
                f"**{totals['action_targets']}** | **{totals['phase']}** | "
                f"**{totals['voice']}** |  |"
            ),
            "",
            "## Marlin 제안 생성",
            "",
            (
                f"- 모델: `{model['id']}` revision "
                f"`{model['revision']}`"
            ),
            (
                f"- 동시 실행 상한: {marlin['concurrency']['hard_limit']} "
                f"(실행 프로세스 {marlin['concurrency']['max_processes']})"
            ),
            (
                f"- 기본 transcript/full-scan job: "
                f"{marlin_counts['completed_count']}/"
                f"{marlin_counts['job_count']} 완료, 실패 0"
            ),
            (
                f"- 실제 full-scan run: {coverage_counts['scan_run_count']}, "
                f"완료 anchor {coverage_counts['completed_anchor_count']}"
            ),
            (
                "- gap을 분모에서 제외한 실제 clip 합집합 기준 observable "
                "coverage: 모든 11개 케이스 100%"
            ),
            "",
            "Marlin 결과는 모두 proposal-only이며 최종 정답 권한이 없다. "
            "generic full-span saturation, gap, clip-end invalid span은 Codex가 "
            "정확 프레임으로 다시 확인해 기각하거나 보충 실행했다.",
            "",
            "## DT 투영과 평가 범위",
            "",
            "- 허용 DT edge: `mayo_stand→scrub_nurse`, "
            "`scrub_nurse→surgeon`, `surgeon→mayo_stand`",
            "- `mayo→scrub→surgeon`은 두 물리 substep을 유지하되 surgeon "
            "도착만 action target이다.",
            "- `surgeon→scrub→mayo`는 동일 물체 연속성이 보인 경우에만 "
            "Mayo 도착 시각의 `surgeon→mayo`로 collapse한다.",
            "- scrub-only Mayo 정리 왕복, 불완결 반환, correction의 오도구, "
            "procedure completion 이후 cleanup은 행동 정답에서 제외한다.",
            "- 집도의/assistant는 demo contract상 `surgeon`으로 정규화하고 "
            "actor identity는 채점하지 않는다.",
            "- initial inventory와 instance 연속성이 충분하지 않아 state, "
            "physical feasibility, reuse/recover는 default-deny다.",
            "- 다음 도구 latency는 모든 정상 도착을 쓰는 "
            "`visual_anticipatory`와 tool-identifying voice "
            "`available_sec` 이후만 쓰는 `voice_causal`을 분리한다. "
            "완성 transcript가 늦다는 이유로 실제 visible handover를 "
            "삭제하지 않는다.",
            "- 모든 raw action target은 interval/cutoff mask precedence를 "
            "적용한 뒤에도 action·latency가 유효한지 별도로 감사한다.",
            "- 0704_7에는 procedure-completion 발화가 없어 마지막 명확한 "
            "task frame f1959를 cutoff로 쓰고 바로 다음 f1960부터 cleanup "
            "mask를 연다. 두 경계 사이는 카메라 frame 간격이며 평가 가능한 "
            "누락 frame은 없다.",
            "",
            "## Phase 상태",
            "",
            "- P03: clip 시작 시 이미 진행 중인 left-censored active state",
            "- P04: Army-Navy가 실제로 자리 잡고 견인이 지속되는 상태",
            "- P05: 안정적 견인 아래 표적 조직 조작이 지속되는 상태",
            "- P06: clamp/Allis/Mosquito 등의 국소 제어 뒤 energy pattern이 "
            "지속되는 상태",
            "- 경계는 요청·음성 correction·handover 점이 아니라, 이후 "
            "1–2초 multiview로 지속성이 확인되는 첫 상태 frame이다.",
            "",
            "현재 Phase는 전부 ambiguous/context-only다. 이 calibration "
            "영상으로 VLM 분리 가능성과 next-tool 응집도를 비교해 ontology를 "
            "수정·동결한 뒤, 새 영상으로 held-out 평가해야 한다.",
            "",
            "## 통합 검증",
            "",
            (
                f"- 최종 배치 감사: `{_relative(batch_audit_path, repo_root)}` "
                f"(11/11 PASS, SHA-256 `{sha256_file(batch_audit_path)}`)"
            ),
            (
                f"- Marlin coverage 감사: "
                f"`{_relative(coverage_audit_path, repo_root)}` "
                f"(SHA-256 `{sha256_file(coverage_audit_path)}`)"
            ),
            (
                f"- Marlin 실행 provenance: "
                f"`{_relative(marlin_batch_path, repo_root)}` "
                f"(SHA-256 `{sha256_file(marlin_batch_path)}`)"
            ),
            "",
            "감사는 manifest/hash, 실제 clip coverage, proposal/voice 전수 "
            "검토, 허용 DT edge, 원시 이벤트의 projection 1회 매핑, "
            "return/action 역할, Phase anchor 오염, default-deny mask, "
            "review timestamp와 정보경계를 fail-closed로 확인한다.",
            "",
            "## 정보경계",
            "",
            "Observed reference, DT reference, Phase timestamp, evaluation "
            "mask, projection/review log는 offline evaluator 전용이다. VLM, "
            "reducer, DT reducer, BT 또는 skill runtime 입력으로 전달해서는 "
            "안 된다.",
            "",
            "## 케이스 manifest",
            "",
        ]
    )
    for row in rows:
        lines.append(
            f"- `{row['case_id']}`: "
            f"`annotations/observable_tool_events/cases/"
            f"{row['case_id']}/annotation_manifest.json` "
            f"(SHA-256 `{row['manifest_sha256']}`, Phase "
            f"`{row['phase_file']}`, masks `{row['masks_file']}`)"
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--batch-audit", type=Path, required=True)
    parser.add_argument("--coverage-audit", type=Path, required=True)
    parser.add_argument("--marlin-batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        rendered = build_summary(
            repo_root=args.repo.resolve(),
            batch_audit_path=args.batch_audit.resolve(),
            coverage_audit_path=args.coverage_audit.resolve(),
            marlin_batch_path=args.marlin_batch.resolve(),
        )
        output = args.output.resolve()
        publish_create_only({output: rendered.encode("utf-8")})
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        encode_json(
            {
                "ok": True,
                "output": str(output),
                "sha256": sha256_file(output),
            }
        ).decode("utf-8"),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
