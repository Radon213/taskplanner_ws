import type {
  BedRobotArmState,
  BedRobotArmStateArray,
} from "../types";
import { isBoundedRosPayload } from "./rosMessageBounds";

const BED_ROBOT_ARM_STATES = new Set([
  "standby",
  "direct_teach",
  "retracting",
  "changing_tool",
  "moving_to_standby",
  "fault",
  "protective_stop",
  "unknown",
]);

const BED_ROBOT_PROCEDURE_LAYOUTS: Record<string, ReadonlySet<string>> = {
  thyroidectomy: new Set(["army_navy"]),
  nephrectomy: new Set(["left_malleable", "right_malleable"]),
  inguinal_hernia_repair: new Set(["left_army_navy", "right_army_navy"]),
};

export const BED_ROBOT_STATUS_MAX_AGE_MS = 3000;

export type ValidatedBedRobotArmStatus = {
  stampMs: number;
  receivedAtMs: number;
  revision: number;
  procedureType: string;
  arms: BedRobotArmState[];
};

export function canonicalBedRobotProcedure(procedureId: string): string {
  const normalized = procedureId.trim().toLowerCase();
  if (normalized === "thyroidectomy" || normalized === "thyroidectomy_demo") {
    return "thyroidectomy";
  }
  if (normalized === "inguinal_hernia_repair_demo") {
    return "inguinal_hernia_repair";
  }
  return normalized === "nephrectomy" ? normalized : "";
}

function normalizeBedRobotArmState(message: unknown): BedRobotArmState | null {
  if (!message || typeof message !== "object") return null;
  const arm = message as Partial<BedRobotArmState>;
  const armId = String(arm.arm_id || "").trim();
  if (!armId || String(arm.role || "").trim().toLowerCase() !== "retraction") {
    return null;
  }
  return {
    arm_id: armId,
    role: "retraction",
    role_instance_id: String(arm.role_instance_id || "").trim(),
    state: String(arm.state || "unknown").trim().toLowerCase(),
    direct_teach_active: Boolean(arm.direct_teach_active),
    reason_code: String(arm.reason_code || "").trim(),
  };
}

export function normalizeBedRobotArmStates(message: unknown): BedRobotArmState[] {
  if (!Array.isArray(message)) return [];
  return message
    .map((arm) => normalizeBedRobotArmState(arm))
    .filter((arm): arm is BedRobotArmState => arm !== null);
}

function rosTimeToMilliseconds(stamp: BedRobotArmStateArray["stamp"] | undefined): number | null {
  const sec = Number(stamp?.sec);
  const nanosec = Number(stamp?.nanosec);
  if (
    !Number.isSafeInteger(sec) ||
    sec < 0 ||
    !Number.isInteger(nanosec) ||
    nanosec < 0 ||
    nanosec >= 1_000_000_000
  ) {
    return null;
  }
  return sec * 1000 + nanosec / 1_000_000;
}

/**
 * Validate the complete, procedure-specific bed-arm snapshot before the hook
 * can expose it. Partial or contradictory arrays are rejected as one unit.
 */
export function normalizeBedRobotArmStatus(message: unknown): ValidatedBedRobotArmStatus | null {
  if (!isBoundedRosPayload(message) || !message || typeof message !== "object") return null;
  const status = message as Partial<BedRobotArmStateArray>;
  const procedureType = String(status.procedure_type || "").trim().toLowerCase();
  const expectedRoles = BED_ROBOT_PROCEDURE_LAYOUTS[procedureType];
  const stampMs = rosTimeToMilliseconds(status.stamp);
  const revision = Number(status.revision);
  if (
    !expectedRoles ||
    !Array.isArray(status.arms) ||
    stampMs === null ||
    stampMs <= 0 ||
    !Number.isSafeInteger(revision) ||
    revision < 0
  ) {
    return null;
  }

  const arms = normalizeBedRobotArmStates(status.arms);
  if (arms.length !== status.arms.length || arms.length !== expectedRoles.size) {
    return null;
  }
  const armIds = new Set<string>();
  const roles = new Set<string>();
  for (const arm of arms) {
    if (
      !new Set(["arm_1", "arm_2"]).has(arm.arm_id) ||
      armIds.has(arm.arm_id) ||
      !expectedRoles.has(arm.role_instance_id) ||
      roles.has(arm.role_instance_id) ||
      !BED_ROBOT_ARM_STATES.has(arm.state) ||
      arm.direct_teach_active !== (arm.state === "direct_teach")
    ) {
      return null;
    }
    armIds.add(arm.arm_id);
    roles.add(arm.role_instance_id);
  }
  return roles.size === expectedRoles.size
    ? { stampMs, receivedAtMs: Date.now(), revision, procedureType, arms }
    : null;
}

export function sameBedRobotArmState(
  left: BedRobotArmState[],
  right: BedRobotArmState[],
): boolean {
  if (left.length !== right.length) return false;
  const leftById = new Map(left.map((arm) => [arm.arm_id, arm]));
  return right.every((arm) => {
    const previous = leftById.get(arm.arm_id);
    return Boolean(
      previous &&
      previous.role === arm.role &&
      previous.role_instance_id === arm.role_instance_id &&
      previous.state === arm.state &&
      previous.direct_teach_active === arm.direct_teach_active &&
      previous.reason_code === arm.reason_code,
    );
  });
}
