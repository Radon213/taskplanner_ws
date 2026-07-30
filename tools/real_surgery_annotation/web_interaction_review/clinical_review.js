"use strict";

const state = {
  clinical: null,
  context: null,
  finalContext: null,
  cases: [],
  items: [],
  selectedId: null,
  draft: null,
  baseline: null,
  reviewNotes: "",
  viewMode: "draft",
  videoView: "flir",
  mediaMode: "loading",
  mediaConfigured: false,
  videoReady: false,
  companionReady: false,
  currentTimeSec: 0,
  saving: false,
  loading: false,
  toastTimer: null,
  loadingTimer: null,
  loadingShownAt: 0,
  contextError: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const video = $("#clinical-video");
const companionVideo = $("#companion-video");

const KIND_LABELS = {
  activity_segment: "임상 행위",
  clinical_observation: "임상 관찰",
  state_change: "상태 변화",
  unobservable_span: "판독 불가",
};

const KIND_SYMBOLS = {
  activity_segment: "행",
  clinical_observation: "관",
  state_change: "변",
  unobservable_span: "불",
};

const STATUS_LABELS = {
  unreviewed: "미검토",
  proposed: "미검토",
  confirmed: "확정",
  ambiguous: "애매",
  rejected: "기각",
};

const STATUS_SYMBOLS = {
  unreviewed: "○",
  proposed: "○",
  confirmed: "✓",
  ambiguous: "?",
  rejected: "×",
};

const REVIEWER_ROLE_LABELS = {
  clinical_reviewer: "임상 검토자",
  clinician: "임상의",
  surgeon: "집도의",
};

const CONFIDENCE_LABELS = new Set([
  "low",
  "medium",
  "high",
  "not_assessable",
]);

const FIELD_CONFIDENCE_LABELS = {
  instrument: "도구",
  action: "행위",
  target: "대상",
  immediate_effects: "직후 관찰 결과",
  anatomy: "해부학",
  observability: "관측 가능성",
  observable_findings: "직접 관찰",
  clinical_interpretations: "임상 해석",
};

const TIMELINE_TRACKS = {
  activity_segment: "#activity-track",
  clinical_observation: "#observation-track",
  state_change: "#change-track",
  unobservable_span: "#unobservable-track",
};

function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function clone(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function asArray(value) {
  if (Array.isArray(value)) return value;
  if (value === null || value === undefined) return [];
  if (isObject(value)) {
    for (const key of ["annotations", "records", "items", "events", "actions"]) {
      if (Array.isArray(value[key])) return value[key];
    }
  }
  return [];
}

function finiteNumber(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric;
  }
  return null;
}

function finiteValues(values) {
  return values
    .filter((value) => value !== null && value !== undefined && value !== "")
    .map(Number)
    .filter(Number.isFinite);
}

function parseConfidenceText(rawValue) {
  const text = String(rawValue ?? "").trim();
  if (!text) return { empty: true, valid: true, value: null };
  if (CONFIDENCE_LABELS.has(text)) {
    return { empty: false, valid: true, value: text };
  }
  const numeric = Number(text);
  if (Number.isFinite(numeric) && numeric >= 0 && numeric <= 1) {
    return { empty: false, valid: true, value: numeric };
  }
  return { empty: false, valid: false, value: text };
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function normalizeStatus(value) {
  const status = String(value || "").toLowerCase();
  return ["confirmed", "ambiguous", "rejected"].includes(status)
    ? status
    : "unreviewed";
}

function annotationId(value, fallbackIndex = 0) {
  return String(
    value?.annotation_id ||
      value?._clinical_review?.annotation_id ||
      value?._review_ui?.annotation_id ||
      value?.candidate_id ||
      value?.id ||
      `clinical-${fallbackIndex}`,
  );
}

function annotationKind(value) {
  const kind = String(
    value?.annotation_kind || value?.kind || "clinical_observation",
  );
  return KIND_LABELS[kind] ? kind : "clinical_observation";
}

function activityFields(value) {
  return isObject(value?.activity) ? value.activity : {};
}

function anatomyFields(value) {
  return isObject(value?.anatomy) ? value.anatomy : {};
}

function anchorSec(value) {
  return (
    finiteNumber(
      value?.anchor_sec,
      value?.time_sec,
      value?.evidence_start_sec,
      value?.activity_start_sec,
    ) || 0
  );
}

function evidenceStartSec(value) {
  return (
    finiteNumber(
      value?.evidence_start_sec,
      value?.activity_start_sec,
      value?.anchor_sec,
      value?.time_sec,
    ) || 0
  );
}

function evidenceEndSec(value) {
  const start = evidenceStartSec(value);
  const end = finiteNumber(
    value?.evidence_end_sec,
    value?.activity_end_sec,
    value?.anchor_sec,
    value?.time_sec,
  );
  return Math.max(start, end === null ? start : end);
}

function formatTime(value) {
  const numeric = finiteNumber(value);
  if (numeric === null) return "—";
  const minutes = Math.floor(Math.max(0, numeric) / 60);
  const seconds = Math.max(0, numeric) - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${seconds
    .toFixed(3)
    .padStart(6, "0")}`;
}

function formatRange(start, end) {
  return `${formatTime(start)}–${formatTime(end)}`;
}

function humanize(value) {
  if (value === null || value === undefined || value === "") return "미상";
  if (typeof value === "string") return value.replaceAll("_", " ");
  if (isObject(value)) {
    return humanize(
      value.label ||
        value.name ||
        value.id ||
        value.finding ||
        value.interpretation ||
        value.description,
    );
  }
  return String(value);
}

function compactText(value) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }
  if (isObject(value)) {
    for (const key of [
      "finding",
      "observation",
      "interpretation",
      "label",
      "description",
      "effect",
      "value",
      "name",
    ]) {
      if (value[key] !== undefined && value[key] !== null) {
        return compactText(value[key]);
      }
    }
  }
  try {
    return JSON.stringify(value);
  } catch (_error) {
    return String(value);
  }
}

function valueList(value) {
  if (Array.isArray(value)) return value;
  if (value === null || value === undefined || value === "") return [];
  return [value];
}

function listToLines(value) {
  return valueList(value)
    .map(compactText)
    .filter(Boolean)
    .join("\n");
}

function labelsToText(value) {
  if (Array.isArray(value)) return value.map(compactText).join(", ");
  return compactText(value);
}

function textToLabels(text, original) {
  const value = String(text || "").trim();
  if (!Array.isArray(original)) return value;
  return value
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function listKeyForTemplate(template, purpose) {
  const keysByPurpose = {
    finding: ["finding", "observation", "description", "label", "value"],
    interpretation: [
      "interpretation",
      "label",
      "description",
      "finding",
      "value",
    ],
    effect: ["effect", "finding", "description", "label", "value"],
  };
  const keys = keysByPurpose[purpose] || ["value", "label", "description"];
  return keys.find((key) => Object.hasOwn(template, key)) || keys[0];
}

function linesToList(text, original, purpose) {
  const lines = String(text || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const originals = valueList(original);
  const usesObjects = originals.some(isObject);
  if (!usesObjects) return lines;
  return lines.map((line, index) => {
    const template = isObject(originals[index])
      ? clone(originals[index])
      : isObject(originals[0])
        ? clone(originals[0])
        : {};
    const key = listKeyForTemplate(template, purpose);
    template[key] = line;
    if (purpose === "interpretation") {
      template.needs_surgeon_review = true;
    }
    return template;
  });
}

function observabilityText(value) {
  if (!isObject(value)) return compactText(value);
  return compactText(
    value.overall || value.status || value.label || value.assessability || value,
  );
}

function updateObservability(original, text) {
  if (!isObject(original)) return text;
  const next = clone(original);
  const key = ["overall", "status", "label", "assessability"].find((candidate) =>
    Object.hasOwn(next, candidate),
  );
  next[key || "overall"] = text;
  return next;
}

function hasExplicitActivityBounds(annotation) {
  return (
    isObject(annotation) &&
    (Object.hasOwn(annotation, "activity_start_sec") ||
      Object.hasOwn(annotation, "activity_end_sec"))
  );
}

function updateActivityBoundsVisibility(annotation) {
  const editable =
    annotationKind(annotation) === "activity_segment" &&
    hasExplicitActivityBounds(annotation);
  $("#activity-bounds-fields").hidden = !editable;
  $("#activity-bounds-policy").hidden = editable;
  $("#activity-bounds-policy").textContent =
    annotationKind(annotation) === "activity_segment"
      ? "Explicit activity 경계가 없습니다. 불명확한 경계를 새로 만들지 않고 evidence만 유지합니다."
      : "Activity 경계는 activity_segment에만 허용됩니다. 유형 변경 시 기존 경계는 저장 payload에서 제거됩니다.";
}

function syncFieldConfidenceFromForm() {
  const fieldConfidence = isObject(state.draft?.field_confidence)
    ? clone(state.draft.field_confidence)
    : {};
  $$("[data-confidence-key]").forEach((input) => {
    const key = input.dataset.confidenceKey;
    const parsed = parseConfidenceText(input.value);
    if (parsed.empty) {
      delete fieldConfidence[key];
    } else {
      fieldConfidence[key] = parsed.value;
    }
  });
  state.draft.field_confidence = fieldConfidence;
}

function formValidationError() {
  const anatomyConfidence = parseConfidenceText($("#anatomy-confidence").value);
  if (anatomyConfidence.empty || !anatomyConfidence.valid) {
    return {
      control: $("#anatomy-confidence"),
      message:
        "해부학 confidence는 0–1 또는 low / medium / high / not_assessable 중 하나여야 합니다.",
    };
  }

  for (const input of $$("[data-confidence-key]")) {
    const parsed = parseConfidenceText(input.value);
    if (!parsed.empty && !parsed.valid) {
      return {
        control: input,
        message: `${FIELD_CONFIDENCE_LABELS[input.dataset.confidenceKey] || input.dataset.confidenceKey} confidence는 0–1 또는 low / medium / high / not_assessable 중 하나여야 합니다.`,
      };
    }
  }
  if (
    !isObject(state.draft?.field_confidence) ||
    Object.keys(state.draft.field_confidence).length === 0
  ) {
    return {
      control: $("#confidence-instrument"),
      message: "Field confidence는 적어도 한 항목을 포함해야 합니다.",
    };
  }

  if (
    annotationKind(state.draft) === "activity_segment" &&
    hasExplicitActivityBounds(state.draft)
  ) {
    const start = finiteNumber(state.draft.activity_start_sec);
    const end = finiteNumber(state.draft.activity_end_sec);
    if (start === null || end === null) {
      return {
        control:
          start === null ? $("#activity-start-sec") : $("#activity-end-sec"),
        message: "기존 activity 시작과 종료 경계는 함께 입력해야 합니다.",
      };
    }
    if (end < start) {
      return {
        control: $("#activity-end-sec"),
        message: "Activity 종료 경계는 시작 경계보다 빠를 수 없습니다.",
      };
    }
    if (start < timelineStart() || end > visualEndSec()) {
      return {
        control:
          start < timelineStart()
            ? $("#activity-start-sec")
            : $("#activity-end-sec"),
        message: "Activity 경계는 canonical visual timeline 안에 있어야 합니다.",
      };
    }
  }
  return null;
}

function requestedCaseFromUrl() {
  const value = new URLSearchParams(window.location.search).get("case");
  return value ? value.trim() : "";
}

function requestedModeFromUrl() {
  return new URLSearchParams(window.location.search).get("clinical_mode") ===
    "final"
    ? "final"
    : "draft";
}

function activeCaseId() {
  return String(
    state.clinical?.case_id ||
      state.context?.active_case_id ||
      state.context?.case_id ||
      requestedCaseFromUrl() ||
      "",
  );
}

function withCase(path, caseId = activeCaseId()) {
  const url = new URL(path, window.location.origin);
  if (caseId && !url.searchParams.has("case")) {
    url.searchParams.set("case", caseId);
  }
  return `${url.pathname}${url.search}`;
}

async function fetchJson(path, { optional = false } = {}) {
  let response;
  try {
    response = await fetch(withCase(path), { cache: "no-store" });
  } catch (_error) {
    if (optional) return null;
    throw new Error("서버에 연결할 수 없습니다. 연결을 확인해 주세요.");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (optional) return null;
    throw new Error(
      payload?.error || payload?.message || "검수 데이터를 불러오지 못했습니다.",
    );
  }
  return payload;
}

function candidateDigest(candidate) {
  return String(
    candidate?._clinical_review?.candidate_sha256 ||
      candidate?._review_ui?.candidate_sha256 ||
      candidate?.candidate_sha256 ||
      candidate?.sha256 ||
      "",
  );
}

function effectiveReviewMap() {
  const result = new Map();
  const visit = (value, keyHint = "") => {
    if (Array.isArray(value)) {
      value.forEach((item) => visit(item));
      return;
    }
    if (!isObject(value)) return;
    const id = String(
      value.annotation_id ||
        value.candidate_id ||
        value.adjudicated_annotation?.annotation_id ||
        keyHint ||
        "",
    );
    if (id && (value.review_status || value.adjudicated_annotation)) {
      result.set(id, value);
      return;
    }
    for (const [key, child] of Object.entries(value)) {
      if (["progress", "counts", "summary"].includes(key)) continue;
      visit(child, key);
    }
  };
  visit(state.clinical?.effective_reviews);
  return result;
}

function unwrapReferenceItem(value) {
  if (!isObject(value)) return value;
  return value.adjudicated_annotation || value.annotation || value.record || value;
}

function reviewPayload(value) {
  if (!isObject(value)) return null;
  if (isObject(value.clinical_review)) return value.clinical_review;
  if (isObject(value.review)) return value.review;
  return value;
}

function referenceRecords() {
  const reference = state.clinical?.reference;
  if (Array.isArray(reference)) return reference;
  if (!isObject(reference)) return [];
  for (const key of [
    "annotations",
    "records",
    "items",
    "events",
    "clinical_annotations",
  ]) {
    if (Array.isArray(reference[key])) return reference[key];
  }
  return Object.entries(reference)
    .filter(
      ([key, value]) =>
        !["available", "path", "sha256", "schema", "count"].includes(key) &&
        isObject(value) &&
        Boolean(
          value.annotation_id ||
            value.annotation?.annotation_id ||
            value.adjudicated_annotation?.annotation_id,
        ),
    )
    .map(([, value]) => value);
}

function buildItems() {
  if (!state.clinical) return [];
  if (state.viewMode === "final") {
    return referenceRecords()
      .map((record, index) => {
        const annotation = unwrapReferenceItem(record);
        if (!isObject(annotation)) return null;
        const review = reviewPayload(record);
        return {
          id: annotationId(annotation, index),
          source: annotation,
          annotation,
          review,
          status: normalizeStatus(
            review?.review_status ||
              record?.review_status ||
              annotation?.review_status ||
              "confirmed",
          ),
          digest: candidateDigest(annotation),
          final: true,
        };
      })
      .filter(Boolean)
      .sort((left, right) => anchorSec(left.annotation) - anchorSec(right.annotation));
  }

  const reviews = effectiveReviewMap();
  return asArray(state.clinical.candidates)
    .map((candidate, index) => {
      if (!isObject(candidate)) return null;
      const id = annotationId(candidate, index);
      const review = reviews.get(id) || null;
      const annotation =
        review?.adjudicated_annotation || review?.annotation || candidate;
      return {
        id,
        source: candidate,
        annotation,
        review,
        status: normalizeStatus(review?.review_status || candidate.review_status),
        digest: candidateDigest(candidate),
        final: false,
      };
    })
    .filter(Boolean)
    .sort((left, right) => anchorSec(left.annotation) - anchorSec(right.annotation));
}

function selectedItem() {
  return state.items.find((item) => item.id === state.selectedId) || null;
}

function statusLabel(status) {
  return STATUS_LABELS[normalizeStatus(status)] || "미검토";
}

function statusSymbol(status) {
  return STATUS_SYMBOLS[normalizeStatus(status)] || "○";
}

function itemTitle(item) {
  const annotation = item.annotation;
  const kind = annotationKind(annotation);
  const activity = activityFields(annotation);
  if (kind === "activity_segment") {
    return [activity.instrument, activity.action, activity.target]
      .map(labelsToText)
      .map((value) => humanize(value))
      .filter((value) => value !== "미상")
      .join(" · ") || "임상 행위";
  }
  if (kind === "unobservable_span") {
    const status = observabilityText(annotation.observability);
    return status
      ? `판독 불가 · ${humanize(status)}`
      : "판독 불가 구간";
  }
  return (
    compactText(valueList(annotation.observable_findings)[0]) ||
    KIND_LABELS[kind]
  );
}

function snapshotSignature(annotation = state.draft, notes = state.reviewNotes) {
  return JSON.stringify({ annotation, notes: String(notes || "") });
}

function hasDirtyDraft() {
  return (
    state.viewMode === "draft" &&
    Boolean(state.draft && state.baseline) &&
    snapshotSignature() !== state.baseline
  );
}

function guardNavigation() {
  if (state.saving) {
    toast("저장이 끝난 뒤 이동해 주세요.");
    return false;
  }
  if (!hasDirtyDraft()) return true;
  return window.confirm(
    "저장하지 않은 임상 필드 변경이 있습니다. 변경을 버리고 이동할까요?",
  );
}

function toast(message) {
  const element = $("#toast");
  clearTimeout(state.toastTimer);
  element.textContent = String(message);
  element.hidden = false;
  state.toastTimer = setTimeout(() => {
    element.hidden = true;
  }, 3000);
}

async function beginLoading() {
  state.loading = true;
  $("#clinical-main").setAttribute("aria-busy", "true");
  $("#candidate-list-error").hidden = true;
  $("#candidate-list-empty").hidden = true;
  $("#candidate-list").hidden = true;
  clearTimeout(state.loadingTimer);
  state.loadingTimer = setTimeout(() => {
    state.loadingShownAt = performance.now();
    $("#candidate-list-loading").hidden = false;
  }, 300);
}

async function finishLoading() {
  clearTimeout(state.loadingTimer);
  if (!$("#candidate-list-loading").hidden) {
    const elapsed = performance.now() - state.loadingShownAt;
    if (elapsed < 300) {
      await new Promise((resolve) => setTimeout(resolve, 300 - elapsed));
    }
  }
  $("#candidate-list-loading").hidden = true;
  $("#clinical-main").setAttribute("aria-busy", "false");
  state.loading = false;
}

function renderCaseSelector() {
  const selector = $("#case-selector");
  const rawCases =
    state.cases.length > 0
      ? state.cases
      : [
          {
            case_id: activeCaseId(),
            label: activeCaseId(),
          },
        ];
  selector.replaceChildren();
  rawCases
    .filter((entry) => entry?.case_id)
    .forEach((entry) => {
      const option = document.createElement("option");
      option.value = String(entry.case_id);
      option.textContent = String(entry.label || entry.case_id);
      selector.append(option);
    });
  if (activeCaseId()) selector.value = activeCaseId();
  selector.disabled = state.saving || selector.options.length < 2;
  selector.title =
    selector.options.length > 1
      ? "검수할 수술 영상을 선택합니다"
      : "현재 서버에는 한 영상만 등록되어 있습니다";
  $("#event-review-link").href = withCase("/");
}

function renderModeControl() {
  $$("[data-review-mode]").forEach((button) => {
    const active = button.dataset.reviewMode === state.viewMode;
    button.setAttribute("aria-pressed", String(active));
    button.disabled = state.saving;
  });
  const reviewerDisabled = state.saving || state.viewMode === "final";
  $("#reviewer-id").disabled = reviewerDisabled;
  $("#reviewer-role").disabled = reviewerDisabled;
}

function statusCounts() {
  const counts = { confirmed: 0, ambiguous: 0, rejected: 0, unreviewed: 0 };
  state.items.forEach((item) => {
    counts[normalizeStatus(item.status)] += 1;
  });
  return counts;
}

function renderProgress() {
  const counts = statusCounts();
  const total = state.items.length;
  const reviewed = counts.confirmed + counts.ambiguous + counts.rejected;
  const progressValue = total > 0 ? Math.round((reviewed / total) * 100) : 0;
  $("#review-progress-label").textContent =
    state.viewMode === "final" ? "임상 최종본" : "임상 초안 검수";
  $("#review-progress-count").textContent = `${reviewed} / ${total}`;
  $("#review-progress-track").setAttribute("aria-valuemax", String(total));
  $("#review-progress-track").setAttribute("aria-valuenow", String(reviewed));
  $("#review-progress-fill").style.width = `${progressValue}%`;
  $("#remaining-count").textContent =
    state.viewMode === "final"
      ? `${total} 최종`
      : `${counts.unreviewed} 미검토`;
  $("#confirmed-count").textContent = String(counts.confirmed);
  $("#ambiguous-count").textContent = String(counts.ambiguous);
  $("#rejected-count").textContent = String(counts.rejected);
}

function renderCandidateList() {
  const list = $("#candidate-list");
  list.replaceChildren();
  $("#candidate-list-error").hidden = true;
  $("#candidate-list-empty").hidden = state.items.length > 0;
  list.hidden = state.items.length === 0;

  if (state.items.length === 0) {
    $("#candidate-list-empty-title").textContent =
      state.viewMode === "final"
        ? "생성된 임상 최종본이 없습니다"
        : "표시할 임상 후보가 없습니다";
    $("#candidate-list-empty-detail").textContent =
      state.viewMode === "final"
        ? "사람 검수가 완료되면 별도 파생 최종본이 표시됩니다."
        : "이 case의 임상 후보 파일을 확인한 뒤 다시 불러와 주세요.";
    return;
  }

  state.items.forEach((item) => {
    const annotation = item.annotation;
    const kind = annotationKind(annotation);
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "candidate-list-item";
    button.dataset.annotationId = item.id;
    button.setAttribute("aria-current", String(item.id === state.selectedId));
    button.setAttribute(
      "aria-label",
      `${formatTime(anchorSec(annotation))} ${KIND_LABELS[kind]}, ${itemTitle(
        item,
      )}, ${statusLabel(item.status)}`,
    );

    const icon = document.createElement("span");
    icon.className = "candidate-kind-icon";
    icon.textContent = KIND_SYMBOLS[kind];
    icon.setAttribute("aria-hidden", "true");

    const copy = document.createElement("span");
    copy.className = "candidate-copy";
    const title = document.createElement("strong");
    title.textContent = itemTitle(item);
    const meta = document.createElement("small");
    meta.textContent = `${formatTime(anchorSec(annotation))} · ${
      KIND_LABELS[kind]
    }`;
    copy.append(title, meta);

    const status = document.createElement("span");
    status.className = `candidate-status ${normalizeStatus(item.status)}`;
    status.textContent = statusSymbol(item.status);
    status.title = statusLabel(item.status);
    status.setAttribute("aria-label", statusLabel(item.status));
    button.append(icon, copy, status);
    button.addEventListener("click", () => selectItem(item.id));
    li.append(button);
    list.append(li);
  });
}

function setText(selector, value, fallback = "—") {
  $(selector).textContent =
    value === null || value === undefined || value === "" ? fallback : String(value);
}

function historyForItem(item) {
  const raw = state.clinical?.action_history;
  const itemReview = reviewPayload(item.review);
  let actions = [];
  if (Array.isArray(raw)) {
    actions = raw;
  } else if (isObject(raw)) {
    const itemHistory = raw[item.id];
    actions = Array.isArray(itemHistory)
      ? itemHistory
      : isObject(itemHistory)
        ? [itemHistory]
        : asArray(raw.actions || raw.records);
  }
  if (Array.isArray(itemReview?.action_history)) {
    actions = [...actions, ...itemReview.action_history];
  }
  if (
    itemReview &&
    !actions.some(
      (action) =>
        (reviewPayload(action)?.action_id || action?.action_id) &&
        (reviewPayload(action)?.action_id || action?.action_id) ===
          itemReview.action_id,
    )
  ) {
    actions.push(itemReview);
  }
  const seen = new Set();
  return actions.filter((action) => {
    const review = reviewPayload(action);
    const id = String(
      review?.action_id ||
        action?.action_id ||
        review?.id ||
        action?.id ||
        `${review?.annotation_id || action?.annotation_id}:${
          review?.reviewed_at ||
          review?.created_at ||
          action?.reviewed_at ||
          action?.created_at
        }`,
    );
    const matches =
      String(
        review?.annotation_id ||
          action?.annotation_id ||
          review?.candidate_id ||
          action?.candidate_id ||
          review?.adjudicated_annotation?.annotation_id ||
          action?.adjudicated_annotation?.annotation_id ||
          "",
      ) === item.id;
    if (!matches || seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

function renderActionHistory(item) {
  const list = $("#action-history");
  list.replaceChildren();
  const actions = historyForItem(item);
  if (actions.length === 0) {
    const li = document.createElement("li");
    li.textContent = "아직 저장된 사람 검수 이력이 없습니다.";
    list.append(li);
    return;
  }
  actions.forEach((action) => {
    const li = document.createElement("li");
    const review = reviewPayload(action) || {};
    const reviewer =
      review.reviewer_id || review.reviewer || review.actor || "검토자 미상";
    const reviewerRole =
      review.reviewer_role || action.reviewer_role || "clinical_reviewer";
    const when =
      review.reviewed_at ||
      review.created_at ||
      review.timestamp ||
      "시각 미상";
    const roleLabel = REVIEWER_ROLE_LABELS[reviewerRole] || reviewerRole;
    li.textContent = `${statusLabel(
      review.review_status || action.review_status,
    )} · ${reviewer} (${roleLabel}) · ${when}`;
    if (review.notes) li.title = String(review.notes);
    list.append(li);
  });
}

function renderInspector() {
  const item = selectedItem();
  const empty = $("#inspector-empty");
  const form = $("#clinical-form");
  if (!item || !state.draft) {
    empty.hidden = false;
    form.hidden = true;
    $("#final-read-only").hidden = true;
    $("#selection-status").textContent = "선택 없음";
    $("#selection-status").removeAttribute("data-status");
    return;
  }

  empty.hidden = true;
  form.hidden = false;
  $("#final-read-only").hidden = state.viewMode !== "final";
  $("#selection-status").textContent = statusLabel(item.status);
  $("#selection-status").dataset.status = normalizeStatus(item.status);
  renderActionHistory(item);

  const provenance = isObject(state.draft.provenance)
    ? state.draft.provenance
    : {};
  setText("#provenance-generator", provenance.generator);
  setText("#provenance-model", provenance.model);
  setText("#provenance-authority", provenance.authority);
  setText("#source-views", valueList(state.draft.source_views).join(", "));

  const readOnly = state.viewMode === "final";
  form.querySelectorAll("input, select, textarea").forEach((control) => {
    control.disabled = readOnly || state.saving;
  });
  $("#discard-draft").hidden = readOnly;
  $(".review-actions").hidden = readOnly;
  $("#review-scope-help").hidden = readOnly;
  $("#dirty-indicator").hidden = readOnly;
  updateDirtyIndicator();
}

function populateForm() {
  const item = selectedItem();
  if (!item || !state.draft) return;
  const annotation = state.draft;
  const activity = activityFields(annotation);
  const anatomy = anatomyFields(annotation);
  const fieldConfidence = isObject(annotation.field_confidence)
    ? annotation.field_confidence
    : {};
  $("#annotation-kind").value = annotationKind(annotation);
  $("#activity-start-sec").value =
    annotation.activity_start_sec === undefined
      ? ""
      : String(annotation.activity_start_sec);
  $("#activity-end-sec").value =
    annotation.activity_end_sec === undefined
      ? ""
      : String(annotation.activity_end_sec);
  updateActivityBoundsVisibility(annotation);
  $("#instrument").value = labelsToText(activity.instrument);
  $("#action").value = labelsToText(activity.action);
  $("#target").value = labelsToText(activity.target);
  $("#immediate-effects").value = listToLines(activity.immediate_effects);
  $("#anatomy-label").value = compactText(anatomy.label);
  $("#anatomy-granularity").value = compactText(anatomy.granularity);
  $("#anatomy-visibility").value = compactText(anatomy.visibility);
  $("#anatomy-confidence").value = compactText(anatomy.confidence);
  $("#observability").value = observabilityText(annotation.observability);
  $("#observable-findings").value = listToLines(annotation.observable_findings);
  $("#clinical-interpretations").value = listToLines(
    annotation.clinical_interpretations,
  );
  $$("[data-confidence-key]").forEach((input) => {
    const value = fieldConfidence[input.dataset.confidenceKey];
    input.value = value === null || value === undefined ? "" : String(value);
    input.removeAttribute("aria-invalid");
  });
  $("#review-notes").value = state.reviewNotes;
  $("#anchor-summary").textContent = `f${
    annotation.anchor_source_frame_idx ?? "—"
  } · ${formatTime(anchorSec(annotation))}`;
  $("#evidence-summary").textContent = formatRange(
    evidenceStartSec(annotation),
    evidenceEndSec(annotation),
  );
  $("#form-error").hidden = true;
}

function syncDraftFromForm() {
  if (!state.draft || state.viewMode === "final") return;
  const nextKind = $("#annotation-kind").value;
  const hadExplicitBounds = hasExplicitActivityBounds(state.draft);
  const originalActivity = activityFields(state.draft);
  const activity = clone(originalActivity);
  activity.instrument = textToLabels(
    $("#instrument").value,
    originalActivity.instrument,
  );
  activity.action = textToLabels($("#action").value, originalActivity.action);
  activity.target = textToLabels($("#target").value, originalActivity.target);
  activity.immediate_effects = linesToList(
    $("#immediate-effects").value,
    originalActivity.immediate_effects,
    "effect",
  );
  state.draft.activity = activity;

  const originalAnatomy = anatomyFields(state.draft);
  const anatomy = clone(originalAnatomy);
  anatomy.label = $("#anatomy-label").value.trim();
  anatomy.granularity = $("#anatomy-granularity").value.trim();
  anatomy.visibility = $("#anatomy-visibility").value.trim();
  const confidenceText = $("#anatomy-confidence").value.trim();
  const confidenceNumber = finiteNumber(confidenceText);
  anatomy.confidence =
    confidenceNumber === null ? confidenceText : confidenceNumber;
  state.draft.anatomy = anatomy;
  state.draft.annotation_kind = nextKind;
  if (nextKind !== "activity_segment") {
    delete state.draft.activity_start_sec;
    delete state.draft.activity_end_sec;
  } else if (hadExplicitBounds) {
    const startText = $("#activity-start-sec").value.trim();
    const endText = $("#activity-end-sec").value.trim();
    state.draft.activity_start_sec =
      finiteNumber(startText) === null ? startText : Number(startText);
    state.draft.activity_end_sec =
      finiteNumber(endText) === null ? endText : Number(endText);
  }
  state.draft.observability = updateObservability(
    state.draft.observability,
    $("#observability").value.trim(),
  );
  state.draft.observable_findings = linesToList(
    $("#observable-findings").value,
    state.draft.observable_findings,
    "finding",
  );
  state.draft.clinical_interpretations = linesToList(
    $("#clinical-interpretations").value,
    state.draft.clinical_interpretations,
    "interpretation",
  );
  syncFieldConfidenceFromForm();
  state.reviewNotes = $("#review-notes").value;
  updateActivityBoundsVisibility(state.draft);
  updateDirtyIndicator();
  renderClinicalOverlay();
  renderTimeline();
}

function updateDirtyIndicator() {
  const indicator = $("#dirty-indicator");
  const dirty = hasDirtyDraft();
  indicator.dataset.dirty = String(dirty);
  indicator.textContent = dirty ? "저장하지 않은 변경" : "저장된 상태";
  $("#discard-draft").disabled = !dirty || state.saving;
}

function discardDraft() {
  const item = selectedItem();
  if (!item || !state.baseline || state.viewMode === "final") return;
  const baseline = JSON.parse(state.baseline);
  state.draft = clone(baseline.annotation);
  state.reviewNotes = baseline.notes;
  populateForm();
  renderInspector();
  renderClinicalOverlay();
  renderTimeline();
  toast("저장하지 않은 변경을 취소했습니다.");
}

function selectItem(id, { bypassDirty = false, seek = true, focus = false } = {}) {
  if (id === state.selectedId && state.draft) {
    if (seek) seekToEvidenceStart();
    if (focus) {
      $(`[data-annotation-id="${CSS.escape(id)}"]`)?.focus();
    }
    return true;
  }
  if (!bypassDirty && !guardNavigation()) return false;
  const item = state.items.find((entry) => entry.id === id);
  if (!item) return false;
  state.selectedId = item.id;
  state.draft = clone(item.annotation);
  state.reviewNotes = String(
    item.review?.review?.notes ||
      item.review?.notes ||
      item.review?.review_notes ||
      "",
  );
  state.baseline = snapshotSignature(state.draft, state.reviewNotes);
  populateForm();
  renderCandidateList();
  renderInspector();
  renderClinicalOverlay();
  renderTimeline();
  if (seek) seekToEvidenceStart();
  if (focus) {
    $(`[data-annotation-id="${CSS.escape(item.id)}"]`)?.focus();
  }
  return true;
}

function renderClinicalOverlay() {
  const annotation = state.draft;
  const observable = $("#observable-overlay");
  const interpretation = $("#interpretation-overlay");
  if (!annotation) {
    observable.hidden = true;
    interpretation.hidden = true;
    $("#annotation-output").textContent = "후보 —";
    $("#evidence-range").textContent = "근거 구간 —";
    return;
  }
  const item = selectedItem();
  const activity = activityFields(annotation);
  const findings = valueList(annotation.observable_findings)
    .map(compactText)
    .filter(Boolean);
  const interpretations = valueList(annotation.clinical_interpretations)
    .map(compactText)
    .filter(Boolean);
  const activityTitle = [activity.instrument, activity.action, activity.target]
    .map(humanize)
    .filter((value) => value !== "미상")
    .join(" · ");

  observable.hidden = false;
  $("#observable-overlay-title").textContent =
    activityTitle || KIND_LABELS[annotationKind(annotation)];
  $("#observable-overlay-detail").textContent =
    findings.slice(0, 2).join(" · ") ||
    "직접 관찰 항목이 비어 있습니다. 영상 근거를 확인해 주세요.";
  interpretation.hidden = interpretations.length === 0;
  $("#interpretation-overlay-title").textContent =
    interpretations.slice(0, 2).join(" · ") || "임상 해석 없음";
  $("#interpretation-overlay-detail").textContent =
    "AI 제안이며 임상 정답으로 확정되지 않았습니다.";
  $("#annotation-output").textContent = item?.id || "후보 —";
  $("#evidence-range").textContent = `근거 ${formatRange(
    evidenceStartSec(annotation),
    evidenceEndSec(annotation),
  )}`;
}

function timelineStart() {
  return (
    finiteNumber(
      state.clinical?.manifest?.start_sec,
      state.clinical?.manifest?.source_timeline?.start_sec,
      state.context?.start_sec,
      state.context?.timestamps_sec?.[0],
    ) || 0
  );
}

function timelineEnd() {
  const contextTimestamps = Array.isArray(state.context?.timestamps_sec)
    ? state.context.timestamps_sec
    : [];
  const audioTail = Array.isArray(
    state.clinical?.manifest?.review_media?.audio_only_tail_sec,
  )
    ? state.clinical.manifest.review_media.audio_only_tail_sec
    : [];
  const candidateEnds = state.items.map((item) => {
    const annotation =
      item.id === state.selectedId && state.draft ? state.draft : item.annotation;
    return markerBounds(annotation).end;
  });
  const declaredEnds = finiteValues([
    state.clinical?.manifest?.review_media?.container_end_sec,
    ...audioTail,
    state.clinical?.media?.duration_sec,
    state.clinical?.manifest?.duration_sec,
    state.clinical?.manifest?.media?.duration_sec,
    state.context?.media?.duration_sec,
    state.context?.end_sec,
    contextTimestamps.at(-1),
    video.duration,
    ...candidateEnds,
  ]);
  return Math.max(
    timelineStart() + 0.001,
    declaredEnds.length ? Math.max(...declaredEnds) : 1,
  );
}

function visualEndSec() {
  const contextTimestamps = Array.isArray(state.context?.timestamps_sec)
    ? state.context.timestamps_sec
    : [];
  const explicitEnds = finiteValues([
    state.clinical?.manifest?.review_media?.video_end_sec,
    state.clinical?.manifest?.source_timeline?.end_sec,
    state.clinical?.media?.visual_end_sec,
    state.context?.media?.visual_end_sec,
    state.context?.visual_end_sec,
  ]);
  if (explicitEnds.length) {
    return Math.max(timelineStart(), Math.max(...explicitEnds));
  }
  const evidenceEnds = finiteValues(
    state.items.map((item) => evidenceEndSec(item.annotation)),
  );
  if (evidenceEnds.length) {
    return Math.max(timelineStart(), Math.max(...evidenceEnds));
  }
  return (
    finiteNumber(
      state.clinical?.manifest?.duration_sec,
      state.clinical?.manifest?.media?.duration_sec,
      state.context?.media?.duration_sec,
      state.context?.end_sec,
      contextTimestamps.at(-1),
      video.duration,
    ) || timelineEnd()
  );
}

function timelinePercent(timeSec) {
  const start = timelineStart();
  return (
    (clamp(Number(timeSec) || start, start, timelineEnd()) - start) /
    (timelineEnd() - start)
  ) * 100;
}

function markerBounds(annotation) {
  const kind = annotationKind(annotation);
  if (kind === "activity_segment") {
    return {
      start:
        finiteNumber(annotation.activity_start_sec, annotation.evidence_start_sec) ??
        anchorSec(annotation),
      end:
        finiteNumber(annotation.activity_end_sec, annotation.evidence_end_sec) ??
        anchorSec(annotation),
      range: true,
    };
  }
  if (kind === "unobservable_span") {
    const observability = isObject(annotation.observability)
      ? annotation.observability
      : {};
    for (const key of [
      "missing_interval_sec",
      "missing_visual_interval_sec",
      "unobservable_interval_sec",
    ]) {
      const interval = observability[key];
      if (!Array.isArray(interval) || interval.length < 2) continue;
      const start = finiteNumber(interval[0]);
      const end = finiteNumber(interval[1]);
      if (start === null || end === null) continue;
      return {
        start: Math.min(start, end),
        end: Math.max(start, end),
        range: true,
      };
    }
    return {
      start: evidenceStartSec(annotation),
      end: evidenceEndSec(annotation),
      range: true,
    };
  }
  return { start: anchorSec(annotation), end: anchorSec(annotation), range: false };
}

function renderTimelineRuler() {
  const ruler = $("#timeline-ruler");
  ruler.replaceChildren();
  [0, 0.25, 0.5, 0.75, 1].forEach((fraction) => {
    const tick = document.createElement("span");
    tick.className = "timeline-tick";
    tick.style.left = `${fraction * 100}%`;
    tick.textContent = formatTime(
      timelineStart() + fraction * (timelineEnd() - timelineStart()),
    ).slice(0, 5);
    ruler.append(tick);
  });
}

function renderTimeline() {
  Object.values(TIMELINE_TRACKS).forEach((selector) => {
    $(selector).replaceChildren();
  });
  renderTimelineRuler();
  state.items.forEach((item) => {
    const annotation =
      item.id === state.selectedId && state.draft ? state.draft : item.annotation;
    const kind = annotationKind(annotation);
    const track = $(TIMELINE_TRACKS[kind]);
    if (!track) return;
    const bounds = markerBounds(annotation);
    const marker = document.createElement("button");
    marker.type = "button";
    marker.className = `timeline-marker${bounds.range ? " range-marker" : ""}`;
    marker.dataset.annotationId = item.id;
    marker.dataset.kind = kind;
    marker.dataset.status = normalizeStatus(item.status);
    marker.setAttribute("aria-current", String(item.id === state.selectedId));
    marker.setAttribute(
      "aria-label",
      `${KIND_LABELS[kind]} ${itemTitle(item)}, ${formatRange(
        bounds.start,
        bounds.end,
      )}, ${statusLabel(item.status)}`,
    );
    const left = timelinePercent(bounds.start);
    if (bounds.range) {
      const right = timelinePercent(Math.max(bounds.start, bounds.end));
      marker.style.left = `${left}%`;
      marker.style.width = `max(44px, ${Math.max(0.2, right - left)}%)`;
    } else {
      marker.style.left = `calc(${left}% - 22px)`;
    }
    marker.addEventListener("click", (event) => {
      event.stopPropagation();
      selectItem(item.id);
    });
    track.append(marker);
  });
  updateTimelinePlayhead();
}

function updateTimelinePlayhead() {
  const timeline = $("#timeline");
  const playhead = $("#timeline-playhead");
  const labelWidth = Number.parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue(
      "--track-label-width",
    ),
  );
  const usableWidth = Math.max(0, timeline.clientWidth - labelWidth);
  const fraction = timelinePercent(state.currentTimeSec) / 100;
  playhead.style.left = `${labelWidth + usableWidth * fraction}px`;
  playhead.setAttribute("aria-valuemin", String(timelineStart()));
  playhead.setAttribute("aria-valuemax", String(timelineEnd()));
  playhead.setAttribute("aria-valuenow", String(state.currentTimeSec));
  playhead.setAttribute("aria-valuetext", formatTime(state.currentTimeSec));
}

function configureView(viewName) {
  const next = viewName === "composite" ? "composite" : "flir";
  if (next === "composite" && $("#composite-view").disabled) return;
  state.videoView = next;
  $("#video-shell").dataset.videoView = next;
  $$(".view-mode-control [data-video-view]").forEach((button) => {
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.videoView === next),
    );
  });

  const independent = state.mediaMode === "independent";
  $(".companion-pane").hidden = !(next === "composite" && independent);
  if (next === "flir") {
    $("#viewer-title").textContent = "수술부위 근접뷰";
    $("#viewer-source-badge").textContent =
      state.mediaMode === "legacy-composite" ? "FLIR crop" : "FLIR";
    $("#video-shell").setAttribute(
      "aria-label",
      "선택한 임상 후보의 FLIR 근접 근거 영상",
    );
  } else {
    $("#viewer-title").textContent = "CAM4 + FLIR 합성 문맥";
    $("#viewer-source-badge").textContent =
      independent ? "동기 2-view" : "원본 composite";
    $("#video-shell").setAttribute(
      "aria-label",
      "선택한 임상 후보의 CAM4와 FLIR 합성 근거 영상",
    );
  }
}

function mediaDescriptor() {
  const media =
    state.clinical?.media ||
    state.clinical?.manifest?.media ||
    state.context?.media ||
    {};
  const views = isObject(media.video_views) ? media.video_views : {};
  const flirUrl =
    views.flir?.video_url ||
    views.flir?.url ||
    media.flir_video_url ||
    media.flir_url;
  const cam4Url =
    views.cam4?.video_url ||
    views.cam4?.url ||
    media.cam4_video_url ||
    media.cam4_url;
  const legacyUrl =
    media.composite_video_url ||
    media.video_url ||
    media.review_video_url ||
    state.clinical?.manifest?.review_video_url;
  if (flirUrl && cam4Url) {
    return {
      mode: "independent",
      primaryUrl: withCase(flirUrl),
      companionUrl: withCase(cam4Url),
    };
  }
  if (legacyUrl) {
    return {
      mode: "legacy-composite",
      primaryUrl: withCase(legacyUrl),
      companionUrl: null,
    };
  }
  return null;
}

function setVideoFailure(message) {
  state.videoReady = false;
  $("#video-loading").hidden = true;
  $("#video-empty").hidden = true;
  $("#video-error").hidden = false;
  $("#video-error-detail").textContent = message;
  $("#play-toggle").disabled = true;
}

function configureMedia() {
  pausePlayback();
  state.videoReady = false;
  state.companionReady = false;
  state.mediaConfigured = false;
  $("#video-loading").hidden = false;
  $("#video-empty").hidden = true;
  $("#video-error").hidden = true;
  $("#play-toggle").disabled = true;
  const descriptor = mediaDescriptor();
  if (!descriptor) {
    $("#video-loading").hidden = true;
    $("#video-empty").hidden = false;
    $("#video-empty strong").textContent = "이 case에 연결된 근거 영상이 없습니다";
    $("#video-empty p").textContent =
      "임상 manifest와 기존 타임라인 미디어 포인터를 확인해 주세요.";
    state.mediaMode = "unavailable";
    $("#video-shell").dataset.mediaMode = "unavailable";
    return;
  }
  state.mediaMode = descriptor.mode;
  $("#video-shell").dataset.mediaMode = descriptor.mode;
  video.src = descriptor.primaryUrl;
  companionVideo.removeAttribute("src");
  if (descriptor.companionUrl) {
    companionVideo.src = descriptor.companionUrl;
    companionVideo.load();
  }
  video.load();
  state.mediaConfigured = true;
  $("#composite-view").disabled = false;
  configureView(state.videoView);
}

function syncCompanion({ force = false, play = false } = {}) {
  if (state.mediaMode !== "independent" || !state.companionReady) return;
  if (
    force ||
    Math.abs(companionVideo.currentTime - video.currentTime) > 0.08
  ) {
    try {
      companionVideo.currentTime = video.currentTime;
    } catch (_error) {
      // Metadata can become available one task after the loadedmetadata event.
    }
  }
  companionVideo.playbackRate = video.playbackRate;
  if (play) {
    companionVideo.play().catch(() => {});
  } else if (video.paused) {
    companionVideo.pause();
  }
}

function selectedEvidenceBounds() {
  const annotation = state.draft || selectedItem()?.annotation;
  if (!annotation) return null;
  return {
    start: evidenceStartSec(annotation),
    end: evidenceEndSec(annotation),
  };
}

function seekToTime(timeSec) {
  const target = clamp(
    Number(timeSec) || 0,
    timelineStart(),
    Math.min(timelineEnd(), visualEndSec()),
  );
  state.currentTimeSec = target;
  if (state.videoReady) {
    try {
      video.currentTime = Math.min(
        target,
        Number.isFinite(video.duration) ? video.duration : target,
      );
    } catch (_error) {
      // Seeking before metadata is harmless; loadedmetadata repeats the seek.
    }
  }
  if (state.companionReady) syncCompanion({ force: true });
  updatePlaybackReadout();
}

function seekToEvidenceStart() {
  const bounds = selectedEvidenceBounds();
  if (!bounds) return;
  seekToTime(bounds.start);
}

function updatePlaybackReadout() {
  $("#time-output").textContent = formatTime(state.currentTimeSec);
  updateTimelinePlayhead();
  renderReadOnlyContext();
}

function pausePlayback() {
  video.pause();
  companionVideo.pause();
}

async function togglePlayback() {
  if (!state.videoReady) return;
  if (!video.paused) {
    pausePlayback();
    return;
  }
  const bounds = selectedEvidenceBounds();
  if (
    bounds &&
    $("#loop-evidence").checked &&
    state.currentTimeSec >= bounds.end - 0.04
  ) {
    seekToTime(bounds.start);
  }
  try {
    await video.play();
    syncCompanion({ force: true, play: true });
  } catch (_error) {
    setVideoFailure("브라우저가 재생을 시작하지 못했습니다. 다시 시도해 주세요.");
  }
}

function contextTimestamps() {
  return Array.isArray(state.context?.timestamps_sec)
    ? state.context.timestamps_sec
    : [];
}

function timeForContextFrame(frameIndex) {
  const timestamps = contextTimestamps();
  const index = Number(frameIndex);
  if (Number.isInteger(index) && timestamps[index] !== undefined) {
    return Number(timestamps[index]);
  }
  return null;
}

function contextEffectiveAction(candidate) {
  return (
    candidate?._review_ui?.effective_decision ||
    candidate?.effective_decision ||
    candidate?._review_ui?.legacy_decision ||
    candidate?._review_ui?.human_decision ||
    null
  );
}

function contextFields(candidate) {
  const effective = contextEffectiveAction(candidate)?.adjudicated_fields || {};
  const eventType =
    effective.event_type || candidate?.event_type || "implicit_tool_request";
  const sourceFrame = finiteNumber(
    effective.source_frame_idx,
    candidate?.source_frame_idx,
  );
  const startFrame = finiteNumber(
    effective.start_source_frame_idx,
    candidate?.start_source_frame_idx,
    sourceFrame,
  );
  const endFrame = finiteNumber(
    effective.end_source_frame_idx,
    candidate?.end_source_frame_idx,
    startFrame,
  );
  return {
    event_type: eventType,
    source_frame_idx: sourceFrame,
    start_source_frame_idx: startFrame,
    end_source_frame_idx: endFrame,
    time_sec:
      finiteNumber(candidate?.time_sec, timeForContextFrame(sourceFrame)) || 0,
    start_sec:
      finiteNumber(candidate?.start_sec, timeForContextFrame(startFrame)) || 0,
    end_sec:
      finiteNumber(candidate?.end_sec, timeForContextFrame(endFrame)) || 0,
    tool: effective.tool ?? candidate?.tool,
    from: effective.from ?? candidate?.from,
    to: effective.to ?? candidate?.to,
    phase_id: effective.phase_id ?? candidate?.phase_id,
  };
}

function contextInteractionItems() {
  const collections = [
    state.context?.effective_annotations,
    state.context?.candidates,
    state.context?.human_annotations,
  ];
  const items = [];
  const seen = new Set();
  collections.forEach((collection) => {
    asArray(collection).forEach((candidate, index) => {
      const id = String(
        candidate?._review_ui?.candidate_id ||
          candidate?.candidate_id ||
          candidate?.event_id ||
          `context-${index}`,
      );
      if (seen.has(id)) return;
      const status = normalizeStatus(
        contextEffectiveAction(candidate)?.review_status ||
          candidate?.review_status,
      );
      if (status === "rejected") return;
      seen.add(id);
      items.push(candidate);
    });
  });
  return items;
}

function phaseEvents() {
  const track = state.finalContext?.context_tracks?.phase;
  const events = asArray(track?.events);
  if (track?.available && events.length > 0) return events;
  return contextInteractionItems().filter(
    (item) => contextFields(item).event_type === "phase_start",
  );
}

function speechEvents() {
  const track = state.finalContext?.context_tracks?.speech;
  return track?.available ? asArray(track.events) : [];
}

function speechAvailabilitySec(event) {
  return (
    finiteNumber(
      event?.available_sec,
      event?._review_ui?.complete_text_available_sec,
      event?.time_sec,
    ) || 0
  );
}

function speechStage(event, timeSec = state.currentTimeSec) {
  const end = finiteNumber(event?.end_sec);
  const available = speechAvailabilitySec(event);
  if (timeSec + 1e-7 >= available) return "text_available";
  if (end !== null && timeSec <= end + 1e-7) return "speaking";
  return "awaiting_text";
}

function currentSpeechEvent() {
  const now = state.currentTimeSec;
  const holdSec = 2.5;
  return (
    speechEvents()
      .filter((event) => {
        const start = finiteNumber(event?.time_sec);
        const end = Math.max(start || 0, finiteNumber(event?.end_sec) || start || 0);
        const available = speechAvailabilitySec(event);
        return (
          start !== null &&
          now + 1e-7 >= start &&
          now <= Math.max(end, available) + holdSec + 1e-7
        );
      })
      .sort(
        (left, right) =>
          Number(right?.time_sec || 0) - Number(left?.time_sec || 0),
      )[0] || null
  );
}

function renderReadOnlyContext() {
  const now = state.currentTimeSec;
  const phase =
    phaseEvents()
      .map((event) => ({ event, fields: contextFields(event) }))
      .filter(({ fields }) => fields.time_sec <= now + 1e-7)
      .sort((left, right) => right.fields.time_sec - left.fields.time_sec)[0] ||
    null;
  $("#phase-context").textContent = phase
    ? `${humanize(phase.fields.phase_id || phase.event.phase_id)} · ${formatTime(
        phase.fields.time_sec,
      )}부터`
    : state.contextError
      ? "문맥을 불러오지 못함"
      : "현재 Phase 문맥 없음";

  const speech = currentSpeechEvent();
  const speechItem = $(".speech-context-item");
  if (!speech) {
    speechItem.removeAttribute("data-stage");
    $("#speech-context-badge").textContent = "음성 · 정답 아님";
    $("#speech-context").textContent = state.contextError
      ? "음성 문맥을 불러오지 못함"
      : "현재 발화 없음";
  } else {
    const stage = speechStage(speech);
    speechItem.dataset.stage = stage;
    if (stage === "text_available") {
      $("#speech-context-badge").textContent = "음성 문맥 · 정답 아님";
      $("#speech-context").textContent = speech.text || "발화 원문 없음";
    } else if (stage === "speaking") {
      $("#speech-context-badge").textContent = "음성 발화 중 · 정답 아님";
      $("#speech-context").textContent = `원문은 ${formatTime(
        speechAvailabilitySec(speech),
      )}부터 표시`;
    } else {
      $("#speech-context-badge").textContent =
        "음성 텍스트 처리 중 · 정답 아님";
      $("#speech-context").textContent = `발화 종료 · ${formatTime(
        speechAvailabilitySec(speech),
      )}부터 원문 표시`;
    }
  }

  const interactions = contextInteractionItems().map((event) => ({
    event,
    fields: contextFields(event),
  }));
  const activeRequest = interactions
    .filter(
      ({ fields }) =>
        fields.event_type === "implicit_tool_request" &&
        fields.start_sec <= now + 1e-7 &&
        fields.end_sec >= now - 1e-7,
    )
    .sort((left, right) => right.fields.start_sec - left.fields.start_sec)[0];
  const nearbyTransfer = interactions
    .filter(
      ({ fields }) =>
        fields.event_type === "tool_transfer" &&
        Math.abs(fields.time_sec - now) <= 1.5,
    )
    .sort(
      (left, right) =>
        Math.abs(left.fields.time_sec - now) -
        Math.abs(right.fields.time_sec - now),
    )[0];
  if (activeRequest) {
    $("#interaction-context").textContent = `암묵적 요청 · ${formatRange(
      activeRequest.fields.start_sec,
      activeRequest.fields.end_sec,
    )}`;
  } else if (nearbyTransfer) {
    const fields = nearbyTransfer.fields;
    $("#interaction-context").textContent = `${humanize(fields.tool)} · ${humanize(
      fields.from,
    )} → ${humanize(fields.to)}`;
  } else {
    $("#interaction-context").textContent = state.contextError
      ? "탐색 문맥을 불러오지 못함"
      : "현재 이벤트 없음";
  }
}

function setSaving(saving) {
  state.saving = saving;
  renderCaseSelector();
  renderModeControl();
  $("#confirm").disabled = saving;
  $("#ambiguous").disabled = saving;
  $("#reject").disabled = saving;
  $("#discard-draft").disabled = saving || !hasDirtyDraft();
  $("#reviewer-id").disabled = saving || state.viewMode === "final";
  $("#reviewer-role").disabled = saving || state.viewMode === "final";
  $("#clinical-form")
    .querySelectorAll("input, select, textarea")
    .forEach((control) => {
      control.disabled = saving || state.viewMode === "final";
    });
}

function stripReviewMetadata(annotation) {
  const clean = clone(annotation);
  delete clean._clinical_review;
  delete clean._review_ui;
  delete clean.candidate_sha256;
  if (Array.isArray(clean.clinical_interpretations)) {
    clean.clinical_interpretations = clean.clinical_interpretations.map(
      (interpretation) => {
        if (!isObject(interpretation)) return interpretation;
        return { ...interpretation, needs_surgeon_review: true };
      },
    );
  }
  if (Object.hasOwn(clean, "needs_surgeon_review")) {
    clean.needs_surgeon_review = true;
  }
  return clean;
}

function clientRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `clinical-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function saveReview(reviewStatus) {
  if (state.viewMode !== "draft" || state.saving) return;
  const item = selectedItem();
  if (!item || !state.draft) {
    toast("먼저 임상 후보를 선택해 주세요.");
    return;
  }
  syncDraftFromForm();
  const validationError = formValidationError();
  if (validationError) {
    validationError.control.setAttribute("aria-invalid", "true");
    $("#form-error").textContent = validationError.message;
    $("#form-error").hidden = false;
    validationError.control.focus();
    return;
  }
  const reviewerId = $("#reviewer-id").value.trim();
  if (!reviewerId) {
    $("#reviewer-id").setAttribute("aria-invalid", "true");
    $("#form-error").textContent = "검토자 ID를 입력한 뒤 판정을 저장해 주세요.";
    $("#form-error").hidden = false;
    $("#reviewer-id").focus();
    return;
  }
  const reviewerRole = Object.hasOwn(
    REVIEWER_ROLE_LABELS,
    $("#reviewer-role").value,
  )
    ? $("#reviewer-role").value
    : "clinical_reviewer";
  if (!item.digest) {
    $("#form-error").textContent =
      "후보별 SHA-256이 없어 저장할 수 없습니다. 임상 상태를 다시 불러와 주세요.";
    $("#form-error").hidden = false;
    return;
  }
  const start = evidenceStartSec(state.draft);
  const anchor = anchorSec(state.draft);
  const end = evidenceEndSec(state.draft);
  if (!(start <= anchor && anchor <= end)) {
    $("#form-error").textContent =
      "근거 구간은 시작 ≤ anchor ≤ 종료 순서여야 합니다.";
    $("#form-error").hidden = false;
    return;
  }

  $("#form-error").hidden = true;
  setSaving(true);
  const body = {
    case_id: activeCaseId(),
    revision: state.clinical?.revision,
    annotation_id: item.id,
    candidate_sha256: item.digest,
    review_status: reviewStatus,
    reviewer_id: reviewerId,
    reviewer_role: reviewerRole,
    notes: state.reviewNotes.trim(),
    adjudicated_annotation: stripReviewMetadata(state.draft),
    client_request_id: clientRequestId(),
  };
  const supersedes =
    item.review?.action_id ||
    item.review?.id ||
    item.review?.review_action_id ||
    "";
  if (supersedes) body.supersedes_action_id = supersedes;

  try {
    const response = await fetch(withCase("/api/clinical-action"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message =
        response.status === 409
          ? "다른 검토가 먼저 저장되어 revision이 바뀌었습니다. 변경을 복사해 둔 뒤 다시 불러와 주세요."
          : payload?.error || payload?.message || "임상 판정을 저장하지 못했습니다.";
      throw new Error(message);
    }
    const nextState = isObject(payload.state) ? payload.state : null;
    if (nextState) {
      state.clinical = nextState;
    } else {
      state.clinical = await fetchJson("/api/clinical-review");
    }
    const selectedId = item.id;
    state.items = buildItems();
    state.selectedId = null;
    state.draft = null;
    state.baseline = null;
    renderAll();
    selectItem(selectedId, { bypassDirty: true, seek: false });
    toast(
      `${statusLabel(reviewStatus)} 판정을 append-only 검수 이력에 저장했습니다.`,
    );
  } catch (error) {
    $("#form-error").textContent = error.message;
    $("#form-error").hidden = false;
  } finally {
    setSaving(false);
  }
}

function renderAll() {
  state.items = buildItems();
  renderCaseSelector();
  renderModeControl();
  renderProgress();
  renderCandidateList();
  renderInspector();
  renderClinicalOverlay();
  renderTimeline();
  renderReadOnlyContext();
}

function switchMode(nextMode) {
  const mode = nextMode === "final" ? "final" : "draft";
  if (mode === state.viewMode) return;
  if (!guardNavigation()) return;
  state.viewMode = mode;
  state.selectedId = null;
  state.draft = null;
  state.baseline = null;
  const url = new URL(window.location.href);
  if (mode === "final") {
    url.searchParams.set("clinical_mode", "final");
  } else {
    url.searchParams.delete("clinical_mode");
  }
  window.history.replaceState({}, "", `${url.pathname}${url.search}`);
  renderAll();
  const first =
    state.items.find((item) => item.status === "unreviewed") || state.items[0];
  if (first) selectItem(first.id, { bypassDirty: true });
}

function navigateItem(direction) {
  if (state.items.length === 0 || !guardNavigation()) return;
  const index = Math.max(
    0,
    state.items.findIndex((item) => item.id === state.selectedId),
  );
  const next = state.items[clamp(index + direction, 0, state.items.length - 1)];
  if (next) selectItem(next.id, { bypassDirty: true, focus: true });
}

async function loadContextPointers(payload) {
  const pointers = isObject(payload?.context_api) ? payload.context_api : {};
  const timelineUrl =
    pointers.timeline_state_url || pointers.state_url || "/api/state";
  const finalUrl =
    pointers.final_review_url ||
    payload?.context_api?.speech_context_url ||
    "/api/final-review";
  const [contextResult, finalResult] = await Promise.allSettled([
    fetchJson(timelineUrl, { optional: true }),
    fetchJson(finalUrl, { optional: true }),
  ]);
  state.context =
    contextResult.status === "fulfilled" ? contextResult.value : null;
  state.finalContext =
    finalResult.status === "fulfilled" ? finalResult.value : null;
  state.contextError =
    state.context === null
      ? "기존 타임라인 문맥을 불러오지 못했습니다."
      : null;
}

async function loadState({ preserveSelection = false } = {}) {
  if (state.loading) return;
  await beginLoading();
  const previousId = preserveSelection ? state.selectedId : null;
  try {
    const [clinicalResult, casesResult] = await Promise.allSettled([
      fetchJson("/api/clinical-review"),
      fetchJson("/api/cases", { optional: true }),
    ]);
    if (clinicalResult.status !== "fulfilled") {
      throw clinicalResult.reason;
    }
    const payload = clinicalResult.value;
    if (!isObject(payload) || payload.ok === false) {
      throw new Error(
        payload?.error || "임상 검수 응답 형식이 올바르지 않습니다.",
      );
    }
    const requestedCase = requestedCaseFromUrl();
    if (
      requestedCase &&
      payload.case_id &&
      String(payload.case_id) !== requestedCase
    ) {
      throw new Error("요청한 case와 임상 검수 응답의 case가 다릅니다.");
    }
    state.clinical = payload;
    const casesPayload =
      casesResult.status === "fulfilled" ? casesResult.value : null;
    state.cases = asArray(casesPayload?.cases || payload.available_cases);
    state.viewMode = requestedModeFromUrl();
    await loadContextPointers(payload);
    state.items = buildItems();
    state.selectedId = null;
    state.draft = null;
    state.baseline = null;
    renderAll();
    configureMedia();

    const selection =
      state.items.find((item) => item.id === previousId) ||
      state.items.find((item) => item.status === "unreviewed") ||
      state.items[0];
    if (selection) {
      selectItem(selection.id, { bypassDirty: true });
    } else {
      $("#video-loading").hidden = true;
      $("#video-empty").hidden = false;
    }
  } catch (error) {
    $("#candidate-list-error").hidden = false;
    $("#candidate-list-error-detail").textContent = error.message;
    $("#candidate-list-empty").hidden = true;
    $("#candidate-list").hidden = true;
    setVideoFailure(error.message);
  } finally {
    await finishLoading();
  }
}

function installEventHandlers() {
  $("#case-selector").addEventListener("change", (event) => {
    const nextCase = event.target.value;
    if (!guardNavigation()) {
      event.target.value = activeCaseId();
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.set("case", nextCase);
    window.location.assign(url);
  });

  $$("[data-review-mode]").forEach((button) => {
    button.addEventListener("click", () => switchMode(button.dataset.reviewMode));
  });

  $$("[data-video-view]").forEach((button) => {
    button.addEventListener("click", () => configureView(button.dataset.videoView));
  });

  $("#reload-state").addEventListener("click", () => {
    if (guardNavigation()) loadState();
  });
  $("#empty-reload").addEventListener("click", () => {
    if (guardNavigation()) loadState();
  });
  $("#retry-video").addEventListener("click", configureMedia);
  $("#play-toggle").addEventListener("click", togglePlayback);
  $("#step-back").addEventListener("click", () =>
    seekToTime(state.currentTimeSec - 1),
  );
  $("#step-forward").addEventListener("click", () =>
    seekToTime(state.currentTimeSec + 1),
  );
  $("#playback-rate").addEventListener("change", () => {
    const rate = Number($("#playback-rate").value);
    video.playbackRate = rate;
    companionVideo.playbackRate = rate;
  });
  $("#seek-evidence-start").addEventListener("click", seekToEvidenceStart);
  $("#previous-candidate").addEventListener("click", () => navigateItem(-1));
  $("#next-candidate").addEventListener("click", () => navigateItem(1));
  $("#discard-draft").addEventListener("click", discardDraft);
  $("#confirm").addEventListener("click", () => saveReview("confirmed"));
  $("#ambiguous").addEventListener("click", () => saveReview("ambiguous"));
  $("#reject").addEventListener("click", () => saveReview("rejected"));
  $("#reviewer-id").addEventListener("input", () => {
    if ($("#reviewer-id").value.trim()) {
      $("#reviewer-id").removeAttribute("aria-invalid");
      $("#form-error").hidden = true;
    }
  });
  $("#reviewer-id").addEventListener("change", () => {
    localStorage.setItem(
      "surgery-clinical-reviewer-id",
      $("#reviewer-id").value.trim(),
    );
  });
  $("#reviewer-role").addEventListener("change", () => {
    localStorage.setItem(
      "surgery-clinical-reviewer-role",
      $("#reviewer-role").value,
    );
  });

  $("#clinical-form")
    .querySelectorAll("input, select, textarea")
    .forEach((control) => {
      const handleDraftInput = () => {
        control.removeAttribute("aria-invalid");
        $("#form-error").hidden = true;
        syncDraftFromForm();
      };
      control.addEventListener("input", handleDraftInput);
      control.addEventListener("change", handleDraftInput);
    });

  video.addEventListener("loadedmetadata", () => {
    state.videoReady = true;
    state.mediaConfigured = true;
    if (
      state.mediaMode === "legacy-composite" &&
      video.videoWidth > 0 &&
      video.videoHeight > 0 &&
      video.videoWidth / video.videoHeight < 2.7
    ) {
      state.mediaMode = "single-flir";
      $("#video-shell").dataset.mediaMode = "single-flir";
      $("#composite-view").disabled = true;
      $("#composite-view").title =
        "이 case에는 별도 CAM4 합성 소스가 연결되어 있지 않습니다";
      configureView("flir");
    }
    video.playbackRate = Number($("#playback-rate").value);
    $("#video-loading").hidden = true;
    $("#video-error").hidden = true;
    $("#video-empty").hidden = false;
    $("#play-toggle").disabled = false;
    if (selectedItem()) {
      $("#video-empty").hidden = true;
      seekToEvidenceStart();
    }
  });

  companionVideo.addEventListener("loadedmetadata", () => {
    state.companionReady = true;
    syncCompanion({ force: true, play: !video.paused });
  });

  video.addEventListener("error", () => {
    if (!video.getAttribute("src")) return;
    setVideoFailure(
      "미디어를 불러오지 못했습니다. case의 검토 프록시를 확인해 주세요.",
    );
  });

  companionVideo.addEventListener("error", () => {
    state.companionReady = false;
    if (state.videoView === "composite") {
      toast("CAM4 비교 영상을 불러오지 못해 FLIR만 표시합니다.");
      configureView("flir");
    }
  });

  video.addEventListener("timeupdate", () => {
    const bounds = selectedEvidenceBounds();
    const canonicalVisualEnd = visualEndSec();
    if (video.currentTime > canonicalVisualEnd + 0.001) {
      pausePlayback();
      video.currentTime = canonicalVisualEnd;
      state.currentTimeSec = canonicalVisualEnd;
      syncCompanion({ force: true });
    } else if (
      bounds &&
      $("#loop-evidence").checked &&
      video.currentTime >= bounds.end - 0.025 &&
      bounds.end > bounds.start + 0.025
    ) {
      video.currentTime = bounds.start;
      state.currentTimeSec = bounds.start;
      syncCompanion({ force: true, play: !video.paused });
    } else {
      state.currentTimeSec = video.currentTime;
      syncCompanion();
    }
    updatePlaybackReadout();
  });

  video.addEventListener("play", () => {
    $("#play-toggle").textContent = "Ⅱ";
    $("#play-toggle").setAttribute("aria-label", "일시정지");
    syncCompanion({ force: true, play: true });
  });

  video.addEventListener("pause", () => {
    $("#play-toggle").textContent = "▶";
    $("#play-toggle").setAttribute("aria-label", "재생");
    companionVideo.pause();
  });

  video.addEventListener("ended", () => {
    const bounds = selectedEvidenceBounds();
    if ($("#loop-evidence").checked && bounds && bounds.end > bounds.start) {
      seekToTime(bounds.start);
      togglePlayback();
    }
  });

  $("#timeline").addEventListener("click", (event) => {
    if (event.target.closest(".timeline-marker")) return;
    const canvas = event.target.closest(".timeline-canvas, .timeline-ruler");
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const fraction = clamp((event.clientX - rect.left) / rect.width, 0, 1);
    seekToTime(
      timelineStart() + fraction * (timelineEnd() - timelineStart()),
    );
  });

  window.addEventListener("resize", updateTimelinePlayhead);
  window.addEventListener("beforeunload", (event) => {
    if (!hasDirtyDraft() && !state.saving) return;
    event.preventDefault();
    event.returnValue = "";
  });

  document.addEventListener("keydown", (event) => {
    const active = document.activeElement;
    const tag = active?.tagName || "";
    const inputType = tag === "INPUT" ? active.type : "";
    const editing =
      tag === "TEXTAREA" ||
      tag === "SELECT" ||
      (tag === "INPUT" &&
        !["checkbox", "radio", "button"].includes(inputType)) ||
      active?.isContentEditable;
    if (editing) return;
    if (
      event.code === "Space" &&
      (tag === "BUTTON" ||
        (tag === "INPUT" && ["checkbox", "radio"].includes(inputType)))
    ) {
      return;
    }
    if (
      (event.key === "Home" || event.key === "End") &&
      (active === $("#timeline") || active === $("#timeline-playhead"))
    ) {
      event.preventDefault();
      seekToTime(event.key === "Home" ? timelineStart() : timelineEnd());
      return;
    }
    if (event.code === "Space") {
      event.preventDefault();
      togglePlayback();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      seekToTime(state.currentTimeSec - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      seekToTime(state.currentTimeSec + 1);
    } else if (event.key.toLowerCase() === "p") {
      event.preventDefault();
      navigateItem(-1);
    } else if (event.key.toLowerCase() === "n") {
      event.preventDefault();
      navigateItem(1);
    } else if (event.key.toLowerCase() === "v") {
      event.preventDefault();
      configureView(state.videoView === "flir" ? "composite" : "flir");
    } else if (event.key.toLowerCase() === "l") {
      event.preventDefault();
      $("#loop-evidence").checked = !$("#loop-evidence").checked;
      toast(
        $("#loop-evidence").checked
          ? "근거 구간 반복을 켰습니다."
          : "근거 구간 반복을 껐습니다.",
      );
    } else if (event.key === "Enter" && state.viewMode === "draft") {
      event.preventDefault();
      saveReview("confirmed");
    } else if (
      event.key.toLowerCase() === "a" &&
      state.viewMode === "draft"
    ) {
      event.preventDefault();
      saveReview("ambiguous");
    } else if (
      event.key.toLowerCase() === "r" &&
      event.shiftKey &&
      state.viewMode === "draft"
    ) {
      event.preventDefault();
      saveReview("rejected");
    } else if (event.key === "Escape" && hasDirtyDraft()) {
      event.preventDefault();
      discardDraft();
    }
  });
}

const savedReviewer = localStorage.getItem("surgery-clinical-reviewer-id");
if (savedReviewer) $("#reviewer-id").value = savedReviewer;
const savedReviewerRole = localStorage.getItem("surgery-clinical-reviewer-role");
if (Object.hasOwn(REVIEWER_ROLE_LABELS, savedReviewerRole)) {
  $("#reviewer-role").value = savedReviewerRole;
}
state.viewMode = requestedModeFromUrl();
installEventHandlers();
loadState().catch((error) => {
  $("#candidate-list-error").hidden = false;
  $("#candidate-list-error-detail").textContent = error.message;
  setVideoFailure(error.message);
});
