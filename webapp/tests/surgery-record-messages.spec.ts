import { expect, test } from "playwright/test";

import {
  isFreshSurgeryRecordReceipt,
  isTerminalSurgeryRecordReceipt,
  normalizeSurgeryRecordReceipt,
  sameSurgeryRecordReceipt,
} from "../src/ros/surgeryRecordMessages";
import {
  parseSurgeryRecordSummary,
  projectSurgeryRecordReceipt,
} from "../src/presentation/surgeryRecordPresentation";

const COMPLETED_AT = "2026-08-31T02:03:05Z";
const RECORD_TEXT = "\n[Original]\n  exact indentation remains\n";
const STRUCTURED_SUMMARY = [
  "1. 수술명:",
  "갑상선 절제술",
  "2. 수술부위 및 주요 소견:",
  "경부 중앙부와 갑상선 우엽의 병변을 확인했습니다.",
  "3. 수술과정:",
  "1) 절개 부위를 소독하고 노출했습니다.",
  "   1) 보조 절개선을 확인했습니다.",
  "2) 병변을 박리하고 절제했습니다.",
  "4. 사용기구 및 재료:",
  "메츠바움 가위, 전기소작기, 거즈",
  "5. 출혈 및 지혈:",
  "소량 출혈을 전기소작으로 지혈했습니다.",
].join("\n");

const INLINE_STRUCTURED_SUMMARY = [
  "1.수술명: 갑상선절제술(시연)",
  "2.수술부위 및 주요 소견: ",
  "3.수술과정:",
  "  1) 수술 필드가 어둡고 가려져 해부학적 구조 확인 및 조직 조작이 불가능함.",
  "4.사용기구 및 재료: Grasper, Suction device, Bipolar instrument, Bovie pencil, Bipolar forceps",
  "5.출혈 및 지혈:",
].join("\n");

function message(overrides: Record<string, unknown> = {}) {
  return {
    data: JSON.stringify({
      schema: "taskplanner.surgery_record.receipt.v1",
      request_id: "record-42",
      procedure_run_id: "run-42",
      surgery_code: "THY-42",
      record_text: RECORD_TEXT,
      submit_state: "SUCCEEDED",
      success: true,
      http_status: 201,
      receipt_id: "receipt-42",
      received_at: "2026-08-31T02:03:04Z",
      completed_at: COMPLETED_AT,
      server_result: "success",
      response: JSON.stringify({
        result: "success",
        data: {
          noteTitle: "수술기록지",
          date: "2026-08-31",
          roomName: "Preclinical Center",
          surgeryCode: "THY-42",
          id: "server-record-42",
          receivedAt: "2026-08-31T02:03:04.000Z",
          summary: STRUCTURED_SUMMARY,
        },
      }, null, 2),
      response_truncated: false,
      error: "",
      ...overrides,
    }),
  };
}

test("normalizes the exact durable receipt and retains the original TXT", () => {
  const receipt = normalizeSurgeryRecordReceipt(message());

  expect(receipt).toEqual(expect.objectContaining({
    requestId: "record-42",
    surgeryCode: "THY-42",
    submitState: "SUCCEEDED",
    receiptId: "receipt-42",
    recordText: RECORD_TEXT,
    responseTruncated: false,
  }));
  expect(isTerminalSurgeryRecordReceipt(receipt)).toBe(true);
  expect(isFreshSurgeryRecordReceipt(receipt, Date.parse("2026-08-31T02:03:20Z"))).toBe(true);
});

test("accepts the full 65,535-character source text while keeping the receipt bounded", () => {
  const receipt = normalizeSurgeryRecordReceipt(message({ record_text: "x".repeat(65_535) }));

  expect(receipt?.recordText).toHaveLength(65_535);
});

test("rejects malformed, expanded, and oversized public receipt payloads", () => {
  expect(normalizeSurgeryRecordReceipt(message({ response_truncated: "false" }))).toBeNull();
  expect(normalizeSurgeryRecordReceipt(message({ record_text: "x".repeat(128 * 1024 + 1) }))).toBeNull();
  expect(normalizeSurgeryRecordReceipt(message({ unreviewed_field: "must reject" }))).toBeNull();
  expect(normalizeSurgeryRecordReceipt({ data: "x".repeat(256 * 1024 + 1) })).toBeNull();
});

test("deduplicates durable receipt re-delivery without hiding a new result", () => {
  const first = normalizeSurgeryRecordReceipt(message());
  const repeated = normalizeSurgeryRecordReceipt(message());
  const next = normalizeSurgeryRecordReceipt(message({ request_id: "record-43" }));

  expect(sameSurgeryRecordReceipt(first, repeated)).toBe(true);
  expect(sameSurgeryRecordReceipt(first, next)).toBe(false);
});

test("projects a standard five-section server summary into the shared record view", () => {
  const receipt = normalizeSurgeryRecordReceipt(message());
  expect(receipt).not.toBeNull();
  if (!receipt) return;

  const presentation = projectSurgeryRecordReceipt(receipt);
  expect(presentation.outcome).toBe("success");
  expect(presentation.document.kind).toBe("structured");
  expect(presentation.document.sections).toHaveLength(5);
  expect(presentation.document.procedureSteps).toEqual([
    { number: 1, text: "절개 부위를 소독하고 노출했습니다.\n   1) 보조 절개선을 확인했습니다." },
    { number: 2, text: "병변을 박리하고 절제했습니다." },
  ]);
  expect(presentation.document.instruments).toEqual(["메츠바움 가위", "전기소작기", "거즈"]);
  expect(presentation.metadata.id).toBe("server-record-42");
});

test("projects complete inline section values into the same structured record view", () => {
  const document = parseSurgeryRecordSummary(INLINE_STRUCTURED_SUMMARY);

  expect(document.kind).toBe("structured");
  expect(document.sections.map((section) => section.text)).toEqual([
    "갑상선절제술(시연)",
    "",
    "  1) 수술 필드가 어둡고 가려져 해부학적 구조 확인 및 조직 조작이 불가능함.",
    "Grasper, Suction device, Bipolar instrument, Bovie pencil, Bipolar forceps",
    "",
  ]);
  expect(document.procedureSteps).toEqual([
    {
      number: 1,
      text: "수술 필드가 어둡고 가려져 해부학적 구조 확인 및 조직 조작이 불가능함.",
    },
  ]);
  expect(document.instruments).toEqual([
    "Grasper",
    "Suction device",
    "Bipolar instrument",
    "Bovie pencil",
    "Bipolar forceps",
  ]);
});

test("leaves non-standard and raw responses intact instead of inventing sections", () => {
  expect(parseSurgeryRecordSummary("자유 형식의 수술기록지")).toEqual(expect.objectContaining({
    kind: "plain",
    summary: "자유 형식의 수술기록지",
  }));

  const receipt = normalizeSurgeryRecordReceipt(message({ response: "not-json" }));
  expect(receipt).not.toBeNull();
  if (!receipt) return;
  const presentation = projectSurgeryRecordReceipt(receipt);
  expect(presentation.document.kind).toBe("none");
  expect(presentation.source.text).toBe("not-json");
  expect(presentation.warnings).toContain("response_not_json");
});
