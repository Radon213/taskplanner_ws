import { useMemo } from "react";

import type {
  DebugCapabilityStatus,
  IntegrationDebugStatus,
} from "./useIntegrationDebugBridge";

export type DebugCapabilityName =
  | "observer"
  | "control"
  | "asr"
  | "record"
  | "network";

export interface DebugCapabilityView extends DebugCapabilityStatus {
  /** Browser-facing constraint, distinct from the runtime's status detail. */
  constraint: string;
}

const CAPABILITY_ORDER: readonly DebugCapabilityName[] = [
  "observer",
  "control",
  "asr",
  "record",
  "network",
];

const CONSTRAINTS: Record<DebugCapabilityName, string> = {
  observer: "상태·토픽 관찰은 언제든 가능하며 ROS 쓰기는 만들지 않습니다.",
  control: "ROS 개입은 시나리오가 일시정지/정지 상태이고 수동 제어를 arm한 경우에만 가능합니다.",
  asr: "USB/PipeWire는 이 owner에서만 열립니다. ASR만 범위 재시작할 수 있습니다.",
  record: "수술기록 입력·API 키는 이 owner에만 필요합니다.",
  network: "DDS 설정 변경은 이 owner의 재시작을 요구할 수 있습니다.",
};

function absentCapability(name: DebugCapabilityName): DebugCapabilityView {
  return {
    name,
    enabled: false,
    state: "not_started",
    description: "현재 Debug backend가 capability 상태를 아직 제공하지 않습니다.",
    restart_scope: `debug-${name}`,
    constraint: CONSTRAINTS[name],
  };
}

/**
 * One owner-facing projection for Debug UI panels.  It intentionally keeps
 * connection freshness and backend lifecycle separate from write admission.
 */
export function useDebugCapabilities(status: IntegrationDebugStatus | null) {
  return useMemo(() => {
    const supplied = new Map(
      (status?.capabilities ?? []).map((capability) => [capability.name, capability]),
    );
    const capabilities = CAPABILITY_ORDER.map((name) => {
      const capability = supplied.get(name);
      return capability
        ? { ...capability, constraint: CONSTRAINTS[name] }
        : absentCapability(name);
    });
    const byName = Object.fromEntries(
      capabilities.map((capability) => [capability.name, capability]),
    ) as Record<DebugCapabilityName, DebugCapabilityView>;
    return {
      capabilities,
      byName,
      observerReady: byName.observer.enabled,
      controlEnabled: byName.control.enabled,
      interventionConstraint: byName.control.enabled
        ? CONSTRAINTS.control
        : "이 Debug 프로세스는 관찰 전용입니다. 제어 owner를 별도로 시작하세요.",
    };
  }, [status?.capabilities]);
}
