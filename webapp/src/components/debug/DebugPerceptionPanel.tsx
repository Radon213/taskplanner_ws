import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Axis3d,
  Boxes,
  CheckCircle2,
  Droplets,
  Hand,
  Layers3,
  Radar,
  ScanLine,
  ShieldCheck,
  Timer,
} from "lucide-react";

import type {
  DebugReadOnlyTopicSubscriber,
} from "../../hooks/useIntegrationDebugBridge";
import {
  DebugPerceptionDiagnostics,
  DebugPerceptionEvidenceState,
  DebugPerceptionHealth,
  DebugPerceptionTransportMode,
  DebugPerceptionAuthMode,
  DebugSupportPlaneDiagnostics,
  DebugToolPose,
  DEBUG_PERCEPTION_DIAGNOSTICS_TOPIC,
  DEBUG_PERCEPTION_FINAL_OVERLAY_STATUS_TOPIC,
  DEBUG_PERCEPTION_FINAL_OVERLAY_TOPIC,
  DEBUG_PERCEPTION_BLOOD_SEMANTICS_TOPIC,
  DEBUG_PERCEPTION_HAND_KEYPOINTS_TOPIC,
  DEBUG_PERCEPTION_HEALTH_TOPIC,
  DEBUG_PERCEPTION_TOOL_POSES_TOPIC,
} from "../../utils/debugPerceptionContract";
import { useDebugPerceptionBridge } from "../../hooks/useDebugPerceptionBridge";
import { DirectPerceptionOverlayPanel } from "./DirectPerceptionOverlayPanel";
import "./DebugPerceptionPanel.css";

interface DebugPerceptionPanelProps {
  subscribeTopic: DebugReadOnlyTopicSubscriber;
}

function cn(...names: string[]): string {
  return names.join(" ");
}

function ageLabel(receivedAt: number | undefined, now: number): string {
  if (!receivedAt) return "수신 전";
  const ageMs = Math.max(0, now - receivedAt);
  return ageMs < 1000 ? `${Math.round(ageMs)} ms 전` : `${(ageMs / 1000).toFixed(1)} s 전`;
}

function latencyLabel(value: number | undefined): string {
  return value === undefined ? "—" : `${value.toFixed(value >= 1000 ? 0 : 1)} ms`;
}

function algorithmLabel(algorithms: string[]): string {
  return algorithms.length ? algorithms.map((item) => item.toUpperCase()).join(" · ") : "없음";
}

const POSE_MODE_LABELS = [
  "INVALID",
  "POSITION_3D_ONLY",
  "PLANAR_4DOF_WITH_NORMAL_PRIOR",
  "FULL_6D",
  "AMBIGUOUS",
] as const;
const POSE_VALIDITY_LABELS = ["INVALID", "VALID", "DEGRADED", "STALE"] as const;
const DOF_LABELS = ["X", "Y", "Z", "ROLL", "PITCH", "YAW"] as const;
const HAND_JOINT_LABELS = [
  "WRIST",
  "THUMB CMC", "THUMB MCP", "THUMB IP", "THUMB TIP",
  "INDEX MCP", "INDEX PIP", "INDEX DIP", "INDEX TIP",
  "MIDDLE MCP", "MIDDLE PIP", "MIDDLE DIP", "MIDDLE TIP",
  "RING MCP", "RING PIP", "RING DIP", "RING TIP",
  "PINKY MCP", "PINKY PIP", "PINKY DIP", "PINKY TIP",
] as const;

function signedNumber(value: number, digits = 3): string {
  const rounded = Math.abs(value) < 0.5 * 10 ** -digits ? 0 : value;
  return `${rounded >= 0 ? "+" : ""}${rounded.toFixed(digits)}`;
}

function probabilityLabel(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function optionalProbabilityLabel(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : probabilityLabel(value);
}

function optionalDistanceLabel(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 1000).toFixed(2)} mm`;
}

function transportPresentation(mode: DebugPerceptionTransportMode | undefined): {
  label: string;
  detail: string;
  tone: "ready" | "waiting";
} {
  if (mode === "https") {
    return { label: "HTTPS · TLS", detail: "원격/LAN 암호화 전송", tone: "ready" };
  }
  if (mode === "http_local") {
    return { label: "LOCAL HTTP", detail: "loopback 전용 평문 개발 경로", tone: "ready" };
  }
  if (mode === "http_trusted_lan_dev") {
    return { label: "TRUSTED LAN HTTP", detail: "신뢰 LAN 개발 모드 · 평문", tone: "waiting" };
  }
  return { label: "대기", detail: "transport claim 수신 전", tone: "waiting" };
}

function authPresentation(mode: DebugPerceptionAuthMode | undefined): {
  label: string;
  detail: string;
  tone: "ready" | "waiting";
} {
  if (mode === "bearer") {
    return { label: "BEARER TOKEN", detail: "인증 헤더 사용", tone: "ready" };
  }
  if (mode === "none_local") {
    return { label: "LOCAL · NO TOKEN", detail: "loopback 한정 무인증", tone: "ready" };
  }
  if (mode === "none_trusted_lan_dev") {
    return { label: "LAN · NO TOKEN", detail: "신뢰 LAN 개발 모드 · 무인증", tone: "waiting" };
  }
  return { label: "대기", detail: "auth claim 수신 전", tone: "waiting" };
}

function supportPlanePresentation(
  diagnostics: DebugSupportPlaneDiagnostics | null,
  crossValidated: boolean,
): { tone: "success" | "warning" | "error" | "empty"; label: string } {
  if (!diagnostics) return { tone: "empty", label: "진단 대기" };
  if (!diagnostics.validationRequested) return { tone: "warning", label: "검증 미요청" };
  if (!diagnostics.artifactLoaded) return { tone: "error", label: "Artifact 없음" };
  if (!diagnostics.runtimeValidation.evaluated) {
    return { tone: "warning", label: "Live gate 미평가" };
  }
  return diagnostics.runtimeValidation.valid && crossValidated
    ? { tone: "success", label: "Live drift gate 유효" }
    : { tone: "error", label: "Live drift gate 무효" };
}

function toolPosePresentation(
  tool: DebugToolPose,
  supportPlaneValidated: boolean,
): { tone: "ready" | "position-only" | "invalid"; label: string; detail: string } {
  if (!tool.positionValid || tool.validity === 0 || tool.validity === 3) {
    return { tone: "invalid", label: "사용 불가", detail: "유효한 metric 위치가 없습니다." };
  }
  if (!tool.orientationValid) {
    return {
      tone: "position-only",
      label: "위치만",
      detail: "XYZ만 유효 · quaternion과 자세 축 사용 금지",
    };
  }
  if (!supportPlaneValidated) {
    return {
      tone: "invalid",
      label: "계약 불일치",
      detail: "support plane 미검증 · orientation 사용 금지",
    };
  }
  if (tool.poseMode === 2) {
    return {
      tone: "ready",
      label: "평면 제약 4DoF",
      detail: "XYZ + 평면 heading 관측 · /tf quaternion의 roll/pitch는 support-plane normal prior로 구성된 값",
    };
  }
  if (tool.poseMode === 3) {
    return { tone: "ready", label: "Full 6D", detail: "6개 DoF가 모두 관측됐습니다." };
  }
  return { tone: "invalid", label: "모호함", detail: "pose mode를 실행 입력으로 사용할 수 없습니다." };
}

function evidencePresentation(
  state: DebugPerceptionEvidenceState,
  health: DebugPerceptionHealth | null,
  diagnostics: DebugPerceptionDiagnostics | null,
): { tone: "success" | "warning" | "error" | "empty"; title: string } {
  if (state === "ready") {
    return diagnostics?.emptyDetectionResult
      ? { tone: "success", title: "실행 완료 · 검출 0건" }
      : { tone: "success", title: "Scalar 실행 증거 검증 가능" };
  }
  if (state === "disabled") return { tone: "warning", title: "PNU provider 비활성" };
  if (state === "stale") return { tone: "error", title: "인식 결과 만료" };
  if (state === "contract_error") return { tone: "error", title: "인식 계약 불일치" };
  if (state === "error") {
    return { tone: "error", title: health?.lastErrorCode || diagnostics?.errorCode || "PNU 추론 오류" };
  }
  if (state === "waiting_for_matching_raw") return { tone: "warning", title: "동일 stamp 증거 대기" };
  if (state === "waiting_for_overlay") return { tone: "warning", title: "새 diagnostics 대기" };
  if (state === "waiting_for_frame") return { tone: "empty", title: "CAM4 입력 대기" };
  return { tone: "empty", title: "PNU health 대기" };
}

export function DebugPerceptionPanel({ subscribeTopic }: DebugPerceptionPanelProps) {
  const {
    health,
    diagnostics,
    toolPoseEvidence,
    handEvidence,
    bloodEvidence,
    toolPoseDetail,
    handDetail,
    bloodDetail,
    toolPoseContractError,
    handContractError,
    bloodContractError,
    evidenceState,
    evidenceDetail,
  } = useDebugPerceptionBridge(subscribeTopic);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const presentation = evidencePresentation(evidenceState, health, diagnostics);
  const displayDiagnostics = diagnostics;
  const evidenceUsable = evidenceState !== "stale"
    && evidenceState !== "error"
    && evidenceState !== "contract_error";
  const usableHealth = evidenceUsable && health?.enabled && health.connected && !health.stale
    ? health
    : null;
  const usableDiagnostics = evidenceUsable
    && usableHealth
    && !displayDiagnostics?.errorCode
    && displayDiagnostics?.sourceStampKey === usableHealth.sourceStampKey
    ? displayDiagnostics
    : null;
  const executedZero = Boolean(
    usableDiagnostics
    && usableDiagnostics.executedAlgorithms.length > 0
    && usableDiagnostics.emptyDetectionResult,
  );
  const requestedAlgorithms = health?.requestedAlgorithms ?? displayDiagnostics?.requestedAlgorithms ?? [];
  const executedAlgorithms = usableDiagnostics?.executedAlgorithms ?? usableHealth?.executedAlgorithms ?? [];
  const metricReady = usableDiagnostics?.metric3dReady ?? usableHealth?.metric3dReady ?? false;
  const metricReasons = usableDiagnostics?.metric3dReasons ?? usableHealth?.metric3dReasons ?? [];
  const depthAligned = usableDiagnostics?.depthAligned ?? usableHealth?.depthAligned ?? false;
  const supportPlaneDiagnostics = usableDiagnostics?.supportPlaneDiagnostics ?? null;
  const supportPlaneValidated = Boolean(
    usableHealth?.supportPlaneValidated
    && usableDiagnostics?.supportPlaneValidated
    && supportPlaneDiagnostics?.runtimeValidation.valid,
  );
  const transport = transportPresentation(
    usableDiagnostics?.transportMode ?? usableHealth?.transportMode,
  );
  const auth = authPresentation(usableDiagnostics?.authMode ?? usableHealth?.authMode);
  const transportAuditVerified = Boolean(
    usableDiagnostics
    && usableHealth
    && usableDiagnostics.transportMode === usableHealth.transportMode
    && usableDiagnostics.authMode === usableHealth.authMode,
  );
  const supportPlane = supportPlanePresentation(
    supportPlaneDiagnostics,
    supportPlaneValidated,
  );
  const calibrationFit = supportPlaneDiagnostics?.calibrationFit ?? null;
  const runtimePlane = supportPlaneDiagnostics?.runtimeValidation ?? null;
  const exactToolPoseEvidence = evidenceUsable
    && !toolPoseContractError
    && toolPoseEvidence
    && usableDiagnostics
    && toolPoseEvidence.poses.sourceStampKey === usableDiagnostics.sourceStampKey
    && toolPoseEvidence.poses.frameId === usableDiagnostics.frameId
    ? toolPoseEvidence
    : null;
  const toolPoses = exactToolPoseEvidence?.poses.tools ?? [];
  const poseAxisCount = toolPoses.filter((tool) => tool.orientationValid).length;
  const positionOnlyCount = toolPoses.filter(
    (tool) => tool.positionValid && !tool.orientationValid,
  ).length;
  const posePanelTone = toolPoseContractError
    ? "error"
    : !exactToolPoseEvidence
      ? "empty"
      : toolPoses.length === 0 || poseAxisCount > 0
        ? "success"
        : "warning";
  const posePanelTitle = toolPoseContractError
    ? "Tool 자세 계약 불일치"
    : !exactToolPoseEvidence
      ? "ToolPoseArray 대기"
      : toolPoses.length === 0
        ? "실행 완료 · Tool 0건"
        : poseAxisCount > 0
          ? `자세 수치 ${poseAxisCount}건 검토 가능`
          : positionOnlyCount > 0
            ? `위치만 ${positionOnlyCount}건`
            : "사용 가능한 Tool 자세 없음";
  const exactHandEvidence = evidenceUsable
    && handEvidence
    && usableDiagnostics
    && handEvidence.result.sourceStampKey === usableDiagnostics.sourceStampKey
    && handEvidence.result.frameId === usableDiagnostics.frameId
    ? handEvidence
    : null;
  const exactBloodEvidence = evidenceUsable
    && bloodEvidence
    && usableDiagnostics
    && bloodEvidence.result.sourceStampNsKey === `${BigInt(usableDiagnostics.sourceStampSec) * 1_000_000_000n + BigInt(usableDiagnostics.sourceStampNanosec)}`
    && bloodEvidence.result.frameId === usableDiagnostics.frameId
    ? bloodEvidence
    : null;
  const hands = exactHandEvidence?.result.hands ?? [];
  const handDepthSource = exactHandEvidence?.result.depthSource ?? null;
  const bloodInstances = exactBloodEvidence?.result.detections ?? [];
  const handTone = handContractError ? "error" : exactHandEvidence ? "success" : "empty";
  const bloodTone = bloodContractError ? "error" : exactBloodEvidence ? "success" : "empty";
  const handTitle = handContractError
    ? "Hand 증거 계약 불일치"
    : exactHandEvidence
      ? hands.length === 0 ? "실행 완료 · Hand 0건" : `Hand ${hands.length}건 검토 가능`
      : "HandKeypoints 대기";
  const bloodTitle = bloodContractError
    ? "Blood 증거 계약 불일치"
    : exactBloodEvidence
      ? bloodInstances.length === 0 ? "실행 완료 · Blood 0건" : `Blood ${bloodInstances.length}건 검토 가능`
      : "Blood semantics 대기";

  return (
    <section className="debug-panel-stack" data-slot="debug-perception-panel">
      <DirectPerceptionOverlayPanel subscribeTopic={subscribeTopic} />

      <article className="debug-section-card debug-perception-card">
        <div className="debug-section-heading">
          <div>
            <p>OPTIONAL · PNU SCALAR EVIDENCE</p>
            <h2>Taskplanner PNU 중계 검증</h2>
            <span>상단의 shared final raster와 별개로 health·diagnostics·typed 결과를 동일 ROS source stamp와 frame_id에서만 결합합니다.</span>
          </div>
          <span className={cn("debug-perception-state-badge", presentation.tone)}>
            {presentation.tone === "success"
              ? <CheckCircle2 size={16} aria-hidden="true" />
              : presentation.tone === "empty"
                ? <Radar size={16} aria-hidden="true" />
                : <AlertTriangle size={16} aria-hidden="true" />}
            {presentation.title}
          </span>
        </div>

        <div className="debug-perception-layout">
          <aside className="debug-perception-shared-raster-note" data-slot="debug-perception-image-consumer-disabled" role="status">
            <Layers3 size={20} aria-hidden="true" />
            <div>
              <strong>영상 소비는 상단 final raster 한 장으로 제한됩니다.</strong>
              <span>이 scalar 검증 영역은 원본·검출·자세 이미지 토픽을 구독하지 않습니다. 레이어 활성/누락 상태는 상단 server status를 확인하세요.</span>
              <code>{DEBUG_PERCEPTION_FINAL_OVERLAY_TOPIC}</code>
            </div>
          </aside>
          <div className="debug-perception-summary">
            <div
              className={cn("debug-state-message", presentation.tone)}
              data-slot="debug-perception-evidence-state"
              role={presentation.tone === "error" ? "alert" : "status"}
            >
              {presentation.tone === "success"
                ? <CheckCircle2 size={19} aria-hidden="true" />
                : <AlertTriangle size={19} aria-hidden="true" />}
              <div>
                <strong>{presentation.title}</strong>
                <span>{evidenceDetail}</span>
              </div>
            </div>

            <dl className="debug-perception-contract-grid" aria-label="PNU 인식 실행 계약">
              <div>
                <dt>PROVIDER</dt>
                <dd>{health?.provider === "pnu_hand_blood" ? "PNU hand-blood-tools" : "대기"}</dd>
                <dd className="debug-perception-contract-detail">
                  {health ? `${health.status} · ${ageLabel(health.receivedAt, now)}` : "health 수신 전"}
                </dd>
              </div>
              <div>
                <dt>MODEL</dt>
                <dd data-slot="debug-perception-model-state">
                  {usableHealth?.modelReady
                    ? "READY"
                    : evidenceUsable
                      ? health ? "NOT READY" : "대기"
                      : "검증 보류"}
                </dd>
                <dd className="debug-perception-contract-detail" title={usableDiagnostics?.modelVersion}>
                  {usableDiagnostics?.modelVersion || (evidenceUsable ? "버전 수신 전" : "현재 모델 증거 없음")}
                </dd>
              </div>
              <div>
                <dt>REQUESTED</dt>
                <dd>{algorithmLabel(requestedAlgorithms)}</dd>
                <dd className="debug-perception-contract-detail">구성된 알고리즘</dd>
              </div>
              <div>
                <dt>EXECUTED</dt>
                <dd data-slot="debug-perception-executed-state">
                  {evidenceUsable ? algorithmLabel(executedAlgorithms) : "검증 보류"}
                </dd>
                <dd className="debug-perception-contract-detail">
                  {executedZero ? "정상 실행 · 검출 0건" : executedAlgorithms.length ? "응답 검증 완료" : "실행 증거 없음"}
                </dd>
              </div>
              <div data-slot="debug-perception-transport">
                <dt>TRANSPORT</dt>
                <dd className={transport.tone}>{transport.label}</dd>
                <dd className="debug-perception-contract-detail">
                  {transportAuditVerified ? `${transport.detail} · health/diagnostics 일치` : transport.detail}
                </dd>
              </div>
              <div data-slot="debug-perception-auth">
                <dt>AUTH</dt>
                <dd className={auth.tone}>{auth.label}</dd>
                <dd className="debug-perception-contract-detail">
                  {transportAuditVerified ? `${auth.detail} · 교차검증 완료` : auth.detail}
                </dd>
              </div>
            </dl>
          </div>
        </div>
      </article>

      <article className="debug-section-card debug-tool-pose-section" data-slot="debug-tool-pose-section">
        <div className="debug-section-heading">
          <div>
            <p>TOOL POSE · CAMERA FRAME</p>
            <h2>수술 도구 자세 검토</h2>
            <span>표시값은 CAM4 color optical frame 기준이며 Taskplanner 명령 권한이 아닙니다.</span>
          </div>
          <span className={cn("debug-perception-state-badge", posePanelTone)} data-slot="debug-tool-pose-readiness">
            {posePanelTone === "success"
              ? <CheckCircle2 size={16} aria-hidden="true" />
              : posePanelTone === "empty"
                ? <Radar size={16} aria-hidden="true" />
                : <AlertTriangle size={16} aria-hidden="true" />}
            {posePanelTitle}
          </span>
        </div>

        <div
          className={cn("debug-state-message", posePanelTone)}
          data-slot="debug-pose-contract-state"
          role={toolPoseContractError ? "alert" : "status"}
        >
          {posePanelTone === "success"
            ? <CheckCircle2 size={19} aria-hidden="true" />
            : <AlertTriangle size={19} aria-hidden="true" />}
          <div>
            <strong>{posePanelTitle}</strong>
            <span>{toolPoseDetail}</span>
          </div>
        </div>

        {exactToolPoseEvidence ? (
          <dl className="debug-tool-pose-array-meta" aria-label="ToolPoseArray 메타데이터">
            <div><dt>FRAME</dt><dd><code>{exactToolPoseEvidence.poses.frameId}</code></dd></div>
            <div><dt>SEQUENCE</dt><dd>{exactToolPoseEvidence.poses.sequence}</dd></div>
            <div><dt>MODEL</dt><dd title={exactToolPoseEvidence.poses.modelVersion}>{exactToolPoseEvidence.poses.modelVersion}</dd></div>
            <div><dt>ONTOLOGY</dt><dd title={exactToolPoseEvidence.poses.ontologyVersion}>{exactToolPoseEvidence.poses.ontologyVersion}</dd></div>
            <div><dt>CALIBRATION</dt><dd title={exactToolPoseEvidence.poses.calibrationVersion}>{exactToolPoseEvidence.poses.calibrationVersion}</dd></div>
            <div><dt>CONVENTION</dt><dd title={exactToolPoseEvidence.poses.poseConventionVersion}>{exactToolPoseEvidence.poses.poseConventionVersion}</dd></div>
          </dl>
        ) : null}

        {exactToolPoseEvidence && exactToolPoseEvidence.diagnostics.poseOverlayPublished !== null ? (
          <p className="debug-perception-reason" data-slot="debug-pose-overlay-status">
            Server final overlay pose layer {exactToolPoseEvidence.diagnostics.poseOverlayStatus.toUpperCase()}
            {exactToolPoseEvidence.diagnostics.poseOverlayPublished
              ? ` · Axes ${exactToolPoseEvidence.diagnostics.poseOverlayDrawnAxisCount} · Position-only ${exactToolPoseEvidence.diagnostics.poseOverlayDrawnPositionOnlyCount}`
              : " · 새 자세 오버레이 미발행"}
            {exactToolPoseEvidence.diagnostics.poseOverlayTruncated ? " · TRUNCATED" : ""}
            {exactToolPoseEvidence.diagnostics.poseOverlayRenderEncodeLatencyMs === null
              ? ""
              : ` · ${latencyLabel(exactToolPoseEvidence.diagnostics.poseOverlayRenderEncodeLatencyMs)}`}
          </p>
        ) : null}

        {exactToolPoseEvidence && toolPoses.length === 0 ? (
          <div className="debug-tool-pose-empty" data-slot="debug-tool-pose-empty" role="status">
            <Axis3d size={24} aria-hidden="true" />
            <div>
              <strong>정상 empty result</strong>
              <span>Tool 모델과 typed pose 경로는 실행됐고 현재 장면의 Tool은 0건입니다.</span>
            </div>
          </div>
        ) : null}

        {toolPoses.length > 0 ? (
          <ol className="debug-tool-pose-list" data-slot="debug-tool-pose-list">
            {toolPoses.map((tool) => {
              const pose = toolPosePresentation(tool, supportPlaneValidated);
              return (
                <li
                  className={cn("debug-tool-pose-card", pose.tone)}
                  data-pose-readiness={pose.tone}
                  data-slot="debug-tool-pose-card"
                  key={tool.frameLocalInstanceId}
                >
                  <header>
                    <div>
                      <span>INSTANCE {tool.frameLocalInstanceId}</span>
                      <h3>{tool.className}</h3>
                      <p>class #{tool.canonicalClassId} · model index {tool.modelClassIndex} · {probabilityLabel(tool.classConfidence)}</p>
                    </div>
                    <span className={cn("debug-tool-pose-status", pose.tone)}>
                      {pose.tone === "ready"
                        ? <CheckCircle2 size={15} aria-hidden="true" />
                        : <AlertTriangle size={15} aria-hidden="true" />}
                      {pose.label}
                    </span>
                  </header>
                  <p className="debug-tool-pose-explanation">{pose.detail}</p>

                  <div className="debug-tool-pose-transform">
                    <div>
                      <span>POSITION · m</span>
                      {tool.positionValid ? (
                        <code>X {signedNumber(tool.position.x)} · Y {signedNumber(tool.position.y)} · Z {signedNumber(tool.position.z)}</code>
                      ) : <strong>INVALID · 값 사용 금지</strong>}
                    </div>
                    <div>
                      <span>QUATERNION · x y z w</span>
                      {tool.orientationValid && supportPlaneValidated ? (
                        <code>{signedNumber(tool.orientation.x, 4)} · {signedNumber(tool.orientation.y, 4)} · {signedNumber(tool.orientation.z, 4)} · {signedNumber(tool.orientation.w, 4)}</code>
                      ) : <strong>미검증 · 표시/사용 금지</strong>}
                    </div>
                  </div>

                  <dl className="debug-tool-pose-facts">
                    <div><dt>POSE MODE</dt><dd>{POSE_MODE_LABELS[tool.poseMode]}</dd></div>
                    <div><dt>VALIDITY</dt><dd>{POSE_VALIDITY_LABELS[tool.validity]}</dd></div>
                    <div><dt>POSITION</dt><dd className={tool.positionValid ? "ready" : "waiting"}>{tool.positionValid ? "VALID" : "INVALID"}</dd></div>
                    <div><dt>ORIENTATION</dt><dd className={tool.orientationValid && supportPlaneValidated ? "ready" : "waiting"}>{tool.orientationValid && supportPlaneValidated ? "VALID" : "INVALID"}</dd></div>
                    <div><dt>POSE CONFIDENCE</dt><dd>{probabilityLabel(tool.poseConfidence)} · {tool.poseConfidenceCalibrated ? "CALIBRATED" : "UNCALIBRATED"}</dd></div>
                    <div><dt>SYMMETRY</dt><dd>{tool.symmetryType || "미지정"}</dd></div>
                  </dl>

                  <div className="debug-tool-pose-dof" aria-label={`Tool ${tool.frameLocalInstanceId} 관측 자유도`}>
                    <span>OBSERVED DOF</span>
                    <div>
                      {DOF_LABELS.map((label, index) => (
                        <span className={tool.dofObserved[index] ? "observed" : "prior"} key={label}>
                          {label} {tool.dofObserved[index] ? "관측" : "미관측"}
                        </span>
                      ))}
                    </div>
                  </div>

                  <dl className="debug-tool-pose-quality" aria-label={`Tool ${tool.frameLocalInstanceId} 자세 품질`}>
                    <div><dt>VALID DEPTH</dt><dd>{probabilityLabel(tool.validDepthRatio)}</dd></div>
                    <div><dt>POSE POINTS</dt><dd>{tool.posePointCount}</dd></div>
                    <div><dt>AXIS ANISOTROPY</dt><dd>{tool.axisAnisotropy.toFixed(3)}</dd></div>
                    <div><dt>ENDPOINT SIGN</dt><dd>{probabilityLabel(tool.endpointSignConfidence)}</dd></div>
                    <div><dt>PLANE INLIER</dt><dd>{probabilityLabel(tool.supportPlaneInlierRatio)}</dd></div>
                    <div><dt>PLANE RESIDUAL P95</dt><dd>{(tool.supportPlaneResidualP95M * 1000).toFixed(2)} mm</dd></div>
                  </dl>

                  <div className="debug-tool-pose-evidence">
                    <p><strong>P_obs</strong> u {tool.observationPointUvPx[0].toFixed(1)} · v {tool.observationPointUvPx[1].toFixed(1)} · mask {tool.observationPointInsideMask ? "inside" : "outside"} · depth {tool.observationPointDepthValid ? "valid" : "invalid"}</p>
                    <p title={tool.observationPointDefinition}>{tool.observationPointSelectionMode || "selection mode 없음"} · boundary {tool.observationPointBoundaryClearancePx.toFixed(1)} px</p>
                    <p title={tool.axisDefinition}><strong>AXIS</strong> {tool.axisDefinition || "미검증"}</p>
                  </div>

                  <div className="debug-tool-pose-flags">
                    <strong>FLAGS</strong>
                    {tool.statusFlags.length ? tool.statusFlags.map((flag) => <code key={flag}>{flag}</code>) : <span>없음</span>}
                    {tool.invalidReason ? <p>{tool.invalidReason}</p> : null}
                  </div>
                </li>
              );
            })}
          </ol>
        ) : null}

        <div className="debug-tool-pose-axis-legend" aria-label="자세 축 색상 범례">
          <span><i className="x" />+X 빨강</span>
          <span><i className="y" />+Y 초록</span>
          <span><i className="z" />+Z 파랑</span>
              <strong>관측은 평면 제약 4DoF이며, /tf quaternion의 roll/pitch는 support-plane normal prior로 구성됩니다.</strong>
        </div>
      </article>

      <div className="debug-perception-semantic-grid">
        <article className="debug-section-card debug-semantic-evidence-card" data-slot="debug-hand-evidence">
          <div className="debug-section-heading">
            <div>
              <p>HAND KEYPOINTS · CAMERA FRAME</p>
              <h2>손 21-joint · palm 자세</h2>
              <span>Typed HandKeypoints를 health/diagnostics와 exact source stamp로 결합합니다.</span>
            </div>
            <span className={cn("debug-perception-state-badge", handTone)} data-slot="debug-hand-state">
              {handTone === "success"
                ? <CheckCircle2 size={16} aria-hidden="true" />
                : handTone === "empty"
                  ? <Radar size={16} aria-hidden="true" />
                  : <AlertTriangle size={16} aria-hidden="true" />}
              {handTitle}
            </span>
          </div>
          <div className={cn("debug-state-message", handTone)} role={handContractError ? "alert" : "status"}>
            {handTone === "success"
              ? <CheckCircle2 size={19} aria-hidden="true" />
              : <AlertTriangle size={19} aria-hidden="true" />}
            <div><strong>{handTitle}</strong><span>{handDetail}</span></div>
          </div>
          <p className="debug-semantic-monitor-boundary">
            Hand 3D joint와 palm pose는 CAM4 optical frame의 monitor-only 증거입니다. Robot/world/TCP pose나 Taskplanner 실행 권한이 아닙니다.
          </p>
          {exactHandEvidence ? (
            <dl className="debug-semantic-meta" aria-label="HandKeypoints 프레임 메타데이터">
              <div><dt>COUNT</dt><dd>{hands.length}</dd></div>
              <div><dt>DEPTH SOURCE</dt><dd>{exactHandEvidence.result.depthSource.toUpperCase()}</dd></div>
              <div><dt>FRAME</dt><dd><code>{exactHandEvidence.result.frameId}</code></dd></div>
              <div><dt>SOURCE STAMP</dt><dd><code>{exactHandEvidence.result.sourceStampKey}</code></dd></div>
            </dl>
          ) : null}
          {exactHandEvidence && hands.length === 0 ? (
            <div className="debug-semantic-empty" data-slot="debug-hand-empty" role="status">
              <Hand size={22} aria-hidden="true" />
              <div><strong>정상 empty result</strong><span>Hand 모델은 exact stamp에서 실행됐고 검출은 0건입니다.</span></div>
            </div>
          ) : null}
          {hands.length ? (
            <ol className="debug-hand-list" data-slot="debug-hand-list">
              {hands.map((hand) => {
                const validDepthCount = hand.joints.filter((joint) => joint.validDepth).length;
                return (
                  <li className="debug-hand-card" key={hand.handIndex}>
                    <header>
                      <div><span>HAND {hand.handIndex}</span><h3>{hand.hasHandedness ? hand.handednessLabel : "Handedness 미분류"}</h3></div>
                      <strong>{hand.hasHandedness ? probabilityLabel(hand.handednessScore) : "—"}</strong>
                    </header>
                    <dl className="debug-hand-summary">
                      <div><dt>VALID DEPTH</dt><dd>{validDepthCount} / 21</dd></div>
                      <div><dt>PALM 6D</dt><dd className={hand.hasPalm6d ? "ready" : "waiting"}>{hand.hasPalm6d ? "AVAILABLE" : "UNAVAILABLE"}</dd></div>
                      <div><dt>DEPTH SOURCE</dt><dd>{handDepthSource?.toUpperCase() ?? "—"}</dd></div>
                    </dl>
                    {hand.palm6d ? (
                      <div className="debug-hand-palm" data-slot="debug-hand-palm">
                        <div><span>TRANSLATION · m</span><code>X {signedNumber(hand.palm6d.translation.x)} · Y {signedNumber(hand.palm6d.translation.y)} · Z {signedNumber(hand.palm6d.translation.z)}</code></div>
                        <div><span>QUATERNION · x y z w</span><code>{signedNumber(hand.palm6d.orientation.x, 4)} · {signedNumber(hand.palm6d.orientation.y, 4)} · {signedNumber(hand.palm6d.orientation.z, 4)} · {signedNumber(hand.palm6d.orientation.w, 4)}</code></div>
                      </div>
                    ) : null}
                    <details className="debug-hand-details">
                      <summary>21-joint · rotation matrix 상세</summary>
                      {hand.palm6d ? (
                        <div className="debug-hand-rotation" aria-label={`Hand ${hand.handIndex} palm rotation matrix`}>
                          <span>PALM ROTATION · ROW-MAJOR 3×3</span>
                          {[0, 1, 2].map((row) => (
                            <code key={row}>
                              {hand.palm6d?.rotationMatrix.slice(row * 3, row * 3 + 3).map((value) => signedNumber(value, 4)).join("  ")}
                            </code>
                          ))}
                        </div>
                      ) : null}
                      <ol className="debug-hand-joints" data-slot="debug-hand-joints">
                        {hand.joints.map((joint) => (
                          <li data-depth-valid={joint.validDepth ? "true" : "false"} key={joint.index}>
                            <div><strong>{joint.index}. {HAND_JOINT_LABELS[joint.index]}</strong><span>score {probabilityLabel(joint.score)}</span></div>
                            <code>UV {joint.u.toFixed(1)}, {joint.v.toFixed(1)} px</code>
                            {joint.validDepth
                              ? <code>XYZ {signedNumber(joint.x)}, {signedNumber(joint.y)}, {signedNumber(joint.z)} m</code>
                              : <span>3D depth invalid · XYZ 사용 금지</span>}
                          </li>
                        ))}
                      </ol>
                    </details>
                  </li>
                );
              })}
            </ol>
          ) : null}
        </article>

        <article className="debug-section-card debug-semantic-evidence-card" data-slot="debug-blood-evidence">
          <div className="debug-section-heading">
            <div>
              <p>BLOOD SEMANTICS · CAMERA FRAME</p>
              <h2>혈액 centroid · metric depth</h2>
              <span>Lossless source_stamp_ns 기반 String JSON을 health/diagnostics와 결합합니다.</span>
            </div>
            <span className={cn("debug-perception-state-badge", bloodTone)} data-slot="debug-blood-state">
              {bloodTone === "success"
                ? <CheckCircle2 size={16} aria-hidden="true" />
                : bloodTone === "empty"
                  ? <Radar size={16} aria-hidden="true" />
                  : <AlertTriangle size={16} aria-hidden="true" />}
              {bloodTitle}
            </span>
          </div>
          <div className={cn("debug-state-message", bloodTone)} role={bloodContractError ? "alert" : "status"}>
            {bloodTone === "success"
              ? <CheckCircle2 size={19} aria-hidden="true" />
              : <AlertTriangle size={19} aria-hidden="true" />}
            <div><strong>{bloodTitle}</strong><span>{bloodDetail}</span></div>
          </div>
          <p className="debug-semantic-monitor-boundary">
            Blood centroid와 depth는 CAM4 optical frame의 monitor-only 관측값입니다. Robot/world/TCP pose나 흡인 목표·실행 권한이 아닙니다.
          </p>
          {exactBloodEvidence ? (
            <dl className="debug-blood-combined" data-slot="debug-blood-combined" aria-label="Blood combined centroid">
              <div><dt>INSTANCE COUNT</dt><dd>{bloodInstances.length}</dd></div>
              <div><dt>METRIC 3D</dt><dd className={exactBloodEvidence.result.metric3dReady ? "ready" : "waiting"}>{exactBloodEvidence.result.metric3dReady ? "READY" : "NOT READY"}</dd></div>
              <div><dt>COMBINED XY · px</dt><dd>{exactBloodEvidence.result.combinedCentroidXyPx ? `${exactBloodEvidence.result.combinedCentroidXyPx[0].toFixed(1)}, ${exactBloodEvidence.result.combinedCentroidXyPx[1].toFixed(1)}` : "—"}</dd></div>
              <div><dt>COMBINED Z · m</dt><dd>{exactBloodEvidence.result.combinedCentroidDepthValid && exactBloodEvidence.result.combinedCentroidDepthM !== null ? exactBloodEvidence.result.combinedCentroidDepthM.toFixed(4) : "INVALID · 사용 금지"}</dd></div>
              <div className="debug-blood-stamp"><dt>SOURCE STAMP NS</dt><dd><code>{exactBloodEvidence.result.sourceStampNsKey}</code></dd></div>
            </dl>
          ) : null}
          {exactBloodEvidence && bloodInstances.length === 0 ? (
            <div className="debug-semantic-empty" data-slot="debug-blood-empty" role="status">
              <Droplets size={22} aria-hidden="true" />
              <div><strong>정상 empty result</strong><span>Blood 모델은 exact stamp에서 실행됐고 검출은 0건입니다.</span></div>
            </div>
          ) : null}
          {bloodInstances.length ? (
            <ol className="debug-blood-list" data-slot="debug-blood-list">
              {bloodInstances.map((instance) => (
                <li key={instance.instanceId}>
                  <header><div><span>INSTANCE {instance.instanceId}</span><h3>Blood</h3></div><strong>{probabilityLabel(instance.confidence)}</strong></header>
                  <dl>
                    <div><dt>CENTROID XY · px</dt><dd>{instance.centroidXyPx[0].toFixed(1)}, {instance.centroidXyPx[1].toFixed(1)}</dd></div>
                    <div><dt>DEPTH Z · m</dt><dd className={instance.centroidDepthValid ? "ready" : "waiting"}>{instance.centroidDepthValid && instance.centroidDepthM !== null ? instance.centroidDepthM.toFixed(4) : "INVALID · 사용 금지"}</dd></div>
                    <div><dt>BBOX XYXY · px</dt><dd>{instance.bboxXyxyPx.map((value) => value.toFixed(1)).join(", ")}</dd></div>
                  </dl>
                </li>
              ))}
            </ol>
          ) : null}
        </article>
      </div>

      <div className="debug-perception-detail-grid">
        <article
          className="debug-section-card debug-support-plane-card"
          data-live-valid={supportPlaneValidated ? "true" : "false"}
          data-slot="debug-support-plane-audit"
        >
          <div className="debug-section-heading">
            <div>
              <p>SUPPORT PLANE AUDIT</p>
              <h2>정적 보정 · 실시간 드리프트 게이트</h2>
              <span>Calibration artifact의 fit과 현재 CAM4 frame의 live 검증을 분리합니다.</span>
            </div>
            <span
              className={cn("debug-perception-state-badge", supportPlane.tone)}
              data-slot="debug-support-plane-state"
              role={supportPlane.tone === "error" ? "alert" : "status"}
            >
              {supportPlane.tone === "success"
                ? <CheckCircle2 size={16} aria-hidden="true" />
                : supportPlane.tone === "empty"
                  ? <Radar size={16} aria-hidden="true" />
                  : <AlertTriangle size={16} aria-hidden="true" />}
              {supportPlane.label}
            </span>
          </div>

          <p className="debug-support-plane-boundary">
            <ShieldCheck size={17} aria-hidden="true" />
            이 평면은 CAM4 optical frame의 Tool orientation prior입니다. Robot/world/TCP 좌표 보정이나 실행 권한을 의미하지 않습니다.
          </p>

          <dl className="debug-support-plane-gates" aria-label="Support plane 검증 게이트">
            <div>
              <dt>VALIDATION REQUESTED</dt>
              <dd className={supportPlaneDiagnostics?.validationRequested ? "ready" : "waiting"}>
                {supportPlaneDiagnostics ? supportPlaneDiagnostics.validationRequested ? "YES" : "NO" : "—"}
              </dd>
            </div>
            <div>
              <dt>ARTIFACT LOADED</dt>
              <dd className={supportPlaneDiagnostics?.artifactLoaded ? "ready" : "waiting"}>
                {supportPlaneDiagnostics ? supportPlaneDiagnostics.artifactLoaded ? "YES" : "NO" : "—"}
              </dd>
            </div>
            <div>
              <dt>LIVE EVALUATED</dt>
              <dd className={runtimePlane?.evaluated ? "ready" : "waiting"}>
                {runtimePlane ? runtimePlane.evaluated ? "YES" : "NO" : "—"}
              </dd>
            </div>
            <div>
              <dt>LIVE DRIFT GATE</dt>
              <dd className={supportPlaneValidated ? "ready" : "waiting"}>
                {runtimePlane ? supportPlaneValidated ? "VALID" : "INVALID" : "—"}
              </dd>
            </div>
          </dl>

          <div className="debug-support-plane-metric-groups">
            <section aria-labelledby="debug-support-plane-calibration-title">
              <h3 id="debug-support-plane-calibration-title">CALIBRATION FIT · 저장 artifact</h3>
              <dl data-slot="debug-support-plane-calibration">
                <div><dt>FIT AVAILABLE</dt><dd>{calibrationFit ? calibrationFit.available ? "YES" : "NO" : "—"}</dd></div>
                <div><dt>INLIER RATIO</dt><dd>{optionalProbabilityLabel(calibrationFit?.inlierRatio)}</dd></div>
                <div><dt>RESIDUAL P95</dt><dd>{optionalDistanceLabel(calibrationFit?.residualP95M)}</dd></div>
              </dl>
            </section>
            <section aria-labelledby="debug-support-plane-runtime-title">
              <h3 id="debug-support-plane-runtime-title">RUNTIME VALIDATION · 현재 frame</h3>
              <dl data-slot="debug-support-plane-runtime">
                <div><dt>SAMPLES</dt><dd>{runtimePlane ? runtimePlane.sampleCount.toLocaleString("en-US") : "—"}</dd></div>
                <div><dt>INLIER RATIO</dt><dd>{optionalProbabilityLabel(runtimePlane?.inlierRatio)}</dd></div>
                <div><dt>RESIDUAL MEDIAN</dt><dd>{optionalDistanceLabel(runtimePlane?.residualMedianM)}</dd></div>
                <div><dt>RESIDUAL P95</dt><dd>{optionalDistanceLabel(runtimePlane?.residualP95M)}</dd></div>
                <div className="debug-support-plane-camera-hash">
                  <dt>CAMERAINFO SHA-256</dt>
                  <dd>
                    {runtimePlane?.cameraInfoSha256
                      ? <code title={runtimePlane.cameraInfoSha256}>{runtimePlane.cameraInfoSha256}</code>
                      : "—"}
                  </dd>
                </div>
              </dl>
            </section>
          </div>

          <div className="debug-support-plane-reason-groups">
            <section aria-labelledby="debug-support-plane-static-reasons-title">
              <h3 id="debug-support-plane-static-reasons-title">STATIC REASONS</h3>
              <div data-slot="debug-support-plane-static-reasons">
                {!supportPlaneDiagnostics
                  ? <span>수신 전</span>
                  : supportPlaneDiagnostics.staticReasons.length
                    ? supportPlaneDiagnostics.staticReasons.map((reason) => <code key={reason}>{reason}</code>)
                    : <span>없음</span>}
              </div>
            </section>
            <section aria-labelledby="debug-support-plane-runtime-reasons-title">
              <h3 id="debug-support-plane-runtime-reasons-title">RUNTIME REASONS</h3>
              <div data-slot="debug-support-plane-runtime-reasons">
                {!runtimePlane
                  ? <span>수신 전</span>
                  : runtimePlane.reasons.length
                    ? runtimePlane.reasons.map((reason) => <code key={reason}>{reason}</code>)
                    : <span>없음</span>}
              </div>
            </section>
          </div>
        </article>

        <article className="debug-section-card debug-perception-metric-card">
          <div className="debug-section-heading">
            <div><p>DETECTIONS</p><h2>검출 집계</h2><span>마지막 유효 diagnostics 기준</span></div>
            <Boxes size={19} aria-hidden="true" />
          </div>
          <dl className="debug-perception-kpis">
            <div><dt><ScanLine size={15} aria-hidden="true" />Tool</dt><dd>{usableDiagnostics?.toolDetectionCount ?? "—"}</dd></div>
            <div><dt><Droplets size={15} aria-hidden="true" />Blood</dt><dd>{usableDiagnostics?.bloodDetectionCount ?? "—"}</dd></div>
            <div><dt><Hand size={15} aria-hidden="true" />Hand</dt><dd>{usableDiagnostics?.handCount ?? "—"}</dd></div>
            <div><dt><Layers3 size={15} aria-hidden="true" />Total</dt><dd>{usableDiagnostics?.instanceCount ?? "—"}</dd></div>
          </dl>
          {executedZero ? (
            <p className="debug-perception-zero-note" data-slot="debug-perception-executed-zero">
              <CheckCircle2 size={15} aria-hidden="true" /> 모델은 실행됐습니다. 현재 장면에서 검출된 Tool, Blood, Hand가 없습니다.
            </p>
          ) : null}
          {usableDiagnostics ? (
            <p className="debug-perception-reason" data-slot="debug-perception-overlay-status">
              Server final overlay {usableDiagnostics.overlayStatus.toUpperCase() || "UNKNOWN"}
              {usableDiagnostics.overlayPublished
                ? ` · Drawn T ${usableDiagnostics.overlayDrawnToolCount} / B ${usableDiagnostics.overlayDrawnBloodCount} / H ${usableDiagnostics.overlayDrawnHandCount}`
                : " · 새 오버레이 미발행"}
              {usableDiagnostics.overlayTruncated ? " · TRUNCATED" : ""}
            </p>
          ) : null}
        </article>

        <article className="debug-section-card debug-perception-metric-card">
          <div className="debug-section-heading">
            <div><p>METRIC 3D</p><h2>정렬 깊이·3D 상태</h2><span>정렬과 depth scale 검증을 분리해 표시합니다.</span></div>
            <Layers3 size={19} aria-hidden="true" />
          </div>
          <dl className="debug-perception-readiness">
            <div><dt>Metric 3D</dt><dd className={metricReady ? "ready" : "waiting"}>{metricReady ? "READY" : "NOT READY"}</dd></div>
            <div><dt>Depth aligned</dt><dd className={depthAligned ? "ready" : "waiting"}>{depthAligned ? "YES" : "NO"}</dd></div>
            <div><dt>Depth scale</dt><dd className={usableDiagnostics?.depthScaleValidated ? "ready" : "waiting"}>{usableDiagnostics?.depthScaleValidated ? "VALIDATED" : "NOT VALIDATED"}</dd></div>
            <div><dt>Support plane live</dt><dd className={supportPlaneValidated ? "ready" : "waiting"}>{supportPlaneValidated ? "VALID" : "NOT VALID"}</dd></div>
          </dl>
          <p className="debug-perception-reason">
            {metricReady && supportPlaneValidated
              ? "정렬 깊이와 CameraInfo로 metric 3D 결과를 만들 수 있고 현재 frame의 support-plane drift gate도 유효합니다."
              : metricReady
                ? "Metric 위치는 준비됐지만 live support-plane gate가 무효/미평가라 Tool orientation / 6D는 DEGRADED입니다. 위 audit에서 정적 fit과 runtime 근거를 확인하세요."
                : metricReasons.join(" · ") || "metric 3D 근거를 기다리고 있습니다."}
          </p>
        </article>

        <article className="debug-section-card debug-perception-metric-card">
          <div className="debug-section-heading">
            <div><p>LATENCY</p><h2>추론 지연</h2><span>브라우저 수신 시간이 아니라 worker diagnostics 기준</span></div>
            <Timer size={19} aria-hidden="true" />
          </div>
          <dl className="debug-perception-readiness numeric">
            <div><dt>Inference</dt><dd>{latencyLabel(usableDiagnostics?.inferenceLatencyMs)}</dd></div>
            <div><dt>Source → output</dt><dd>{latencyLabel(usableDiagnostics?.sourceToOutputLatencyMs)}</dd></div>
            <div><dt>Render · encode</dt><dd>{latencyLabel(usableDiagnostics?.renderEncodeLatencyMs)}</dd></div>
            <div><dt>Queue age</dt><dd>{latencyLabel(usableDiagnostics?.queueAgeMs)}</dd></div>
          </dl>
        </article>

        <article className="debug-section-card debug-perception-topic-card">
          <div className="debug-section-heading">
            <div><p>ROS CONTRACT</p><h2>구독 토픽</h2><span>Debug 화면은 읽기 전용이며 planner 명령을 발행하지 않습니다.</span></div>
            <Radar size={19} aria-hidden="true" />
          </div>
          <dl>
            <div><dt>FINAL RASTER</dt><dd><code>{DEBUG_PERCEPTION_FINAL_OVERLAY_TOPIC}</code></dd></div>
            <div><dt>FINAL STATUS</dt><dd><code>{DEBUG_PERCEPTION_FINAL_OVERLAY_STATUS_TOPIC}</code></dd></div>
            <div><dt>TOOL POSES</dt><dd><code>{DEBUG_PERCEPTION_TOOL_POSES_TOPIC}</code></dd></div>
            <div><dt>HAND JOINTS</dt><dd><code>{DEBUG_PERCEPTION_HAND_KEYPOINTS_TOPIC}</code></dd></div>
            <div><dt>BLOOD DATA</dt><dd><code>{DEBUG_PERCEPTION_BLOOD_SEMANTICS_TOPIC}</code></dd></div>
            <div><dt>HEALTH</dt><dd><code>{DEBUG_PERCEPTION_HEALTH_TOPIC}</code></dd></div>
            <div><dt>DIAGNOSTICS</dt><dd><code>{DEBUG_PERCEPTION_DIAGNOSTICS_TOPIC}</code></dd></div>
          </dl>
        </article>
      </div>
    </section>
  );
}
