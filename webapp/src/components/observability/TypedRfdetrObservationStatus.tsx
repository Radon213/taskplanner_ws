import { CheckCircle2, Clock3, Radio } from "lucide-react";

import type {
  TypedRfdetrToolDetectionFrame,
  TypedRfdetrToolDetections,
} from "../../hooks/useRosBridge";
import { CONFIGURED_RFDETR_PRODUCER_HOST } from "../../ros/rfdetrObservationSources";
import type { Language } from "../../utils/display";

function cameraStatus(
  frame: TypedRfdetrToolDetectionFrame | undefined,
  language: Language,
) {
  if (!frame) {
    return {
      state: "waiting",
      label: language === "ko" ? "수신 대기" : "Waiting",
      detail:
        language === "ko"
          ? "typed 메시지 수신 전 · publisher IP는 이 IDL로 검증할 수 없습니다."
          : "No typed message yet · this IDL cannot verify publisher IP.",
    } as const;
  }
  const objectLabel = language === "ko" ? "도구" : "tools";
  return {
    state: "ready",
    label: language === "ko" ? "typed 메시지 수신" : "Typed message received",
    detail: `${frame.instances.length} ${objectLabel} · ${language === "ko" ? "payload 모델" : "payload model"} ${frame.modelVersion || "unknown"}`,
  } as const;
}

/**
 * Production RF-DETR is observer-only. The host shown below is a reviewed
 * deployment-contract value, not DDS publisher-IP attestation. Typed message
 * receipt and payload model_version remain separately visible.
 */
export function TypedRfdetrObservationStatus({
  detections,
  language,
}: {
  detections: TypedRfdetrToolDetections;
  language: Language;
}) {
  const views = (["cam3", "cam4"] as const).map((cameraId) => {
    const frame = detections[cameraId];
    return { cameraId, frame, status: cameraStatus(frame, language) };
  });
  const readyCount = views.filter(({ status }) => status.state === "ready").length;

  return (
    <section
      aria-atomic="true"
      aria-live="polite"
      className="typed-rfdetr-status"
      data-control-surface="false"
      data-slot="typed-rfdetr-observation-status"
      data-configured-producer-host={CONFIGURED_RFDETR_PRODUCER_HOST}
      role="status"
    >
      <header>
        <div>
          <span className="typed-rfdetr-kicker">
            <Radio aria-hidden="true" size={14} strokeWidth={2.2} />
            {language === "ko" ? "구성된 생산자 계약" : "Configured producer contract"}
          </span>
          <strong>{CONFIGURED_RFDETR_PRODUCER_HOST} · {language === "ko" ? "배포 구성" : "deployment config"}</strong>
        </div>
        <span className={readyCount === views.length ? "ready" : "waiting"}>
          {readyCount}/{views.length} {language === "ko" ? "typed 수신" : "typed feeds"}
        </span>
      </header>
      <p>
        {language === "ko"
          ? "192.168.1.7은 배포 계약의 구성값이며, 이 DDS 메시지는 publisher IP를 증명하지 않습니다. 아래에는 typed 메시지 수신과 payload 모델 버전만 표시합니다."
          : "192.168.1.7 is deployment configuration, not publisher-IP attestation. Below, typed-message receipt and the payload model version are shown separately."}
      </p>
      <ul>
        {views.map(({ cameraId, frame, status }) => {
          const StateIcon = status.state === "ready" ? CheckCircle2 : Clock3;
          return (
            <li
              data-camera={cameraId}
              data-state={status.state}
              key={cameraId}
              title={frame?.topic}
            >
              <StateIcon aria-hidden="true" size={15} strokeWidth={2.2} />
              <span>{cameraId.toUpperCase()}</span>
              <strong>{status.label}</strong>
              <small>{status.detail}</small>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
