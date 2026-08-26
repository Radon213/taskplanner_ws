import { BrainCircuit, RadioTower } from "lucide-react";

import type {
  VlmRequestToolDetectionEvidence,
  VlmToolDetectionFreshness,
  VlmToolDetectionInstance,
  VlmToolDetectionView,
  VlmToolDetectionViewId,
  VlmToolDetectionVisualAlignment,
} from "../../hooks/useRosBridge";
import type { Language } from "../../utils/display";

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function detectionFreshnessCopy(
  freshness: VlmToolDetectionFreshness | undefined,
  language: Language,
): readonly [string, string] {
  const status = freshness?.status ?? "unavailable";
  const ko: Record<string, readonly [string, string]> = {
    fresh: ["새 독립 관측", "이 뷰의 새 구조화 관측을 포함했습니다. FLIR 픽셀 정렬은 사용하지 않았습니다."],
    missing: ["관측 없음", "도구가 없다는 뜻이 아닙니다."],
    stale: ["수신 만료", "도구가 없다는 뜻이 아닙니다."],
    unavailable: ["상태 미확인", "도구가 없다는 뜻이 아닙니다."],
  };
  const en: Record<string, readonly [string, string]> = {
    fresh: ["Fresh view-local", "Fresh structured observations from this view were included; no FLIR pixel alignment was used."],
    missing: ["No observation", "This does not mean no tool is present."],
    stale: ["Receive-stale", "This does not mean no tool is present."],
    unavailable: ["Status unavailable", "This does not mean no tool is present."],
  };
  return (language === "ko" ? ko : en)[status] ?? [status, status];
}

function visualAlignmentCopy(
  visualAlignment: VlmToolDetectionVisualAlignment | undefined,
  language: Language,
): readonly [string, string] {
  const status = visualAlignment?.status ?? "unavailable";
  const ko: Record<string, readonly [string, string]> = {
    aligned: ["시각 정렬됨", "진단용 영상 시각 비교가 정렬되었습니다."],
    not_compared_no_flir_reference: ["시각 비교 안 함", "FLIR 기준 프레임 없이도 이 뷰의 구조화 관측은 사용할 수 있습니다."],
    misaligned: ["시각 불일치", "영상 비교는 불일치지만 새 구조화 관측은 별도로 표시합니다."],
    not_compared_missing_source_timestamp: ["시각 비교 안 함", "도구 관측의 원본 시각이 없어 영상 비교만 생략했습니다."],
    not_compared_no_fresh_detector: ["시각 비교 안 함", "새 탐지기 프레임이 없어 영상 비교만 생략했습니다."],
    not_compared_stale_detector: ["시각 비교 안 함", "탐지기 프레임이 만료되어 영상 비교만 생략했습니다."],
    unavailable: ["시각 비교 미확인", "도구 존재 여부 판단에는 사용하지 않습니다."],
  };
  const en: Record<string, readonly [string, string]> = {
    aligned: ["Visual alignment", "Diagnostic frame-time comparison is aligned."],
    not_compared_no_flir_reference: ["Visual comparison skipped", "Fresh structured evidence from this view remains usable without a FLIR reference frame."],
    misaligned: ["Visual timestamp misaligned", "Frame comparison is misaligned; fresh structured evidence remains shown separately."],
    not_compared_missing_source_timestamp: ["Visual comparison skipped", "The detector source timestamp is unavailable, so only diagnostic frame comparison is skipped."],
    not_compared_no_fresh_detector: ["Visual comparison skipped", "No fresh detector frame was available for diagnostic comparison."],
    not_compared_stale_detector: ["Visual comparison skipped", "The detector frame was stale for diagnostic comparison."],
    unavailable: ["Visual comparison unavailable", "It is not used to infer tool presence."],
  };
  return (language === "ko" ? ko : en)[status] ?? [status, status];
}

function compactCoordinate(value: number): string {
  return value.toFixed(2);
}

function detectionBoxCopy(instance: VlmToolDetectionInstance, language: Language): string {
  const [left, top, right, bottom] = instance.bboxXyxyNorm;
  const [u, v] = instance.centerUvNorm;
  const box = `bbox [${compactCoordinate(left)}, ${compactCoordinate(top)} – ${compactCoordinate(right)}, ${compactCoordinate(bottom)}]`;
  const center = `uv [${compactCoordinate(u)}, ${compactCoordinate(v)}]`;
  const observationPoint = instance.observationPointUvNorm
    ? ` · ${language === "ko" ? "관측점" : "point"} [${compactCoordinate(instance.observationPointUvNorm[0])}, ${compactCoordinate(instance.observationPointUvNorm[1])}]`
    : "";
  const depth = instance.depthM === null
    ? ""
    : ` · ${language === "ko" ? "깊이" : "depth"} ${instance.depthM.toFixed(2)} m`;
  return `${box} · ${center}${observationPoint}${depth}`;
}

function detectionRequestStampCopy(evidence: VlmRequestToolDetectionEvidence, language: Language): string {
  const stamp = evidence.contextStampSec;
  if (stamp === null) return language === "ko" ? "요청 시각 미확인" : "Request timestamp unavailable";
  return `${language === "ko" ? "요청 stamp" : "Request stamp"} ${stamp.toFixed(3)}`;
}

function detectionViewName(view: VlmToolDetectionViewId): string {
  return view === "cam_3" ? "CAM3" : "CAM4";
}

function detectionStatusCopy(view: VlmToolDetectionView, language: Language): string {
  if (view.detectionStatus === "no_detections") {
    return language === "ko" ? "새 프레임 · 명시적 탐지 없음" : "Fresh frame · explicit no detections";
  }
  return language === "ko"
    ? `탐지 ${view.instances.length}개`
    : `${view.instances.length} detections`;
}

/**
 * Displays only the typed CAM3/CAM4 facts stored in the VLM request context.
 * It never visualizes a detector image or infers absence from a missing view.
 */
export function VlmStructuredToolDetectionEvidencePanel({
  evidence,
  language,
  className = "",
}: {
  evidence: VlmRequestToolDetectionEvidence | null;
  language: Language;
  className?: string;
}) {
  const viewIds: readonly VlmToolDetectionViewId[] = ["cam_3", "cam_4"];
  if (!evidence) {
    return (
      <section
        className={["operation-vlm-detection-evidence", "state-waiting", className].filter(Boolean).join(" ")}
        data-evidence-kind="structured-not-image"
        data-evidence-state="waiting"
        data-slot="vlm-tool-detection-evidence"
        aria-label={language === "ko" ? "VLM CAM3 CAM4 도구 위치 근거" : "VLM CAM3 CAM4 tool location evidence"}
      >
        <div className="operation-vlm-detection-header">
          <div>
            <span>{language === "ko" ? "VLM 도구 위치 근거" : "VLM tool location evidence"}</span>
            <strong>{language === "ko" ? "CAM3 · CAM4 구조화 관측 대기" : "Waiting for structured CAM3 · CAM4 observations"}</strong>
          </div>
          <RadioTower aria-hidden="true" size={18} strokeWidth={2.1} />
        </div>
        <p>
          {language === "ko"
            ? "VLM 요청 문맥이 도착하면 RF-DETR의 구조화된 박스 좌표만 표시합니다. 마스크·오버레이·탐지 이미지는 위치 근거로 사용하지 않습니다."
            : "When a VLM request context arrives, only typed RF-DETR box coordinates are shown. Masks, overlays, and detector images are not location evidence."}
        </p>
      </section>
    );
  }

  const viewById = new Map(evidence.views.map((view) => [view.view, view]));
  return (
    <section
      className={["operation-vlm-detection-evidence", "state-ready", className].filter(Boolean).join(" ")}
      data-evidence-kind="structured-not-image"
      data-evidence-state="ready"
      data-slot="vlm-tool-detection-evidence"
      data-vlm-request-stamp={evidence.contextStampSec ?? ""}
      aria-label={language === "ko" ? "VLM CAM3 CAM4 도구 위치 근거" : "VLM CAM3 CAM4 tool location evidence"}
    >
      <div className="operation-vlm-detection-header">
        <div>
          <span>{language === "ko" ? "VLM 도구 위치 근거" : "VLM tool location evidence"}</span>
          <strong>{language === "ko" ? "CAM3 · CAM4 RF-DETR 구조화 관측" : "CAM3 · CAM4 RF-DETR structured observations"}</strong>
          <small>{detectionRequestStampCopy(evidence, language)}</small>
        </div>
        <BrainCircuit aria-hidden="true" size={18} strokeWidth={2.1} />
      </div>
      <p className="operation-vlm-detection-boundary">
        {language === "ko"
          ? "위치 근거는 VLM 요청에 포함된 박스·중심 좌표입니다. 마스크·오버레이·탐지 이미지는 사용하지 않습니다."
          : "Location evidence is the box and center coordinates included in the VLM request. Masks, overlays, and detector images are not used."}
      </p>
      <div className="operation-vlm-detection-views">
        {viewIds.map((viewId) => {
          const freshness = evidence.freshness[viewId];
          const visualAlignment = evidence.visualAlignment[viewId];
          const view = viewById.get(viewId);
          const [freshnessLabel, freshnessDetail] = detectionFreshnessCopy(freshness, language);
          const [visualLabel, visualDetail] = visualAlignmentCopy(visualAlignment, language);
          const usable = freshness?.status === "fresh" && Boolean(view);
          return (
            <article
              className={["operation-vlm-detection-view", usable ? "state-fresh" : "state-unavailable"].join(" ")}
              data-detection-status={view?.detectionStatus ?? "not_observed"}
              data-freshness={freshness?.status ?? "unavailable"}
              data-view={viewId}
              data-visual-alignment={visualAlignment?.status ?? "unavailable"}
              key={viewId}
            >
              <header>
                <div>
                  <span>{detectionViewName(viewId)}</span>
                  <strong>{usable && view ? detectionStatusCopy(view, language) : freshnessLabel}</strong>
                  <small>{freshnessDetail}</small>
                </div>
                <em>{freshnessLabel}</em>
              </header>
              <p className="operation-vlm-detection-visual-diagnostic" title={visualDetail}>
                {language === "ko" ? "영상 정렬 진단" : "Visual diagnostic"}: {visualLabel}
              </p>
              {view ? (
                <>
                  <p className="operation-vlm-detection-view-meta">
                    {`${language === "ko" ? "원본 stamp" : "Source stamp"} ${view.sourceStampSec.toFixed(3)} · seq ${view.sequence}`}
                    {view.modelVersion ? ` · ${view.modelVersion}` : ""}
                  </p>
                  {view.detectionStatus === "detections" ? (
                    <ol>
                      {view.instances.map((instance, index) => (
                        <li
                          data-detection-class={instance.className}
                          data-detection-tool={instance.toolId || ""}
                          key={`${instance.toolId || instance.className}-${index}`}
                        >
                          <div>
                            <strong>{instance.toolId || instance.className}</strong>
                            {instance.toolId ? <small>{instance.className}</small> : null}
                            <small>{detectionBoxCopy(instance, language)}</small>
                          </div>
                          <em>{percent(instance.confidence)}</em>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className="operation-vlm-detection-empty">
                      {language === "ko"
                        ? "이 뷰의 새 정렬 프레임에서 명시적으로 탐지된 도구가 없습니다."
                        : "The fresh aligned frame for this view explicitly reported no detected tools."}
                    </p>
                  )}
                </>
              ) : null}
            </article>
          );
        })}
      </div>
    </section>
  );
}
