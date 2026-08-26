import { useId } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";

import type {
  ScenarioRevisionApplyAdmission,
  ScenarioRevisionState,
} from "../../ros/scenarioRevision";
import type { Language } from "../../utils/display";
import "./scenario-revision-control.css";

export type ScenarioRevisionControlProps = {
  language: Language;
  connected: boolean;
  selectedBundle: string;
  activeBundle: string;
  revision: ScenarioRevisionState;
  admission: ScenarioRevisionApplyAdmission;
  onPreview: () => void;
  onApply: () => void;
};

const DISPOSITION_COPY: Record<string, readonly [string, string]> = {
  preview_change_available: ["변경 있음", "Changes available"],
  preview_unchanged: ["최신 상태", "Up to date"],
  change_available: ["변경 있음", "Changes available"],
  unchanged: ["최신 상태", "Up to date"],
  applied: ["적용 완료", "Applied"],
  deferred: ["적용 보류", "Deferred"],
  blocked: ["적용 차단", "Blocked"],
  restart_required: ["재기동 필요", "Restart required"],
  rejected: ["서버 거부", "Rejected"],
};

function localized(value: readonly [string, string], language: Language) {
  return value[language === "ko" ? 0 : 1];
}

function revisionLabel(revision: string, language: Language): string {
  if (!revision) return language === "ko" ? "미수신" : "Not reported";
  const digest = revision.startsWith("sha256:") ? revision.slice(7) : revision;
  return digest.length > 14 ? `${digest.slice(0, 14)}…` : digest;
}

function dispositionLabel(disposition: string, language: Language): string {
  const known = DISPOSITION_COPY[disposition];
  if (known) return localized(known, language);
  return disposition.replace(/_/g, " ");
}

function localizedAdmissionReason(reason: string, language: Language): string {
  if (language !== "ko") return reason;
  const known: Record<string, string> = {
    "Select a procedure bundle first.": "먼저 수술 번들을 선택하세요.",
    "Wait for a fresh server runtime state before applying changes.": "서버의 최신 실행 상태를 수신한 뒤 적용할 수 있습니다.",
    "Wait for the current runtime request to finish.": "현재 실행 요청이 끝난 뒤 적용할 수 있습니다.",
    "Preview this bundle revision before applying it.": "먼저 선택한 번들의 변경 사항을 확인하세요.",
    "The selected bundle already matches the active revision.": "선택한 번들은 이미 현재 revision과 같습니다.",
    "Reloading the active bundle resets procedure state. Fully stop the scenario before applying it.": "현재 번들의 reload는 진행 상태를 초기화합니다. 시나리오를 완전히 정지한 뒤 적용하세요.",
    "Wait until all Live robot, recovery, and cleaner work is idle.": "Live 로봇·회수·정리 작업이 모두 idle이 될 때까지 기다리세요.",
    "Fully stop the Live scenario and active work before changing bundles.": "Live 시나리오와 활성 작업을 완전히 정지한 뒤 번들을 바꿀 수 있습니다.",
    "Pause or fully stop the scenario before changing bundles.": "시나리오를 일시정지하거나 완전히 정지한 뒤 번들을 바꿀 수 있습니다.",
  };
  return known[reason] ?? reason;
}

export function ScenarioRevisionControl({
  language,
  connected,
  selectedBundle,
  activeBundle,
  revision,
  admission,
  onPreview,
  onApply,
}: ScenarioRevisionControlProps) {
  const feedbackId = useId();
  const applyHelpId = useId();
  const busy = revision.phase === "previewing" || revision.phase === "applying";
  const resultMatchesSelection =
    revision.bundleName === selectedBundle &&
    revision.result?.activeBundle === activeBundle;
  const result = resultMatchesSelection ? revision.result : null;
  const previewDisabled = !connected || !selectedBundle || busy;
  const applyDisabled = !admission.allowed || busy;
  const feedbackTone = revision.phase === "failed"
    ? "failed"
    : revision.phase === "applied"
      ? "applied"
      : busy
        ? "pending"
        : revision.phase === "previewed"
          ? result?.changed ? "changed" : "unchanged"
          : "idle";
  const feedback = revision.bundleName === selectedBundle
    ? revision.message
    : "";
  const applyReason = localizedAdmissionReason(admission.reason, language);
  const title = language === "ko" ? "시나리오 revision" : "Scenario revision";

  return (
    <section
      aria-busy={busy}
      className="scenario-revision-control"
      data-phase={revision.phase}
      data-slot="scenario-revision-control"
    >
      <div className="scenario-revision-header">
        <strong>{title}</strong>
        {busy ? (
          <LoaderCircle aria-hidden="true" className="scenario-revision-spinner" size={18} />
        ) : revision.phase === "failed" ? (
          <AlertTriangle aria-hidden="true" size={18} />
        ) : revision.phase === "applied" ? (
          <CheckCircle2 aria-hidden="true" size={18} />
        ) : (
          <RefreshCw aria-hidden="true" size={18} />
        )}
      </div>

      <dl className="scenario-revision-summary">
        <div>
          <dt>{language === "ko" ? "현재 번들" : "Active bundle"}</dt>
          <dd>{activeBundle || (language === "ko" ? "미수신" : "Not reported")}</dd>
        </div>
        <div>
          <dt>{language === "ko" ? "확인 대상" : "Candidate bundle"}</dt>
          <dd>{selectedBundle || (language === "ko" ? "선택 필요" : "Select a bundle")}</dd>
        </div>
        <div>
          <dt>{language === "ko" ? "현재 revision" : "Active revision"}</dt>
          <dd>{revisionLabel(result?.activeRevision ?? "", language)}</dd>
        </div>
        <div>
          <dt>{language === "ko" ? "후보 revision" : "Candidate revision"}</dt>
          <dd>{revisionLabel(result?.candidateRevision ?? "", language)}</dd>
        </div>
      </dl>

      {result ? (
        <div
          className="scenario-revision-disposition"
          data-changed={result.changed ? "true" : "false"}
        >
          {result.changed ? <AlertTriangle aria-hidden="true" size={15} /> : <CheckCircle2 aria-hidden="true" size={15} />}
          <span>{dispositionLabel(result.disposition, language)}</span>
        </div>
      ) : null}

      <div className="scenario-revision-actions">
        <button
          aria-describedby={feedback ? feedbackId : undefined}
          className="button button-secondary"
          disabled={previewDisabled}
          onClick={() => {
            if (!previewDisabled) onPreview();
          }}
          type="button"
        >
          {revision.phase === "previewing" ? (
            <LoaderCircle aria-hidden="true" className="scenario-revision-spinner" size={16} />
          ) : (
            <RefreshCw aria-hidden="true" size={16} />
          )}
          {language === "ko"
            ? revision.phase === "failed" ? "다시 확인" : "변경 확인"
            : revision.phase === "failed" ? "Retry preview" : "Preview changes"}
        </button>
        <button
          aria-describedby={[applyHelpId, feedback ? feedbackId : ""].filter(Boolean).join(" ")}
          className="button button-primary"
          disabled={applyDisabled}
          onClick={() => {
            if (!applyDisabled) onApply();
          }}
          type="button"
        >
          {revision.phase === "applying" ? (
            <LoaderCircle aria-hidden="true" className="scenario-revision-spinner" size={16} />
          ) : (
            <CheckCircle2 aria-hidden="true" size={16} />
          )}
          {language === "ko" ? "revision 적용" : "Apply revision"}
        </button>
      </div>

      <div
        className={["scenario-revision-feedback", feedbackTone].join(" ")}
        id={feedbackId}
        role={feedbackTone === "failed" ? "alert" : "status"}
      >
        {feedback || (language === "ko"
          ? "실행 중에도 확인할 수 있으며 상태는 바뀌지 않습니다."
          : "Preview is read-only and available while running.")}
      </div>
      <div className="scenario-revision-authority" id={applyHelpId} role="note">
        <AlertTriangle aria-hidden="true" size={15} />
        <span>
          {applyReason || (language === "ko"
            ? "적용 직전 상태를 다시 확인하고 서버가 최종 결정합니다."
            : "State is rechecked before apply; the server decides.")}
        </span>
      </div>
    </section>
  );
}
