/**
 * Typed, read-only browser boundary for the durable surgery-record receipt.
 *
 * This is intentionally not the private `/surgery/record/post_status` topic:
 * the public receipt carries the exact TXT sent to the record server, so the
 * operator and SurgiMate can render the same three read-only views.
 */
export const SURGERY_RECORD_RECEIPT_TOPIC = "/surgery/record/receipt";
export const SURGERY_RECORD_RECEIPT_SCHEMA =
  "taskplanner.surgery_record.receipt.v1";

export const SURGERY_RECORD_RECEIPT_MAX_AGE_MS = 60_000;
export const SURGERY_RECORD_RECEIPT_MAX_FUTURE_SKEW_MS = 5_000;
export const MAX_SURGERY_RECORD_RECEIPT_JSON_CHARS = 256 * 1024;
export const MAX_SURGERY_RECORD_TEXT_CHARS = 128 * 1024;
export const MAX_SURGERY_RECORD_RESPONSE_CHARS = 16_384;

const MAX_IDENTIFIER_CHARS = 160;
const MAX_SERVER_RESULT_CHARS = 80;
const MAX_ERROR_CHARS = 512;
const TERMINAL_SUBMIT_STATES = new Set([
  "SUCCEEDED",
  "FAILED",
  "REMOTE_STATE_UNKNOWN",
]);
const RECEIPT_FIELDS = [
  "request_id",
  "procedure_run_id",
  "surgery_code",
  "submit_state",
  "success",
  "http_status",
  "receipt_id",
  "received_at",
  "completed_at",
  "server_result",
  "record_text",
  "response",
  "response_truncated",
  "error",
] as const;
const RECEIPT_KEYS = new Set<string>(["schema", ...RECEIPT_FIELDS]);

export type SurgeryRecordReceipt = {
  requestId: string;
  procedureRunId: string;
  surgeryCode: string;
  submitState: string;
  success: boolean;
  httpStatus: number;
  receiptId: string;
  receivedAt: string;
  completedAt: string;
  serverResult: string;
  /** Exact UTF-8 source text submitted to the record server. */
  recordText: string;
  /** Exact bounded body returned by the record server. */
  response: string;
  responseTruncated: boolean;
  error: string;
};

type ReceiptDecodeResult = {
  receipt: SurgeryRecordReceipt | null;
  reason: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function boundedString(
  value: unknown,
  maximum: number,
  { required = false }: { required?: boolean } = {},
): string | null {
  if (typeof value !== "string" || value.length > maximum) return null;
  if (required && !value.trim()) return null;
  return value;
}

function hasExactReceiptShape(value: Record<string, unknown>): boolean {
  const keys = Object.keys(value);
  return keys.length === RECEIPT_KEYS.size && keys.every((key) => RECEIPT_KEYS.has(key));
}

function completedTime(value: string): number | null {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Receipt-specific validation deliberately does not call the generic ROS JSON
 * bounds helper: a valid `record_text` may be 65,535 characters, one byte
 * beyond its generic per-string 64 KiB limit.
 */
export function decodeSurgeryRecordReceipt(message: unknown): ReceiptDecodeResult {
  if (!isRecord(message)) return { receipt: null, reason: "message_not_object" };
  if (typeof message.data !== "string") return { receipt: null, reason: "payload_not_string" };
  if (!message.data || message.data.length > MAX_SURGERY_RECORD_RECEIPT_JSON_CHARS) {
    return { receipt: null, reason: "payload_out_of_bounds" };
  }

  let value: unknown;
  try {
    value = JSON.parse(message.data);
  } catch {
    return { receipt: null, reason: "payload_not_json" };
  }
  if (!isRecord(value)) return { receipt: null, reason: "payload_not_object" };
  if (!hasExactReceiptShape(value)) return { receipt: null, reason: "unexpected_receipt_shape" };
  if (value.schema !== SURGERY_RECORD_RECEIPT_SCHEMA) {
    return { receipt: null, reason: "schema_mismatch" };
  }

  const requestId = boundedString(value.request_id, MAX_IDENTIFIER_CHARS, { required: true });
  const procedureRunId = boundedString(value.procedure_run_id, MAX_IDENTIFIER_CHARS, { required: true });
  const surgeryCode = boundedString(value.surgery_code, MAX_IDENTIFIER_CHARS, { required: true });
  const submitState = boundedString(value.submit_state, 64, { required: true });
  const receiptId = boundedString(value.receipt_id, MAX_IDENTIFIER_CHARS);
  const receivedAt = boundedString(value.received_at, MAX_IDENTIFIER_CHARS);
  const completedAt = boundedString(value.completed_at, MAX_IDENTIFIER_CHARS, { required: true });
  const serverResult = boundedString(value.server_result, MAX_SERVER_RESULT_CHARS);
  const recordText = boundedString(value.record_text, MAX_SURGERY_RECORD_TEXT_CHARS, { required: true });
  const response = boundedString(value.response, MAX_SURGERY_RECORD_RESPONSE_CHARS);
  const error = boundedString(value.error, MAX_ERROR_CHARS);
  const httpStatus = typeof value.http_status === "number" ? value.http_status : null;

  if (
    requestId === null || procedureRunId === null || surgeryCode === null
    || submitState === null || receiptId === null || receivedAt === null
    || completedAt === null || completedTime(completedAt) === null
    || serverResult === null || recordText === null || response === null || error === null
    || httpStatus === null || !Number.isInteger(httpStatus) || httpStatus < 0 || httpStatus > 599
    || typeof value.success !== "boolean"
    || typeof value.response_truncated !== "boolean"
  ) {
    return { receipt: null, reason: "invalid_receipt_fields" };
  }

  return {
    receipt: {
      requestId,
      procedureRunId,
      surgeryCode,
      submitState,
      success: value.success,
      httpStatus,
      receiptId,
      receivedAt,
      completedAt,
      serverResult,
      recordText,
      response,
      responseTruncated: value.response_truncated,
      error,
    },
    reason: "",
  };
}

export function normalizeSurgeryRecordReceipt(message: unknown): SurgeryRecordReceipt | null {
  return decodeSurgeryRecordReceipt(message).receipt;
}

export function isTerminalSurgeryRecordReceipt(
  receipt: SurgeryRecordReceipt | null,
): receipt is SurgeryRecordReceipt {
  return Boolean(
    receipt
      && receipt.requestId
      && receipt.completedAt
      && TERMINAL_SUBMIT_STATES.has(receipt.submitState),
  );
}

export function isFreshSurgeryRecordReceipt(
  receipt: SurgeryRecordReceipt | null,
  now = Date.now(),
): boolean {
  if (!isTerminalSurgeryRecordReceipt(receipt)) return false;
  const completedAtMs = completedTime(receipt.completedAt);
  return completedAtMs !== null
    && completedAtMs <= now + SURGERY_RECORD_RECEIPT_MAX_FUTURE_SKEW_MS
    && now - completedAtMs <= SURGERY_RECORD_RECEIPT_MAX_AGE_MS;
}

export function surgeryRecordReceiptOutcomeKey(receipt: SurgeryRecordReceipt): string {
  return [
    receipt.requestId,
    receipt.submitState,
    receipt.completedAt,
    receipt.httpStatus,
  ].join("|");
}

/** Ignore durable receipt re-delivery without dropping a genuinely new outcome. */
export function sameSurgeryRecordReceipt(
  left: SurgeryRecordReceipt | null,
  right: SurgeryRecordReceipt | null,
): boolean {
  if (left === right) return true;
  if (!left || !right) return false;
  return left.requestId === right.requestId
    && left.procedureRunId === right.procedureRunId
    && left.surgeryCode === right.surgeryCode
    && left.submitState === right.submitState
    && left.success === right.success
    && left.httpStatus === right.httpStatus
    && left.receiptId === right.receiptId
    && left.receivedAt === right.receivedAt
    && left.completedAt === right.completedAt
    && left.serverResult === right.serverResult
    && left.recordText === right.recordText
    && left.response === right.response
    && left.responseTruncated === right.responseTruncated
    && left.error === right.error;
}
