import type { SurgeryRecordReceipt } from "../ros/surgeryRecordMessages";

type SectionKey =
  | "operationName"
  | "siteAndFindings"
  | "procedure"
  | "instruments"
  | "bleedingAndHemostasis";

export type SurgeryRecordSection = {
  number: number;
  key: SectionKey;
  label: string;
  text: string;
};

export type SurgeryRecordDocument = {
  kind: "none" | "plain" | "structured";
  summary: string;
  sections: SurgeryRecordSection[];
  procedureSteps: Array<{ number: number; text: string }>;
  instruments: string[];
};

type ServerMetadata = {
  noteTitle: string;
  date: string;
  roomName: string;
  surgeryCode: string;
  id: string;
  receivedAt: string;
};

export type SurgeryRecordReceiptPresentation = {
  outcome: "success" | "warning" | "error";
  document: SurgeryRecordDocument;
  source: { kind: "response" | "error" | "empty"; text: string };
  metadata: ServerMetadata & {
    receiptId: string;
    httpStatus: number;
  };
  warnings: string[];
};

const SECTION_DEFINITIONS: ReadonlyArray<Omit<SurgeryRecordSection, "text">> = [
  { number: 1, key: "operationName", label: "수술명" },
  { number: 2, key: "siteAndFindings", label: "수술부위 및 주요 소견" },
  { number: 3, key: "procedure", label: "수술과정" },
  { number: 4, key: "instruments", label: "사용기구 및 재료" },
  { number: 5, key: "bleedingAndHemostasis", label: "출혈 및 지혈" },
];

const EMPTY_METADATA: ServerMetadata = {
  noteTitle: "",
  date: "",
  roomName: "",
  surgeryCode: "",
  id: "",
  receivedAt: "",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function meaningfulText(value: unknown): string {
  const valueText = text(value);
  return valueText.trim() ? valueText : "";
}

function trimOuterBlankLines(value: string): string {
  return value
    .replace(/^(?:[\t ]*\r?\n)+/, "")
    .replace(/(?:\r?\n[\t ]*)+$/, "");
}

function escapeRegularExpression(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

type SectionHeader = {
  sectionIndex: number;
  inlineText: string;
};

function sectionHeader(line: string): SectionHeader | null {
  for (
    let sectionIndex = 0;
    sectionIndex < SECTION_DEFINITIONS.length;
    sectionIndex += 1
  ) {
    const definition = SECTION_DEFINITIONS[sectionIndex];
    const pattern = new RegExp(
      `^\\s*${definition.number}\\.\\s*${escapeRegularExpression(definition.label)}\\s*:\\s*(.*)$`,
      "u",
    );
    const match = pattern.exec(line);
    if (match) return { sectionIndex, inlineText: match[1] ?? "" };
  }
  return null;
}

function parseProcedureSteps(value: string): Array<{ number: number; text: string }> {
  const source = trimOuterBlankLines(value);
  if (!source) return [];

  const lines = source.split(/\r?\n/u);
  const firstContentIndex = lines.findIndex((line) => line.trim().length > 0);
  if (firstContentIndex < 0) return [];

  const firstMatch = /^(\s*)1\)\s*(\S.*)$/u.exec(lines[firstContentIndex]);
  if (!firstMatch) return [];

  const baseIndent = firstMatch[1];
  const steps = [{ number: 1, text: firstMatch[2] }];
  for (let index = firstContentIndex + 1; index < lines.length; index += 1) {
    const line = lines[index];
    const itemMatch = /^(\s*)(\d+)\)\s*(\S.*)$/u.exec(line);
    if (itemMatch && itemMatch[1] === baseIndent) {
      steps.push({ number: Number(itemMatch[2]), text: itemMatch[3] });
      continue;
    }
    const previous = steps[steps.length - 1];
    if (previous) previous.text += `\n${line}`;
  }
  return steps.map((step) => ({ ...step, text: trimOuterBlankLines(step.text) }));
}

function parseInstrumentList(value: string): string[] {
  return trimOuterBlankLines(value)
    .split(/[,，]/u)
    .map((instrument) => instrument.trim())
    .filter(Boolean);
}

function emptyDocument(kind: SurgeryRecordDocument["kind"] = "none", summary = ""): SurgeryRecordDocument {
  return { kind, summary, sections: [], procedureSteps: [], instruments: [] };
}

/**
 * Split the known five-section form only when it is complete and ordered.
 * A server may place a section value after its header colon on the same line;
 * preserve that value in the same structured card instead of falling back to
 * raw text. Any other successful summary remains intact plain text.
 */
export function parseSurgeryRecordSummary(summary: unknown): SurgeryRecordDocument {
  const source = meaningfulText(summary);
  if (!source) return emptyDocument();

  const lines = source.split(/\r?\n/u);
  const headerIndexes: Array<SectionHeader & { lineIndex: number }> = [];
  lines.forEach((line, lineIndex) => {
    const header = sectionHeader(line);
    if (header) headerIndexes.push({ ...header, lineIndex });
  });

  const firstContentIndex = lines.findIndex((line) => line.trim().length > 0);
  const hasExpectedHeaders = headerIndexes.length === SECTION_DEFINITIONS.length
    && headerIndexes.every(({ sectionIndex }, expectedIndex) => sectionIndex === expectedIndex)
    && headerIndexes[0]?.lineIndex === firstContentIndex;
  if (!hasExpectedHeaders) return emptyDocument("plain", source);

  const sections = SECTION_DEFINITIONS.map((definition, index) => {
    const header = headerIndexes[index];
    const start = header.lineIndex + 1;
    const end = index + 1 < headerIndexes.length
      ? headerIndexes[index + 1].lineIndex
      : lines.length;
    return {
      ...definition,
      text: trimOuterBlankLines(
        [header.inlineText, ...lines.slice(start, end)].join("\n"),
      ),
    };
  });
  const procedure = sections.find((section) => section.key === "procedure");
  const instruments = sections.find((section) => section.key === "instruments");
  return {
    kind: "structured",
    summary: source,
    sections,
    procedureSteps: parseProcedureSteps(procedure?.text ?? ""),
    instruments: parseInstrumentList(instruments?.text ?? ""),
  };
}

function responseMetadata(data: Record<string, unknown> | undefined): ServerMetadata {
  return {
    noteTitle: text(data?.noteTitle),
    date: text(data?.date),
    roomName: text(data?.roomName),
    surgeryCode: text(data?.surgeryCode),
    id: text(data?.id),
    receivedAt: text(data?.receivedAt),
  };
}

function parseServerResponse(response: string): {
  kind: "none" | "plain" | "structured" | "raw" | "error";
  reason: string;
  result: string;
  metadata: ServerMetadata;
  document: SurgeryRecordDocument;
} {
  if (!response.trim()) {
    return { kind: "raw", reason: "response_empty", result: "", metadata: EMPTY_METADATA, document: emptyDocument() };
  }

  let envelope: unknown;
  try {
    envelope = JSON.parse(response);
  } catch {
    return { kind: "raw", reason: "response_not_json", result: "", metadata: EMPTY_METADATA, document: emptyDocument() };
  }
  if (!isRecord(envelope)) {
    return { kind: "raw", reason: "response_not_object", result: "", metadata: EMPTY_METADATA, document: emptyDocument() };
  }

  const result = text(envelope.result);
  if (result !== "success") {
    return { kind: "error", reason: result ? "server_result_not_success" : "server_result_missing", result, metadata: EMPTY_METADATA, document: emptyDocument() };
  }
  if (!isRecord(envelope.data)) {
    return { kind: "raw", reason: "success_data_missing", result, metadata: EMPTY_METADATA, document: emptyDocument() };
  }

  const metadata = responseMetadata(envelope.data);
  const document = parseSurgeryRecordSummary(envelope.data.summary);
  if (document.kind === "none") {
    return { kind: "raw", reason: "success_summary_missing", result, metadata, document };
  }
  return {
    kind: document.kind,
    reason: document.kind === "plain" ? "summary_headers_unrecognized" : "",
    result,
    metadata,
    document,
  };
}

function normalizedTimestamp(value: string): string {
  const source = meaningfulText(value);
  if (!source) return "";
  const timestamp = Date.parse(source);
  return Number.isFinite(timestamp) ? String(timestamp) : source;
}

/**
 * Data-only UI model: it parses no more than the exact server response and
 * never persists, sends, or logs a record body.
 */
export function projectSurgeryRecordReceipt(
  receipt: SurgeryRecordReceipt,
): SurgeryRecordReceiptPresentation {
  const server = parseServerResponse(receipt.response);
  const source = receipt.response
    ? { kind: "response" as const, text: receipt.response }
    : receipt.error
      ? { kind: "error" as const, text: receipt.error }
      : { kind: "empty" as const, text: "" };
  const warnings: string[] = [];
  if (receipt.responseTruncated) warnings.push("response_truncated");
  if (server.kind === "plain") warnings.push("summary_headers_unrecognized");
  if (server.kind === "raw") warnings.push(server.reason);
  if (server.kind === "error") warnings.push(server.reason || "server_error");
  if (!receipt.success || receipt.submitState !== "SUCCEEDED") warnings.push("receipt_not_succeeded");
  if (receipt.httpStatus < 200 || receipt.httpStatus >= 300) warnings.push("http_status_not_success");
  if (
    server.metadata.surgeryCode
    && receipt.surgeryCode
    && server.metadata.surgeryCode.trim() !== receipt.surgeryCode.trim()
  ) {
    warnings.push("surgery_code_mismatch");
  }
  if (
    server.metadata.receivedAt
    && receipt.receivedAt
    && normalizedTimestamp(server.metadata.receivedAt) !== normalizedTimestamp(receipt.receivedAt)
  ) {
    warnings.push("received_at_mismatch");
  }

  const outcome = source.kind === "error" || server.kind === "error"
    ? "error"
    : warnings.length > 0 ? "warning" : "success";
  return {
    outcome,
    document: server.document,
    source,
    metadata: {
      ...server.metadata,
      surgeryCode: server.metadata.surgeryCode || receipt.surgeryCode,
      receivedAt: server.metadata.receivedAt || receipt.receivedAt,
      receiptId: receipt.receiptId,
      httpStatus: receipt.httpStatus,
    },
    warnings: [...new Set(warnings)],
  };
}
