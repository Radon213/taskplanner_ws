import { useLayoutEffect, useRef, useState } from "react";

import {
  DEBUG_PERCEPTION_BLOOD_SEMANTICS_TOPIC,
  DEBUG_PERCEPTION_DIAGNOSTICS_TOPIC,
  DEBUG_PERCEPTION_HAND_KEYPOINTS_TOPIC,
  DEBUG_PERCEPTION_HEALTH_TOPIC,
  DEBUG_PERCEPTION_MAX_AGE_MS,
  DEBUG_PERCEPTION_TOOL_POSES_TOPIC,
  debugPerceptionStampNsKey,
  parseBloodSemantics,
  parseHandKeypoints,
  parsePerceptionDiagnostics,
  parsePerceptionHealth,
  parseToolPoseArray,
  type DebugBloodEvidence,
  type DebugBloodSemantics,
  type DebugHandEvidence,
  type DebugHandKeypoints,
  type DebugPerceptionDiagnostics,
  type DebugPerceptionEvidenceState,
  type DebugPerceptionHealth,
  type DebugToolPoseArray,
  type DebugToolPoseEvidence,
} from "../utils/debugPerceptionContract";
import type { DebugReadOnlyTopicSubscriber } from "./useIntegrationDebugBridge";

const MAX_CORRELATION_RECORDS = 48;
const CORRELATION_WINDOW_MS = 5_000;
const INITIAL_EVIDENCE_DETAIL = "PNU 인식 상태와 동일 stamp의 scalar 증거를 기다리고 있습니다.";

function sameStringArray(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function healthDiagnosticsDisagree(
  health: DebugPerceptionHealth,
  diagnostics: DebugPerceptionDiagnostics,
): boolean {
  return health.sourceStampKey !== diagnostics.sourceStampKey
    || !sameStringArray(health.requestedAlgorithms, diagnostics.requestedAlgorithms)
    || !sameStringArray(health.executedAlgorithms, diagnostics.executedAlgorithms)
    || health.detectionCount !== diagnostics.instanceCount
    || health.emptyDetectionResult !== diagnostics.emptyDetectionResult
    || health.metric3dReady !== diagnostics.metric3dReady
    || health.depthAligned !== diagnostics.depthAligned
    || health.supportPlaneValidated !== diagnostics.supportPlaneValidated
    || health.transportMode !== diagnostics.transportMode
    || health.authMode !== diagnostics.authMode
    || (diagnostics.supportPlaneDiagnostics !== null
      && health.supportPlaneValidated
        !== diagnostics.supportPlaneDiagnostics.runtimeValidation.valid);
}

function trimMap<T extends { receivedAt: number }>(map: Map<string, T>, now: number) {
  for (const [key, item] of map) {
    if (now - item.receivedAt > CORRELATION_WINDOW_MS) map.delete(key);
  }
  while (map.size > MAX_CORRELATION_RECORDS) {
    const oldest = map.keys().next().value;
    if (typeof oldest !== "string") break;
    map.delete(oldest);
  }
}

/**
 * Scalar proof for the direct-perception panel. Raster ownership deliberately
 * lives in DirectPerceptionOverlayPanel: this hook never subscribes to raw,
 * detection-overlay, or pose-overlay images.
 */
export function useDebugPerceptionBridge(subscribeTopic: DebugReadOnlyTopicSubscriber) {
  const healthRef = useRef<DebugPerceptionHealth | null>(null);
  const diagnosticsRef = useRef<DebugPerceptionDiagnostics | null>(null);
  const diagnosticsByStampRef = useRef(new Map<string, DebugPerceptionDiagnostics>());
  const pendingToolPosesRef = useRef<DebugToolPoseArray | null>(null);
  const pendingHandsByStampRef = useRef(new Map<string, DebugHandKeypoints>());
  const pendingBloodByStampRef = useRef(new Map<string, DebugBloodSemantics>());
  const toolPoseEvidenceRef = useRef<DebugToolPoseEvidence | null>(null);
  const handEvidenceRef = useRef<DebugHandEvidence | null>(null);
  const bloodEvidenceRef = useRef<DebugBloodEvidence | null>(null);
  const toolPoseContractErrorRef = useRef(false);
  const handContractErrorRef = useRef(false);
  const bloodContractErrorRef = useRef(false);
  const perceptionContractErrorRef = useRef(false);

  const [health, setHealth] = useState<DebugPerceptionHealth | null>(null);
  const [diagnostics, setDiagnostics] = useState<DebugPerceptionDiagnostics | null>(null);
  const [toolPoseEvidence, setToolPoseEvidence] = useState<DebugToolPoseEvidence | null>(null);
  const [handEvidence, setHandEvidence] = useState<DebugHandEvidence | null>(null);
  const [bloodEvidence, setBloodEvidence] = useState<DebugBloodEvidence | null>(null);
  const [toolPoseDetail, setToolPoseDetail] = useState(
    "동일 stamp의 ToolPoseArray와 health/diagnostics를 기다리고 있습니다.",
  );
  const [handDetail, setHandDetail] = useState("동일 stamp의 HandKeypoints를 기다리고 있습니다.");
  const [bloodDetail, setBloodDetail] = useState("동일 stamp의 Blood semantics를 기다리고 있습니다.");
  const [toolPoseContractError, setToolPoseContractError] = useState(false);
  const [handContractError, setHandContractError] = useState(false);
  const [bloodContractError, setBloodContractError] = useState(false);
  const [evidenceState, setEvidenceState] = useState<DebugPerceptionEvidenceState>("waiting_for_health");
  const [evidenceDetail, setEvidenceDetail] = useState(INITIAL_EVIDENCE_DETAIL);

  useLayoutEffect(() => {
    let disposed = false;

    function clearToolPoseEvidence({ clearPending = false } = {}) {
      if (clearPending) pendingToolPosesRef.current = null;
      toolPoseEvidenceRef.current = null;
      setToolPoseEvidence(null);
    }

    function clearHandEvidence({ clearPending = false } = {}) {
      if (clearPending) pendingHandsByStampRef.current.clear();
      handEvidenceRef.current = null;
      setHandEvidence(null);
    }

    function clearBloodEvidence({ clearPending = false } = {}) {
      if (clearPending) pendingBloodByStampRef.current.clear();
      bloodEvidenceRef.current = null;
      setBloodEvidence(null);
    }

    function clearScalarEvidence() {
      clearToolPoseEvidence();
      clearHandEvidence();
      clearBloodEvidence();
    }

    function resetEvidence() {
      healthRef.current = null;
      diagnosticsRef.current = null;
      diagnosticsByStampRef.current.clear();
      pendingToolPosesRef.current = null;
      pendingHandsByStampRef.current.clear();
      pendingBloodByStampRef.current.clear();
      toolPoseEvidenceRef.current = null;
      handEvidenceRef.current = null;
      bloodEvidenceRef.current = null;
      toolPoseContractErrorRef.current = false;
      handContractErrorRef.current = false;
      bloodContractErrorRef.current = false;
      perceptionContractErrorRef.current = false;
      setHealth(null);
      setDiagnostics(null);
      setToolPoseEvidence(null);
      setHandEvidence(null);
      setBloodEvidence(null);
      setToolPoseContractError(false);
      setHandContractError(false);
      setBloodContractError(false);
      setToolPoseDetail("동일 stamp의 ToolPoseArray와 health/diagnostics를 기다리고 있습니다.");
      setHandDetail("동일 stamp의 HandKeypoints를 기다리고 있습니다.");
      setBloodDetail("동일 stamp의 Blood semantics를 기다리고 있습니다.");
      setEvidenceState("waiting_for_health");
      setEvidenceDetail(INITIAL_EVIDENCE_DETAIL);
    }

    function pruneCaches(now: number) {
      trimMap(diagnosticsByStampRef.current, now);
      trimMap(pendingHandsByStampRef.current, now);
      trimMap(pendingBloodByStampRef.current, now);
      if (
        pendingToolPosesRef.current
        && now - pendingToolPosesRef.current.receivedAt > CORRELATION_WINDOW_MS
      ) pendingToolPosesRef.current = null;
    }

    function failHealthDiagnosticsContract() {
      perceptionContractErrorRef.current = true;
      clearScalarEvidence();
      setEvidenceState("contract_error");
      setEvidenceDetail(
        "동일 stamp의 PNU health와 diagnostics 실행·검출·3D 계약이 일치하지 않습니다. support plane 또는 전송 보안 계약을 확인하세요.",
      );
      setToolPoseDetail("PNU health/diagnostics 계약 불일치로 Tool 자세 증거를 지웠습니다.");
    }

    function failToolPoseContract(detail: string) {
      toolPoseContractErrorRef.current = true;
      setToolPoseContractError(true);
      clearToolPoseEvidence();
      setEvidenceState("contract_error");
      setEvidenceDetail(detail);
      setToolPoseDetail(detail);
    }

    function failHandContract(detail: string) {
      handContractErrorRef.current = true;
      setHandContractError(true);
      clearHandEvidence();
      setHandDetail(detail);
    }

    function failBloodContract(detail: string) {
      bloodContractErrorRef.current = true;
      setBloodContractError(true);
      clearBloodEvidence();
      setBloodDetail(detail);
    }

    function currentAnchor(now: number): {
      health: DebugPerceptionHealth;
      diagnostics: DebugPerceptionDiagnostics;
    } | null {
      const currentHealth = healthRef.current;
      if (
        !currentHealth
        || currentHealth.stale
        || !currentHealth.enabled
        || !currentHealth.connected
        || !currentHealth.modelReady
        || now - currentHealth.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
      ) return null;
      const currentDiagnostics = diagnosticsByStampRef.current.get(currentHealth.sourceStampKey);
      if (
        !currentDiagnostics
        || currentDiagnostics.errorCode
        || now - currentDiagnostics.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
      ) return null;
      return { health: currentHealth, diagnostics: currentDiagnostics };
    }

    function promoteAnchor(now = Date.now()) {
      if (disposed) return;
      const currentHealth = healthRef.current;
      if (!currentHealth) {
        setEvidenceState("waiting_for_health");
        setEvidenceDetail(INITIAL_EVIDENCE_DETAIL);
        return;
      }
      if (!currentHealth.enabled) {
        clearScalarEvidence();
        setEvidenceState("disabled");
        setEvidenceDetail("PNU 인식 provider가 비활성화되어 있습니다.");
        return;
      }
      if (!currentHealth.connected || currentHealth.status === "error" || currentHealth.status === "stale") {
        clearScalarEvidence();
        setEvidenceState(currentHealth.status === "stale" ? "stale" : currentHealth.status === "waiting_for_frame" ? "waiting_for_frame" : "error");
        setEvidenceDetail(
          currentHealth.lastErrorMessage
            || (currentHealth.status === "waiting_for_frame"
              ? "PNU worker가 CAM4 입력 프레임을 기다리고 있습니다."
              : `PNU 인식 상태: ${currentHealth.status}`),
        );
        return;
      }
      if (!currentHealth.modelReady) {
        clearScalarEvidence();
        setEvidenceState("error");
        setEvidenceDetail("요청된 PNU 모델이 모두 준비되지 않았습니다.");
        return;
      }
      const exactDiagnostics = diagnosticsByStampRef.current.get(currentHealth.sourceStampKey);
      if (!exactDiagnostics) {
        if (!perceptionContractErrorRef.current) {
          setEvidenceState("waiting_for_overlay");
          setEvidenceDetail("동일 source stamp의 PNU diagnostics scalar record를 기다리고 있습니다.");
        }
        return;
      }
      if (exactDiagnostics.errorCode) {
        clearScalarEvidence();
        setEvidenceState("error");
        setEvidenceDetail(exactDiagnostics.errorMessage || `PNU 추론 실패: ${exactDiagnostics.errorCode}`);
        return;
      }
      if (healthDiagnosticsDisagree(currentHealth, exactDiagnostics)) {
        failHealthDiagnosticsContract();
        return;
      }
      if (
        now - currentHealth.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
        || now - exactDiagnostics.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
      ) return;
      perceptionContractErrorRef.current = false;
      diagnosticsRef.current = exactDiagnostics;
      setDiagnostics(exactDiagnostics);
      setEvidenceState("ready");
      setEvidenceDetail(
        exactDiagnostics.emptyDetectionResult
          ? "모델 실행은 완료됐고 검출은 0건입니다. health/diagnostics scalar 계약이 일치합니다."
          : "동일 source stamp의 health/diagnostics scalar 계약이 일치합니다. 영상은 상단 shared final raster 한 장에서 확인합니다.",
      );
      promoteToolPoseEvidence(now);
      promoteSemanticEvidence(now);
    }

    function promoteToolPoseEvidence(now = Date.now()) {
      if (disposed) return;
      const poses = pendingToolPosesRef.current;
      const anchor = currentAnchor(now);
      if (!poses || !anchor) return;
      const { health: currentHealth, diagnostics: exactDiagnostics } = anchor;
      if (
        poses.sourceStampKey !== exactDiagnostics.sourceStampKey
        || now - poses.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
      ) return;
      if (
        !currentHealth.requestedAlgorithms.includes("tool")
        || !currentHealth.executedAlgorithms.includes("tool")
        || !exactDiagnostics.requestedAlgorithms.includes("tool")
        || !exactDiagnostics.executedAlgorithms.includes("tool")
        || exactDiagnostics.toolDetectionCount !== poses.tools.length
        || poses.frameId !== exactDiagnostics.frameId
      ) {
        failToolPoseContract("동일 stamp의 ToolPoseArray와 PNU 실행·Tool 검출 집계 또는 frame_id가 일치하지 않습니다.");
        return;
      }
      const axisCount = poses.tools.filter((tool) => tool.orientationValid).length;
      const positionOnlyCount = poses.tools.filter(
        (tool) => tool.positionValid && !tool.orientationValid,
      ).length;
      if (axisCount > 0 && !currentHealth.supportPlaneValidated) {
        failToolPoseContract("support plane 미검증 상태에서 orientation_valid Tool 자세가 수신되어 자세 증거를 폐기했습니다.");
        return;
      }
      if (exactDiagnostics.poseOverlayPublished !== null) {
        const countsMatch = !exactDiagnostics.poseOverlayPublished
          ? exactDiagnostics.poseOverlayDrawnAxisCount === 0
            && exactDiagnostics.poseOverlayDrawnPositionOnlyCount === 0
          : exactDiagnostics.poseOverlayTruncated
            ? exactDiagnostics.poseOverlayDrawnAxisCount <= axisCount
              && exactDiagnostics.poseOverlayDrawnPositionOnlyCount <= positionOnlyCount
            : exactDiagnostics.poseOverlayDrawnAxisCount === axisCount
              && exactDiagnostics.poseOverlayDrawnPositionOnlyCount === positionOnlyCount;
        if (!countsMatch) {
          failToolPoseContract("ToolPoseArray 유효 자세 수와 server final-overlay diagnostics 집계가 일치하지 않습니다.");
          return;
        }
      }
      const evidence = { poses, diagnostics: exactDiagnostics };
      toolPoseEvidenceRef.current = evidence;
      toolPoseContractErrorRef.current = false;
      setToolPoseEvidence(evidence);
      setToolPoseContractError(false);
      if (poses.tools.length === 0) {
        setToolPoseDetail("ToolPoseArray 실행 완료 · Tool 0건입니다. final raster의 이전 축은 서버에서 제거됩니다.");
      } else if (axisCount > 0) {
        setToolPoseDetail(`검증된 support plane 근거로 ${axisCount}개 Tool의 평면 제약 자세를 검토합니다.`);
      } else if (positionOnlyCount > 0) {
        setToolPoseDetail(`Tool ${positionOnlyCount}건은 metric 위치만 유효합니다. orientation은 표시·사용하지 않습니다.`);
      } else {
        setToolPoseDetail("Tool 검출은 수신됐지만 사용할 수 있는 metric 위치 또는 orientation이 없습니다.");
      }
    }

    function promoteSemanticEvidence(now = Date.now()) {
      if (disposed) return;
      const anchor = currentAnchor(now);
      if (!anchor) return;
      const { diagnostics: exactDiagnostics } = anchor;
      const hand = pendingHandsByStampRef.current.get(exactDiagnostics.sourceStampKey) ?? null;
      if (hand) {
        if (
          now - hand.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
          || hand.frameId !== exactDiagnostics.frameId
          || !exactDiagnostics.requestedAlgorithms.includes("hand")
          || !exactDiagnostics.executedAlgorithms.includes("hand")
          || exactDiagnostics.handCount !== hand.hands.length
          || (hand.depthSource === "2d_only") === exactDiagnostics.metric3dReady
        ) {
          failHandContract("HandKeypoints가 동일 stamp/frame의 실행·count·depth_source 계약과 일치하지 않습니다.");
        } else {
          const evidence = { diagnostics: exactDiagnostics, result: hand };
          handEvidenceRef.current = evidence;
          pendingHandsByStampRef.current.delete(hand.sourceStampKey);
          setHandEvidence(evidence);
          handContractErrorRef.current = false;
          setHandContractError(false);
          setHandDetail(
            hand.hands.length === 0
              ? "Hand 알고리즘 실행 완료 · Hand 0건입니다."
              : `동일 stamp의 Hand ${hand.hands.length}건과 21-joint scalar 증거를 결합했습니다.`,
          );
        }
      } else if (
        !handContractErrorRef.current
        && handEvidenceRef.current?.result.sourceStampKey !== exactDiagnostics.sourceStampKey
      ) {
        setHandDetail(
          pendingHandsByStampRef.current.size > 0
            ? "HandKeypoints를 stamp별로 버퍼링했습니다. 현재 diagnostics와 동일 stamp 결과를 기다립니다."
            : "현재 diagnostics와 동일 stamp의 HandKeypoints를 기다리고 있습니다.",
        );
      }

      const stampNsKey = debugPerceptionStampNsKey(
        exactDiagnostics.sourceStampSec,
        exactDiagnostics.sourceStampNanosec,
      );
      const blood = pendingBloodByStampRef.current.get(stampNsKey) ?? null;
      if (blood) {
        if (
          now - blood.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
          || blood.frameId !== exactDiagnostics.frameId
          || !exactDiagnostics.requestedAlgorithms.includes("blood")
          || !exactDiagnostics.executedAlgorithms.includes("blood")
          || exactDiagnostics.bloodDetectionCount !== blood.detections.length
          || exactDiagnostics.metric3dReady !== blood.metric3dReady
        ) {
          failBloodContract("Blood semantics가 동일 stamp/frame의 실행·count·metric 3D 계약과 일치하지 않습니다.");
        } else {
          const evidence = { diagnostics: exactDiagnostics, result: blood };
          bloodEvidenceRef.current = evidence;
          pendingBloodByStampRef.current.delete(blood.sourceStampNsKey);
          setBloodEvidence(evidence);
          bloodContractErrorRef.current = false;
          setBloodContractError(false);
          setBloodDetail(
            blood.detections.length === 0
              ? "Blood 알고리즘 실행 완료 · Blood 0건입니다."
              : `동일 stamp의 Blood ${blood.detections.length}건과 centroid scalar 증거를 결합했습니다.`,
          );
        }
      } else if (
        !bloodContractErrorRef.current
        && bloodEvidenceRef.current?.result.sourceStampNsKey !== stampNsKey
      ) {
        setBloodDetail(
          pendingBloodByStampRef.current.size > 0
            ? "Blood semantics를 stamp별로 버퍼링했습니다. 현재 diagnostics와 동일 stamp 결과를 기다립니다."
            : "현재 diagnostics와 동일 stamp의 Blood semantics를 기다리고 있습니다.",
        );
      }
    }

    resetEvidence();

    const unsubscribeHealth = subscribeTopic({
      name: DEBUG_PERCEPTION_HEALTH_TOPIC,
      messageType: "std_msgs/msg/String",
      queueLength: 1,
      reliability: "reliable",
    }, (message) => {
      if (disposed) return;
      const receivedAt = Date.now();
      const next = parsePerceptionHealth(message, receivedAt);
      if (!next) {
        perceptionContractErrorRef.current = true;
        healthRef.current = null;
        setHealth(null);
        clearScalarEvidence();
        setEvidenceState("contract_error");
        setEvidenceDetail("인식 health가 PNU provider 또는 taskplanner.rfdetr_health.v1 계약과 일치하지 않습니다.");
        setToolPoseDetail("인식 health 계약이 불일치해 Tool 자세 증거를 지웠습니다.");
        return;
      }
      const changedStamp = healthRef.current?.sourceStampKey !== next.sourceStampKey;
      healthRef.current = next;
      setHealth(next);
      if (changedStamp) clearScalarEvidence();
      pruneCaches(receivedAt);
      promoteAnchor(receivedAt);
    });

    const unsubscribeDiagnostics = subscribeTopic({
      name: DEBUG_PERCEPTION_DIAGNOSTICS_TOPIC,
      messageType: "std_msgs/msg/String",
      queueLength: 1,
      // The PNU publisher uses sensor-style BEST_EFFORT QoS; requesting
      // RELIABLE would leave this read-only diagnostic stream permanently unmatched.
      reliability: "best_effort",
    }, (message) => {
      if (disposed) return;
      const receivedAt = Date.now();
      const next = parsePerceptionDiagnostics(message, receivedAt);
      if (!next) {
        perceptionContractErrorRef.current = true;
        diagnosticsRef.current = null;
        setDiagnostics(null);
        clearScalarEvidence();
        setEvidenceState("contract_error");
        setEvidenceDetail("PNU diagnostics가 provider, schema, 실행 목록 또는 검출 집계 계약과 일치하지 않습니다.");
        setToolPoseDetail("PNU diagnostics 계약이 불일치해 Tool 자세 증거를 지웠습니다.");
        return;
      }
      diagnosticsByStampRef.current.delete(next.sourceStampKey);
      diagnosticsByStampRef.current.set(next.sourceStampKey, next);
      diagnosticsRef.current = next;
      setDiagnostics(next);
      if (next.errorCode && healthRef.current?.sourceStampKey === next.sourceStampKey) {
        clearScalarEvidence();
        setEvidenceState("error");
        setEvidenceDetail(next.errorMessage || `PNU 추론 실패: ${next.errorCode}`);
      }
      pruneCaches(receivedAt);
      promoteAnchor(receivedAt);
    });

    const unsubscribeToolPoses = subscribeTopic({
      name: DEBUG_PERCEPTION_TOOL_POSES_TOPIC,
      messageType: "surgical_perception_msgs/msg/ToolPoseArray",
      // rosbridge Jazzy's CBOR walker treats fixed bool arrays as nested ROS
      // messages and crashes on ToolPose.dof_observed. The typed scalar topic
      // deliberately uses normal JSON; the sole final raster uses CBOR.
      queueLength: 1,
      reliability: "reliable",
    }, (message) => {
      if (disposed) return;
      const receivedAt = Date.now();
      const poses = parseToolPoseArray(message, receivedAt);
      if (!poses) {
        failToolPoseContract("ToolPoseArray가 schema v1.3, bounded pose 또는 validity 계약과 일치하지 않습니다.");
        return;
      }
      if (toolPoseEvidenceRef.current?.poses.sourceStampKey !== poses.sourceStampKey) {
        clearToolPoseEvidence();
      }
      pendingToolPosesRef.current = poses;
      toolPoseContractErrorRef.current = false;
      setToolPoseContractError(false);
      setToolPoseDetail("ToolPoseArray를 수신했습니다. 같은 source stamp의 health/diagnostics와 결합 중입니다.");
      pruneCaches(receivedAt);
      promoteAnchor(receivedAt);
    });

    const unsubscribeHandKeypoints = subscribeTopic({
      name: DEBUG_PERCEPTION_HAND_KEYPOINTS_TOPIC,
      messageType: "hand_keypoint_interfaces/msg/HandKeypoints",
      queueLength: 1,
      reliability: "reliable",
    }, (message) => {
      if (disposed) return;
      const receivedAt = Date.now();
      const result = parseHandKeypoints(message, receivedAt);
      if (!result) {
        failHandContract("HandKeypoints가 bounded 21-joint, depth validity 또는 palm 6D 계약과 일치하지 않습니다.");
        return;
      }
      pendingHandsByStampRef.current.delete(result.sourceStampKey);
      pendingHandsByStampRef.current.set(result.sourceStampKey, result);
      handContractErrorRef.current = false;
      setHandContractError(false);
      setHandDetail("HandKeypoints를 수신했습니다. 동일 stamp의 diagnostics와 결합 중입니다.");
      pruneCaches(receivedAt);
      promoteAnchor(receivedAt);
    });

    const unsubscribeBloodSemantics = subscribeTopic({
      name: DEBUG_PERCEPTION_BLOOD_SEMANTICS_TOPIC,
      messageType: "std_msgs/msg/String",
      queueLength: 1,
      reliability: "reliable",
    }, (message) => {
      if (disposed) return;
      const receivedAt = Date.now();
      const result = parseBloodSemantics(message, receivedAt);
      if (!result) {
        failBloodContract("Blood semantics가 bounded instance/centroid/depth 또는 lossless source_stamp_ns 계약과 일치하지 않습니다.");
        return;
      }
      pendingBloodByStampRef.current.delete(result.sourceStampNsKey);
      pendingBloodByStampRef.current.set(result.sourceStampNsKey, result);
      bloodContractErrorRef.current = false;
      setBloodContractError(false);
      setBloodDetail("Blood semantics를 수신했습니다. 동일 stamp의 diagnostics와 결합 중입니다.");
      pruneCaches(receivedAt);
      promoteAnchor(receivedAt);
    });

    const freshnessTimer = window.setInterval(() => {
      if (disposed) return;
      const now = Date.now();
      const currentHealth = healthRef.current;
      if (currentHealth && !currentHealth.stale && now - currentHealth.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS) {
        const staleHealth = { ...currentHealth, stale: true };
        healthRef.current = staleHealth;
        setHealth(staleHealth);
        clearScalarEvidence();
        setEvidenceState("stale");
        setEvidenceDetail("PNU health가 3초 이상 갱신되지 않아 scalar Tool·Hand·Blood 증거를 지웠습니다.");
        setToolPoseDetail("PNU health가 만료되어 Tool 자세 증거를 지웠습니다.");
      }
      const currentDiagnostics = diagnosticsRef.current;
      if (currentDiagnostics && now - currentDiagnostics.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS) {
        diagnosticsRef.current = null;
        setDiagnostics(null);
        clearScalarEvidence();
        const refreshedHealth = healthRef.current;
        if (refreshedHealth && !refreshedHealth.stale) {
          setEvidenceState("waiting_for_overlay");
          setEvidenceDetail("PNU diagnostics가 만료되어 새 scalar record를 기다리고 있습니다.");
        }
      }
      if (
        toolPoseEvidenceRef.current
        && (
          now - toolPoseEvidenceRef.current.poses.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
          || now - toolPoseEvidenceRef.current.diagnostics.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
        )
      ) {
        clearToolPoseEvidence();
        setToolPoseDetail("Tool 자세 scalar 증거가 3초 이상 갱신되지 않아 위치·quaternion을 지웠습니다.");
      }
      if (handEvidenceRef.current && (
        now - handEvidenceRef.current.result.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
        || now - handEvidenceRef.current.diagnostics.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
      )) {
        clearHandEvidence();
        setHandDetail("HandKeypoints exact-stamp 증거가 만료되어 2D/3D joint와 palm pose를 지웠습니다.");
      }
      if (bloodEvidenceRef.current && (
        now - bloodEvidenceRef.current.result.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
        || now - bloodEvidenceRef.current.diagnostics.receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS
      )) {
        clearBloodEvidence();
        setBloodDetail("Blood semantics exact-stamp 증거가 만료되어 centroid와 depth를 지웠습니다.");
      }
      pruneCaches(now);
    }, 500);

    return () => {
      disposed = true;
      window.clearInterval(freshnessTimer);
      unsubscribeHealth();
      unsubscribeDiagnostics();
      unsubscribeToolPoses();
      unsubscribeHandKeypoints();
      unsubscribeBloodSemantics();
      resetEvidence();
    };
  }, [subscribeTopic]);

  return {
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
  };
}
