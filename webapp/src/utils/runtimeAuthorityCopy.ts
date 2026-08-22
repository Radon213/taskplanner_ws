import type { RuntimeAuthorityStatus } from "../hooks/useRosBridge";
import type { Language } from "./display";

export type RuntimeAuthorityFeedback = {
  label: string;
  detail: string;
  tone: "ok" | "pending" | "warn";
};

type AuthorityCopyRow = readonly [
  RuntimeAuthorityFeedback["tone"],
  koLabel: string,
  koDetail: string,
  enLabel: string,
  enDetail: string,
];

const AUTHORITY_COPY: Record<RuntimeAuthorityStatus, AuthorityCopyRow> = {
  ready: [
    "ok",
    "ROS 제어 준비",
    "ROS 브리지와 최신 런타임 상태를 확인했습니다. 제어 요청을 보낼 수 있습니다.",
    "ROS control ready",
    "The bridge and a fresh runtime state are verified. Controls are available.",
  ],
  checking: [
    "pending",
    "런타임 확인 중",
    "자동 시작 서비스에서 현재 활성 런타임을 확인하고 있습니다.",
    "Checking runtime",
    "Checking which runtime is active.",
  ],
  connecting: [
    "pending",
    "ROS 연결 중",
    "선택한 런타임의 ROS 브리지에 연결하고 있습니다.",
    "ROS connecting",
    "Connecting to the selected runtime's ROS bridge.",
  ],
  waiting: [
    "pending",
    "브리지 연결 · 상태 대기",
    "ROS 브리지는 연결됐습니다. 제어를 열기 전 최신 런타임 상태를 기다립니다.",
    "Bridge online · state pending",
    "The bridge is online. Waiting for fresh state before enabling controls.",
  ],
  invalid: [
    "warn",
    "브리지 연결 · 상태 오류",
    "상태 형식이 계약과 달라 제어를 잠갔습니다. 유효한 상태가 오면 자동 복구됩니다.",
    "Bridge online · invalid state",
    "State violates the contract. Controls recover after a valid update.",
  ],
  stale: [
    "warn",
    "브리지 연결 · 상태 만료",
    "상태 갱신이 4초 이상 끊겨 제어를 잠갔습니다. 새 상태가 오면 자동 복구됩니다.",
    "Bridge online · state stale",
    "State updates stopped for over four seconds. Controls recover after a fresh update.",
  ],
  reconnecting: [
    "pending",
    "ROS 재연결 중",
    "ROS 상태 채널을 자동으로 다시 연결하고 있습니다.",
    "ROS reconnecting",
    "Reconnecting the ROS state channel automatically.",
  ],
  offline: [
    "warn",
    "ROS 끊김",
    "ROS 브리지가 오프라인입니다. 연결이 복구될 때까지 제어할 수 없습니다.",
    "ROS offline",
    "The ROS bridge is offline. Controls remain unavailable until it recovers.",
  ],
};

export function runtimeAuthorityCopy(
  status: RuntimeAuthorityStatus,
  language: Language,
): RuntimeAuthorityFeedback {
  const [tone, koLabel, koDetail, enLabel, enDetail] = AUTHORITY_COPY[status];
  return language === "ko"
    ? { label: koLabel, detail: koDetail, tone }
    : { label: enLabel, detail: enDetail, tone };
}
