"use strict";

const state = {
  data: null,
  selected: null,
  draft: null,
  selectionBaseline: null,
  currentFrame: 0,
  currentTimeSec: 0,
  videoReady: false,
  fallbackMode: false,
  readyVideoViews: new Set(),
  fallbackVideoViews: new Set(),
  mediaGeneration: 0,
  pendingSeekFrameSelection: null,
  mediaAwaySince: null,
  mediaRecoveryTimer: null,
  mediaRecoveryGeneration: null,
  mediaWasPlayingBeforeAway: false,
  playing: false,
  playbackFrame: null,
  lastPlaybackTime: 0,
  saving: false,
  frameSequence: 0,
  toastTimer: null,
  rejectTimer: null,
  rejectArmedFor: null,
  withdrawTimer: null,
  withdrawArmedFor: null,
  pixelsPerSecond: 12,
  fitTimeline: true,
  followPlayhead: true,
  markerDrag: null,
  suppressMarkerClick: false,
  playheadDrag: false,
  focusModality: "keyboard",
  finalReview: null,
  phaseCatalog: null,
  phaseCatalogLoadSequence: 0,
  viewMode: "edit",
  activeInspector: "interaction",
  clinical: {
    data: null,
    items: [],
    selectedId: null,
    draft: null,
    baseline: null,
    reviewNotes: "",
    viewMode: "draft",
    saving: false,
    loading: false,
    loadError: null,
  },
  overlayEnabled: true,
  overlayFingerprint: null,
  overlayKeys: new Set(),
  pointOverlayExpiry: new Map(),
  overlayTimer: null,
  recognition: {
    data: null,
    enabled: false,
    loadSequence: 0,
    renderFingerprint: null,
    resizeObserver: null,
  },
  filters: {
    request: true,
    transfer: true,
    phase: true,
    speech: true,
    clinical: true,
  },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const REVIEW_VIDEO_VIEWS = ["cam4", "flir", "cam2", "cam3"];
const RECOGNITION_VIDEO_VIEWS = ["cam4", "flir"];
const FOLLOWER_VIDEO_VIEWS = REVIEW_VIDEO_VIEWS.filter((view) => view !== "cam4");
const videos = Object.fromEntries(
  REVIEW_VIDEO_VIEWS.map((view) => [view, $(`#${view}-video`)]),
);
const recognitionCanvases = Object.fromEntries(
  RECOGNITION_VIDEO_VIEWS.map((view) => [
    view,
    $(`#${view}-recognition-overlay`),
  ]),
);
const video = videos.cam4;
const DEFAULT_VIDEO_LOADING_MESSAGE =
  "독립 4-view 검토 영상을 준비하고 있습니다";
const MEDIA_RECOVERY_MIN_AWAY_MS = 1000;
const MEDIA_RECOVERY_DEBOUNCE_MS = 160;
const PHASE_BOUNDARY_TIME_EPSILON_SEC = 1e-6;
const SVG_NAMESPACE = "http://www.w3.org/2000/svg";

function createTimelineSemanticIcon(iconName) {
  const icon = document.createElementNS(SVG_NAMESPACE, "svg");
  icon.classList.add("timeline-semantic-icon");
  icon.dataset.icon = iconName;
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("fill", "none");
  icon.setAttribute("stroke", "currentColor");
  icon.setAttribute("stroke-width", "2");
  icon.setAttribute("stroke-linecap", "round");
  icon.setAttribute("stroke-linejoin", "round");
  icon.setAttribute("aria-hidden", "true");
  icon.setAttribute("focusable", "false");

  const addPath = (pathData) => {
    const path = document.createElementNS(SVG_NAMESPACE, "path");
    path.setAttribute("d", pathData);
    icon.append(path);
  };

  if (iconName === "speaker") {
    addPath("M11 5 6 9H2v6h4l5 4V5Z");
    addPath("M15.54 8.46a5 5 0 0 1 0 7.07");
    addPath("M19.07 4.93a10 10 0 0 1 0 14.14");
  } else if (iconName === "stethoscope") {
    addPath("M11 2v2");
    addPath("M5 2v2");
    addPath("M5 3H4a2 2 0 0 0-2 2v3a6 6 0 0 0 12 0V5a2 2 0 0 0-2-2h-1");
    addPath("M8 14a6 6 0 0 0 12 0v-2");
    const chestPiece = document.createElementNS(SVG_NAMESPACE, "circle");
    chestPiece.setAttribute("cx", "20");
    chestPiece.setAttribute("cy", "10");
    chestPiece.setAttribute("r", "2");
    icon.append(chestPiece);
  }

  return icon;
}

const EVENT_LABELS = {
  implicit_tool_request: "암묵적 요청",
  tool_transfer: "도구 이동",
  phase_start: "수술 단계",
};

const TRACK_FOR_EVENT = {
  implicit_tool_request: "request",
  tool_transfer: "transfer",
  phase_start: "phase",
};

const REVIEW_MODES = new Set(["edit", "final_observed", "final_dt"]);
const FINAL_LAYER_FOR_MODE = {
  final_observed: "observed",
  final_dt: "dt_reference",
};
const LOCATION_LABELS = {
  mayo_stand: "메이요",
  scrub_nurse: "스크럽",
  surgeon: "집도의",
  operative_person_role_unresolved: "수술측 인력(정확 역할 미확정)",
};
const TOOL_LABELS = {
  retractor_bundle_unresolved: "리트랙터 묶음(종류·수량 미확정)",
};

const CLINICAL_STATUS_LABELS = {
  unreviewed: "미검토",
  proposed: "미검토",
  confirmed: "확정",
  ambiguous: "애매",
  rejected: "기각",
};
const CLINICAL_STATUS_SYMBOLS = {
  unreviewed: "○",
  proposed: "○",
  confirmed: "✓",
  ambiguous: "?",
  rejected: "×",
};
const CLINICAL_REVIEWER_ROLE_LABELS = {
  clinical_reviewer: "임상 검토자",
  clinician: "임상의",
  surgeon: "집도의",
};
const CLINICAL_TEXT_MAX_LENGTH = 600;
const CLINICAL_MAX_SENTENCES = 2;

function requestedCaseFromUrl() {
  const value = new URLSearchParams(window.location.search).get("case");
  return value ? value.trim() : "";
}

function removeLegacyWorkspaceParameters() {
  const url = new URL(window.location.href);
  const legacyKeys = ["workspace", "layer", "review_mode", "clinical_mode"];
  const changed = legacyKeys.some((key) => url.searchParams.has(key));
  legacyKeys.forEach((key) => url.searchParams.delete(key));
  if (changed) window.history.replaceState({}, "", url);
}

function apiUrl(path, parameters = {}) {
  const url = new URL(path, window.location.origin);
  const caseId =
    requestedCaseFromUrl() || state.data?.active_case_id || state.data?.case_id;
  if (caseId && !url.searchParams.has("case")) {
    url.searchParams.set("case", caseId);
  }
  Object.entries(parameters).forEach(([key, value]) => {
    if (value === undefined || value === null) return;
    url.searchParams.set(key, String(value));
  });
  return `${url.pathname}${url.search}`;
}

function recognitionStorageKey(caseId) {
  return `surgery-review-rfdetr-overlay:${caseId}`;
}

function clearRecognitionCanvas(view, { hide = true } = {}) {
  const canvas = recognitionCanvases[view];
  if (!canvas) return;
  const context = canvas.getContext("2d");
  if (context) context.clearRect(0, 0, canvas.width, canvas.height);
  canvas.hidden = hide;
  const tile = videoTile(view);
  if (tile) {
    tile.dataset.recognitionActive = "false";
    tile.dataset.recognitionCount = "0";
  }
}

function clearRecognitionOverlays({ hide = true } = {}) {
  RECOGNITION_VIDEO_VIEWS.forEach((view) =>
    clearRecognitionCanvas(view, { hide }),
  );
  state.recognition.renderFingerprint = null;
}

function resetRecognitionOverlayControl() {
  const control = $("#recognition-overlay-control");
  const checkbox = $("#recognition-overlay-enabled");
  state.recognition.data = null;
  state.recognition.enabled = false;
  checkbox.checked = false;
  checkbox.disabled = true;
  control.hidden = true;
  clearRecognitionOverlays();
}

function validRecognitionView(view, frameCount) {
  return (
    view &&
    Number.isInteger(view.source_width) &&
    view.source_width > 0 &&
    Number.isInteger(view.source_height) &&
    view.source_height > 0 &&
    view.frame_count === frameCount &&
    Array.isArray(view.frames) &&
    view.frames.length === frameCount &&
    Number.isInteger(view.continuous_proxy?.width) &&
    Number.isInteger(view.continuous_proxy?.height) &&
    Array.isArray(view.continuous_proxy?.content_rect) &&
    view.continuous_proxy.content_rect.length === 4
  );
}

function validRecognitionPayload(payload, caseId) {
  const frameCount = state.data?.frame_count;
  return (
    payload?.schema === "taskplanner.rfdetr_overlay_bundle.v1" &&
    payload.case_id === caseId &&
    payload.authority === "ai_inference_reference_not_ground_truth" &&
    payload.read_only === true &&
    payload.frame_index_mapping ===
      "source_frame_idx_one_to_one_zero_based" &&
    payload.frame_count === frameCount &&
    RECOGNITION_VIDEO_VIEWS.every((view) =>
      validRecognitionView(payload.views?.[view], frameCount),
    )
  );
}

async function loadRecognitionOverlay(caseId) {
  const sequence = ++state.recognition.loadSequence;
  resetRecognitionOverlayControl();
  if (!/^[A-Za-z0-9_-]+$/.test(caseId || "")) return;
  try {
    const response = await fetch(
      `/rfdetr_overlays/${encodeURIComponent(caseId)}.json`,
      { cache: "no-store" },
    );
    if (!response.ok) return;
    const payload = await response.json();
    if (
      sequence !== state.recognition.loadSequence ||
      !validRecognitionPayload(payload, caseId)
    ) {
      return;
    }
    state.recognition.data = payload;
    const saved = localStorage.getItem(recognitionStorageKey(caseId));
    state.recognition.enabled = saved === "true";
    const checkbox = $("#recognition-overlay-enabled");
    const control = $("#recognition-overlay-control");
    checkbox.checked = state.recognition.enabled;
    checkbox.disabled = false;
    control.hidden = false;
    const cam4Count = Number(
      payload.views.cam4.instance_count || 0,
    ).toLocaleString("ko-KR");
    const flirCount = Number(
      payload.views.flir.instance_count || 0,
    ).toLocaleString("ko-KR");
    control.title =
      `CAM4 ${cam4Count}건 · FLIR ${flirCount}건의 RF-DETR 인식 결과. ` +
      "AI 추론 참고용이며 사람 검수 정답이 아닙니다.";
    renderRecognitionOverlays({ force: true });
  } catch {
    if (sequence === state.recognition.loadSequence) {
      resetRecognitionOverlayControl();
    }
  }
}

function renderCaseSelector(payload) {
  const selector = $("#case-selector");
  const cases = Array.isArray(payload?.available_cases)
    ? payload.available_cases
    : [
        {
          case_id: payload?.case_id,
          label: payload?.case_id,
          media_available: Boolean(payload?.media?.available),
        },
      ];
  selector.replaceChildren();
  cases
    .filter((entry) => entry?.case_id)
    .forEach((entry) => {
      const option = document.createElement("option");
      option.value = String(entry.case_id);
      option.textContent =
        String(entry.label || entry.case_id) +
        (entry.media_available ? "" : " · 프레임 전용");
      selector.append(option);
    });
  const activeCaseId = String(
    payload?.active_case_id || payload?.case_id || "",
  );
  if (activeCaseId) selector.value = activeCaseId;
  selector.disabled =
    isAnySaving() ||
    !payload?.case_selector_enabled ||
    selector.options.length < 2;
  selector.title =
    selector.options.length > 1
      ? "검수할 수술 영상을 선택합니다"
      : "현재 서버에는 한 영상만 등록되어 있습니다";
}

function switchCase(nextCaseId) {
  const selector = $("#case-selector");
  const currentCaseId = String(
    state.data?.active_case_id || state.data?.case_id || "",
  );
  if (!nextCaseId || nextCaseId === currentCaseId) return;
  if (!guardAnyNavigation()) {
    selector.value = currentCaseId;
    return;
  }
  pausePlayback();
  selector.disabled = true;
  const url = new URL(window.location.href);
  url.searchParams.set("case", nextCaseId);
  window.location.assign(url);
}

function isFinalMode() {
  return state.viewMode !== "edit";
}

function finalLayer() {
  const layerName = FINAL_LAYER_FOR_MODE[state.viewMode];
  return layerName ? state.finalReview?.layers?.[layerName] || null : null;
}

function finalInteractionItems() {
  const events = finalLayer()?.events;
  return Array.isArray(events) ? events : [];
}

function speechContextTrack() {
  const track = state.finalReview?.context_tracks?.speech || null;
  return track?.available ? track : null;
}

function phaseContextTrack() {
  const track = state.finalReview?.context_tracks?.phase || null;
  return track?.available ? track : null;
}

function phaseCatalogEntries() {
  const catalog = state.phaseCatalog;
  if (!catalog || !Array.isArray(catalog.phases)) return [];
  const byId = new Map(
    catalog.phases
      .filter((phase) => phase && typeof phase.phase_id === "string")
      .map((phase) => [phase.phase_id, phase]),
  );
  const ordered = Array.isArray(catalog.phase_order)
    ? catalog.phase_order.map((phaseId) => byId.get(phaseId)).filter(Boolean)
    : [];
  const included = new Set(ordered.map((phase) => phase.phase_id));
  return [
    ...ordered,
    ...catalog.phases.filter(
      (phase) => phase && !included.has(phase.phase_id),
    ),
  ];
}

function phaseCatalogEntry(phaseId) {
  return (
    phaseCatalogEntries().find((phase) => phase.phase_id === phaseId) || null
  );
}

function phaseDisplayName(phaseId) {
  const phase = phaseCatalogEntry(phaseId);
  return String(phase?.name_ko || phase?.name || "").trim();
}

function phaseDisplayLabel(phaseId) {
  const id = String(phaseId || "수술 단계");
  const name = phaseDisplayName(id);
  return name ? `${id} · ${name}` : id;
}

function activePhaseTimelineEntries() {
  return sourceItems()
    .map((candidate, index) => {
      const key = candidateKey(candidate, index);
      const selected =
        state.activeInspector === "interaction" &&
        ["candidate", "human", "final"].includes(state.selected?.kind) &&
        state.selected.id === key;
      const fields =
        selected && state.draft ? state.draft : fieldsForCandidate(candidate);
      return { candidate, fields };
    })
    .filter(
      ({ candidate, fields }) =>
        fields.event_type === "phase_start" &&
        reviewStatus(candidate) !== "rejected",
    )
    .sort(
      (left, right) =>
        left.fields.source_frame_idx - right.fields.source_frame_idx,
    );
}

function currentPhaseTimelineEntry(entries = activePhaseTimelineEntries()) {
  return (
    entries
      .filter(
        ({ fields }) =>
          timeForFrame(fields.source_frame_idx) <=
          state.currentTimeSec + PHASE_BOUNDARY_TIME_EPSILON_SEC,
      )
      .at(-1) || null
  );
}

function reviewAttentionTrack() {
  const track =
    state.finalReview?.context_tracks?.review_attention || null;
  return track?.available ? track : null;
}

function speechEvents() {
  const events = speechContextTrack()?.events;
  return Array.isArray(events) ? events : [];
}

function phaseContextEvents() {
  const events = phaseContextTrack()?.events;
  return Array.isArray(events) ? events : [];
}

function reviewAttentionEvents() {
  const events = reviewAttentionTrack()?.events;
  return Array.isArray(events) ? events : [];
}

function speechEventKey(event, fallbackIndex = 0) {
  return String(event?.event_id || `speech-context-${fallbackIndex}`);
}

function speechCompletionSec(event) {
  const startSec = Number(event?.time_sec);
  const endSec = Number(event?.end_sec);
  if (Number.isFinite(endSec)) {
    return Number.isFinite(startSec) ? Math.max(startSec, endSec) : endSec;
  }
  return Number.isFinite(startSec) ? startSec : timelineStart();
}

function nearestSpeechEventToTime(timeSec) {
  const target = Number(timeSec);
  if (!Number.isFinite(target)) return null;
  return (
    speechEvents()
      .map((event, index) => ({
        event,
        index,
        distance: Math.abs(speechCompletionSec(event) - target),
      }))
      .sort(
        (left, right) =>
          left.distance - right.distance || left.index - right.index,
      )[0]?.event || null
  );
}

function selectedSpeechEvent() {
  if (state.selected?.kind !== "speech") return null;
  return (
    speechEvents().find(
      (event, index) => speechEventKey(event, index) === state.selected.id,
    ) || null
  );
}

function finalMeta(candidate) {
  return candidate?._final_review || {};
}

function humanizeIdentifier(value) {
  if (!value) return "—";
  if (LOCATION_LABELS[value]) return LOCATION_LABELS[value];
  if (TOOL_LABELS[value]) return TOOL_LABELS[value];
  return String(value).replaceAll("_", " ");
}

function clinicalIsObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function clinicalClone(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function clinicalAsArray(value) {
  if (Array.isArray(value)) return value;
  if (value === null || value === undefined) return [];
  if (clinicalIsObject(value)) {
    for (const key of ["annotations", "records", "items", "events", "actions"]) {
      if (Array.isArray(value[key])) return value[key];
    }
  }
  return [];
}

function clinicalFiniteNumber(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric;
  }
  return null;
}

function normalizeClinicalStatus(value) {
  const status = String(value || "").toLowerCase();
  return ["confirmed", "ambiguous", "rejected"].includes(status)
    ? status
    : "unreviewed";
}

function clinicalStatusLabel(value) {
  return CLINICAL_STATUS_LABELS[normalizeClinicalStatus(value)] || "미검토";
}

function clinicalStatusSymbol(value) {
  return CLINICAL_STATUS_SYMBOLS[normalizeClinicalStatus(value)] || "○";
}

function clinicalAnnotationId(value, fallbackIndex = 0) {
  return String(
    value?.annotation_id ||
      value?._clinical_review?.annotation_id ||
      value?._review_ui?.annotation_id ||
      value?.candidate_id ||
      value?.id ||
      `clinical-${fallbackIndex}`,
  );
}

function clinicalAnchorSec(value) {
  return (
    clinicalFiniteNumber(
      value?.anchor_sec,
      value?.time_sec,
      value?.evidence_start_sec,
    ) || 0
  );
}

function clinicalEvidenceStartSec(value) {
  return (
    clinicalFiniteNumber(
      value?.evidence_start_sec,
      value?.anchor_sec,
      value?.time_sec,
    ) || 0
  );
}

function clinicalEvidenceEndSec(value) {
  const start = clinicalEvidenceStartSec(value);
  const end = clinicalFiniteNumber(
    value?.evidence_end_sec,
    value?.anchor_sec,
    value?.time_sec,
  );
  return Math.max(start, end === null ? start : end);
}

function clinicalAnchorFrameIndex(value) {
  const frame = clinicalFiniteNumber(
    value?.anchor_source_frame_idx,
    value?.anchor_frame_idx,
  );
  if (frame === null) return null;
  const index = Math.trunc(frame);
  const frameCount = Number(state.data?.frame_count || 0);
  return index >= 0 && index < frameCount ? index : null;
}

function seekToClinicalAnchor(value) {
  const frame = clinicalAnchorFrameIndex(value);
  if (frame !== null) {
    seekToFrame(frame);
    return;
  }
  seekToTime(clinicalAnchorSec(value), { frameSelection: "nearest" });
}

function formatClinicalRange(start, end) {
  return `${formatTime(start)}–${formatTime(end)}`;
}

function clinicalSentenceCount(value) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return 0;
  const terminated = text.match(/[.!?。！？]+(?=\s|$)/g)?.length || 0;
  return terminated + (/[.!?。！？]\s*$/.test(text) ? 0 : 1);
}

function clinicalConfidenceText(annotation, key) {
  const confidence = clinicalIsObject(annotation?.confidence)
    ? annotation.confidence[key]
    : null;
  if (confidence === null || confidence === undefined || confidence === "") {
    return "—";
  }
  return typeof confidence === "number"
    ? confidence.toFixed(2).replace(/0+$/, "").replace(/\.$/, "")
    : String(confidence);
}

function updateClinicalFieldCount(field) {
  const input = $(`#clinical-${field}`);
  const output = $(`#clinical-${field}-count`);
  if (!input || !output) return;
  const sentenceCount = clinicalSentenceCount(input.value);
  output.textContent =
    `${sentenceCount}/${CLINICAL_MAX_SENTENCES}문장 · ` +
    `${input.value.length}/${CLINICAL_TEXT_MAX_LENGTH}자`;
  output.dataset.valid = String(
    sentenceCount >= 1 &&
      sentenceCount <= CLINICAL_MAX_SENTENCES &&
      input.value.length <= CLINICAL_TEXT_MAX_LENGTH,
  );
}

function clinicalCandidateDigest(candidate) {
  return String(
    candidate?._clinical_review?.candidate_sha256 ||
      candidate?._review_ui?.candidate_sha256 ||
      candidate?.candidate_sha256 ||
      candidate?.sha256 ||
      "",
  );
}

function clinicalReviewPayload(value) {
  if (!clinicalIsObject(value)) return null;
  if (clinicalIsObject(value.clinical_review)) return value.clinical_review;
  if (clinicalIsObject(value.review)) return value.review;
  return value;
}

function effectiveClinicalReviewMap() {
  const result = new Map();
  const visit = (value, keyHint = "") => {
    if (Array.isArray(value)) {
      value.forEach((item) => visit(item));
      return;
    }
    if (!clinicalIsObject(value)) return;
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
  visit(state.clinical.data?.effective_reviews);
  return result;
}

function clinicalReferenceRecords() {
  const reference = state.clinical.data?.reference;
  if (Array.isArray(reference)) return reference;
  if (!clinicalIsObject(reference)) return [];
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
        clinicalIsObject(value) &&
        Boolean(
          value.annotation_id ||
            value.annotation?.annotation_id ||
            value.adjudicated_annotation?.annotation_id,
        ),
    )
    .map(([, value]) => value);
}

function buildClinicalItems() {
  if (!state.clinical.data) return [];
  if (state.clinical.viewMode === "final") {
    return clinicalReferenceRecords()
      .map((record, index) => {
        const annotation =
          record?.adjudicated_annotation ||
          record?.annotation ||
          record?.record ||
          record;
        if (!clinicalIsObject(annotation)) return null;
        const review = clinicalReviewPayload(record);
        return {
          id: clinicalAnnotationId(annotation, index),
          source: annotation,
          annotation,
          review,
          status: normalizeClinicalStatus(
            review?.review_status ||
              record?.review_status ||
              annotation?.review_status ||
              "confirmed",
          ),
          digest: clinicalCandidateDigest(annotation),
          final: true,
        };
      })
      .filter(Boolean)
      .sort(
        (left, right) =>
          clinicalAnchorSec(left.annotation) -
          clinicalAnchorSec(right.annotation),
      );
  }

  const reviews = effectiveClinicalReviewMap();
  return clinicalAsArray(state.clinical.data.candidates)
    .map((candidate, index) => {
      if (!clinicalIsObject(candidate)) return null;
      const id = clinicalAnnotationId(candidate, index);
      const review = reviews.get(id) || null;
      const annotation =
        review?.adjudicated_annotation || review?.annotation || candidate;
      return {
        id,
        source: candidate,
        annotation,
        review,
        status: normalizeClinicalStatus(
          review?.review_status || candidate.review_status,
        ),
        digest: clinicalCandidateDigest(candidate),
        final: false,
      };
    })
    .filter(Boolean)
    .sort(
      (left, right) =>
        clinicalAnchorSec(left.annotation) -
        clinicalAnchorSec(right.annotation),
    );
}

function selectedClinicalItem() {
  return (
    state.clinical.items.find(
      (item) => item.id === state.clinical.selectedId,
    ) || null
  );
}

function clinicalItemTitle(item) {
  return String(item?.annotation?.observation || "").trim() || "임상 라벨";
}

function clinicalSnapshotSignature(
  annotation = state.clinical.draft,
  notes = state.clinical.reviewNotes,
) {
  return JSON.stringify({ annotation, notes: String(notes || "") });
}

function hasDirtyClinicalDraft() {
  return (
    state.clinical.viewMode === "draft" &&
    Boolean(state.clinical.draft && state.clinical.baseline) &&
    clinicalSnapshotSignature() !== state.clinical.baseline
  );
}

function isAnySaving() {
  return state.saving || state.clinical.saving;
}

function hasUnsavedWork() {
  return hasDirtyDraft() || hasDirtyClinicalDraft();
}

function guardClinicalNavigation() {
  if (state.clinical.saving) {
    toast("임상 판정을 저장하고 있습니다. 저장이 끝난 뒤 이동해 주세요.");
    return false;
  }
  if (!hasDirtyClinicalDraft()) return true;
  toast("임상 필드 변경이 저장되지 않았습니다. 저장하거나 변경 취소 후 이동하세요.");
  return false;
}

function guardAnyNavigation() {
  if (isAnySaving()) {
    toast("판정을 저장하고 있습니다. 저장이 끝난 뒤 이동해 주세요.");
    return false;
  }
  if (hasDirtyDraft()) {
    toast("현재 이벤트 수정이 저장되지 않았습니다. 확정하거나 변경 취소 후 이동하세요.");
    return false;
  }
  if (hasDirtyClinicalDraft()) {
    toast("임상 필드 변경이 저장되지 않았습니다. 저장하거나 변경 취소 후 이동하세요.");
    return false;
  }
  return true;
}

function clinicalStatusCounts() {
  const counts = {
    confirmed: 0,
    ambiguous: 0,
    rejected: 0,
    unreviewed: 0,
  };
  state.clinical.items.forEach((item) => {
    counts[normalizeClinicalStatus(item.status)] += 1;
  });
  return counts;
}

function clinicalReviewSummary() {
  const counts = clinicalStatusCounts();
  const total = state.clinical.items.length;
  const reviewed = counts.confirmed + counts.ambiguous + counts.rejected;
  return {
    ...counts,
    total,
    reviewed,
    remaining: counts.unreviewed,
  };
}

function clinicalNavigatorListItem(item) {
  const displayItem =
    state.activeInspector === "clinical" &&
    item.id === state.clinical.selectedId &&
    state.clinical.draft
      ? { ...item, annotation: state.clinical.draft }
      : item;
  const annotation = displayItem.annotation;
  const li = document.createElement("li");
  li.dataset.navigatorScope = "clinical";
  li.dataset.navigatorTimeSec = String(clinicalAnchorSec(annotation));
  const button = document.createElement("button");
  button.type = "button";
  button.className = "event-list-item clinical-candidate-list-item";
  button.dataset.clinicalAnnotationId = item.id;
  button.setAttribute(
    "aria-current",
    String(
      state.activeInspector === "clinical" &&
        item.id === state.clinical.selectedId,
    ),
  );
  button.setAttribute(
    "aria-label",
    `${formatTime(clinicalAnchorSec(annotation))} 임상, ` +
      `${clinicalItemTitle(displayItem)}, ${clinicalStatusLabel(item.status)}`,
  );

  const icon = document.createElement("span");
  icon.className = "clinical-candidate-kind";
  icon.setAttribute("aria-hidden", "true");
  icon.append(createTimelineSemanticIcon("stethoscope"));

  const copy = document.createElement("span");
  copy.className = "event-copy";
  const title = document.createElement("strong");
  title.textContent = clinicalItemTitle(displayItem);
  const meta = document.createElement("small");
  meta.textContent =
    `임상 · ${formatClinicalRange(
      clinicalEvidenceStartSec(annotation),
      clinicalEvidenceEndSec(annotation),
    )}`;
  copy.append(title, meta);

  const status = document.createElement("span");
  status.className = `candidate-status ${normalizeClinicalStatus(
    item.status,
  )}`;
  status.textContent = clinicalStatusSymbol(item.status);
  status.title = clinicalStatusLabel(item.status);
  status.setAttribute("aria-label", clinicalStatusLabel(item.status));
  button.append(icon, copy, status);
  button.addEventListener("click", () => selectClinicalItem(item.id));
  li.append(button);
  return li;
}

function clinicalSetText(selector, value, fallback = "—") {
  const node = $(selector);
  if (!node) return;
  node.textContent =
    value === null || value === undefined || value === ""
      ? fallback
      : String(value);
}

function clinicalHistoryForItem(item) {
  const raw = state.clinical.data?.action_history;
  const itemReview = clinicalReviewPayload(item.review);
  let actions = [];
  if (Array.isArray(raw)) {
    actions = raw;
  } else if (clinicalIsObject(raw)) {
    const itemHistory = raw[item.id];
    actions = Array.isArray(itemHistory)
      ? itemHistory
      : clinicalIsObject(itemHistory)
        ? [itemHistory]
        : clinicalAsArray(raw.actions || raw.records);
  }
  if (Array.isArray(itemReview?.action_history)) {
    actions = [...actions, ...itemReview.action_history];
  }
  if (
    itemReview &&
    !actions.some(
      (action) =>
        (clinicalReviewPayload(action)?.action_id || action?.action_id) &&
        (clinicalReviewPayload(action)?.action_id || action?.action_id) ===
          itemReview.action_id,
    )
  ) {
    actions.push(itemReview);
  }
  const seen = new Set();
  return actions.filter((action) => {
    const review = clinicalReviewPayload(action);
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

function renderClinicalActionHistory(item) {
  const list = $("#clinical-action-history");
  list.replaceChildren();
  const actions = clinicalHistoryForItem(item);
  if (actions.length === 0) {
    const li = document.createElement("li");
    li.textContent = "아직 저장된 사람 검수 이력이 없습니다.";
    list.append(li);
    return;
  }
  actions.forEach((action) => {
    const li = document.createElement("li");
    const review = clinicalReviewPayload(action) || {};
    const reviewer =
      review.reviewer_id || review.reviewer || review.actor || "검토자 미상";
    const reviewerRole =
      review.reviewer_role || action.reviewer_role || "clinical_reviewer";
    const when =
      review.reviewed_at ||
      review.created_at ||
      review.timestamp ||
      "시각 미상";
    const roleLabel =
      CLINICAL_REVIEWER_ROLE_LABELS[reviewerRole] || reviewerRole;
    li.textContent = `${clinicalStatusLabel(
      review.review_status || action.review_status,
    )} · ${reviewer} (${roleLabel}) · ${when}`;
    if (review.notes) li.title = String(review.notes);
    list.append(li);
  });
}

function populateClinicalForm() {
  const item = selectedClinicalItem();
  if (!item || !state.clinical.draft) return;
  const annotation = state.clinical.draft;
  $("#clinical-observation").value = String(annotation.observation || "");
  $("#clinical-interpretation").value = String(
    annotation.interpretation || "",
  );
  ["observation", "interpretation"].forEach((field) => {
    $(`#clinical-${field}`).removeAttribute("aria-invalid");
    updateClinicalFieldCount(field);
  });
  $("#clinical-review-notes").value = state.clinical.reviewNotes;
  $("#clinical-anchor-summary").textContent = `f${
    annotation.anchor_source_frame_idx ?? "—"
  } · ${formatTime(clinicalAnchorSec(annotation))}`;
  $("#clinical-evidence-summary").textContent = formatClinicalRange(
    clinicalEvidenceStartSec(annotation),
    clinicalEvidenceEndSec(annotation),
  );
  $("#clinical-form-error").hidden = true;
}

function syncClinicalDraftFromForm() {
  if (
    !state.clinical.draft ||
    state.clinical.viewMode === "final" ||
    state.activeInspector !== "clinical"
  ) {
    return;
  }
  state.clinical.draft.observation =
    $("#clinical-observation").value.trim();
  state.clinical.draft.interpretation =
    $("#clinical-interpretation").value.trim();
  state.clinical.reviewNotes = $("#clinical-review-notes").value;
  ["observation", "interpretation"].forEach(updateClinicalFieldCount);
  renderClinicalDirtyIndicator();
  renderTimeline();
  renderClinicalOverlay();
}

function validateClinicalForm() {
  for (const field of ["observation", "interpretation"]) {
    const control = $(`#clinical-${field}`);
    const text = control.value.trim();
    const label = field === "observation" ? "관찰" : "해석";
    const sentenceCount = clinicalSentenceCount(text);
    if (!text) {
      return {
        control,
        message: `${label}을 1–2문장으로 입력해 주세요.`,
      };
    }
    if (sentenceCount > CLINICAL_MAX_SENTENCES) {
      return {
        control,
        message: `${label}은 2문장을 넘길 수 없습니다. 핵심 사실만 합쳐서 적어 주세요.`,
      };
    }
    if (text.length > CLINICAL_TEXT_MAX_LENGTH) {
      return {
        control,
        message: `${label}은 ${CLINICAL_TEXT_MAX_LENGTH}자를 넘길 수 없습니다.`,
      };
    }
  }
  return null;
}

function renderClinicalDirtyIndicator() {
  const indicator = $("#clinical-dirty-indicator");
  const dirty = hasDirtyClinicalDraft();
  indicator.dataset.dirty = String(dirty);
  indicator.textContent = dirty ? "저장하지 않은 변경" : "저장된 상태";
  $("#clinical-discard-draft").disabled =
    !dirty || state.clinical.saving;
}

function renderClinicalInspector() {
  const item = selectedClinicalItem();
  const empty = $("#clinical-inspector-empty");
  const form = $("#clinical-form");
  if (!item || !state.clinical.draft) {
    empty.hidden = false;
    form.hidden = true;
    $("#clinical-final-read-only").hidden = true;
    $("#clinical-selection-status").textContent = state.clinical.loadError
      ? "불러오기 실패"
      : "선택 없음";
    $("#clinical-selection-status").className =
      "status-badge status-neutral";
    $("#clinical-event-overlay").hidden = true;
    return;
  }

  populateClinicalForm();
  empty.hidden = true;
  form.hidden = false;
  const readOnly = state.clinical.viewMode === "final";
  $("#clinical-final-read-only").hidden = !readOnly;
  const status = normalizeClinicalStatus(item.status);
  $("#clinical-selection-status").textContent = clinicalStatusLabel(status);
  $("#clinical-selection-status").className =
    `status-badge ${
      status === "unreviewed" ? "status-neutral" : `status-${status}`
    }`;
  renderClinicalActionHistory(item);

  const provenance = clinicalIsObject(state.clinical.draft.provenance)
    ? state.clinical.draft.provenance
    : {};
  clinicalSetText("#clinical-provenance-generator", provenance.generator);
  clinicalSetText("#clinical-provenance-model", provenance.model);
  clinicalSetText("#clinical-provenance-authority", provenance.authority);
  clinicalSetText(
    "#clinical-source-views",
    Array.isArray(state.clinical.draft.source_views)
      ? state.clinical.draft.source_views.join(", ")
      : state.clinical.draft.source_views,
  );
  clinicalSetText(
    "#clinical-observation-confidence",
    clinicalConfidenceText(state.clinical.draft, "observation"),
  );
  clinicalSetText(
    "#clinical-interpretation-confidence",
    clinicalConfidenceText(state.clinical.draft, "interpretation"),
  );

  form.querySelectorAll("input, select, textarea").forEach((control) => {
    control.disabled = readOnly || state.clinical.saving;
  });
  $("#clinical-discard-draft").hidden = readOnly;
  form.querySelector(".review-actions").hidden = readOnly;
  $("#clinical-review-scope-help").hidden = readOnly;
  $("#clinical-dirty-indicator").hidden = readOnly;
  renderClinicalDirtyIndicator();
}

function selectClinicalItem(
  id,
  { bypassDirty = false, seek = true, focus = false } = {},
) {
  if (
    state.activeInspector === "clinical" &&
    id === state.clinical.selectedId &&
    state.clinical.draft
  ) {
    if (seek) seekToClinicalAnchor(state.clinical.draft);
    if (focus) {
      $(
        `[data-clinical-annotation-id="${CSS.escape(id)}"]`,
      )?.focus();
    }
    return true;
  }
  if (!bypassDirty && !guardAnyNavigation()) return false;
  const item = state.clinical.items.find((entry) => entry.id === id);
  if (!item) return false;
  state.activeInspector = "clinical";
  state.selected = null;
  state.draft = null;
  state.selectionBaseline = null;
  showFormError("");
  resetDangerArms();
  $("#candidate-alert").hidden = true;
  state.clinical.selectedId = item.id;
  state.clinical.draft = clinicalClone(item.annotation);
  state.clinical.reviewNotes = String(
    item.review?.review?.notes ||
      item.review?.notes ||
      item.review?.review_notes ||
      "",
  );
  state.clinical.baseline = clinicalSnapshotSignature(
    state.clinical.draft,
    state.clinical.reviewNotes,
  );
  renderAll();
  if (seek) {
    seekToClinicalAnchor(state.clinical.draft);
  }
  if (focus) {
    $(`[data-clinical-annotation-id="${CSS.escape(id)}"]`)?.focus();
  }
  return true;
}

function discardClinicalDraft() {
  const item = selectedClinicalItem();
  if (
    !item ||
    !state.clinical.baseline ||
    state.clinical.viewMode === "final"
  ) {
    return;
  }
  const baseline = JSON.parse(state.clinical.baseline);
  state.clinical.draft = clinicalClone(baseline.annotation);
  state.clinical.reviewNotes = baseline.notes;
  populateClinicalForm();
  renderClinicalInspector();
  renderTimeline();
  renderClinicalOverlay();
  toast("저장하지 않은 임상 필드 변경을 취소했습니다.");
}

function navigateClinicalItem(direction) {
  navigateCombinedItem(direction);
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function toast(message, duration = 3600) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    node.hidden = true;
  }, duration);
}

function showFormError(message, targetSelector = null) {
  const node = $("#form-error");
  node.textContent = message || "";
  node.hidden = !message;
  $$("[data-error-linked='true']").forEach((control) => {
    control.removeAttribute("aria-invalid");
    const describedBy = (control.getAttribute("aria-describedby") || "")
      .split(/\s+/)
      .filter((value) => value && value !== "form-error");
    if (describedBy.length) {
      control.setAttribute("aria-describedby", describedBy.join(" "));
    } else {
      control.removeAttribute("aria-describedby");
    }
    delete control.dataset.errorLinked;
  });
  if (!message || !targetSelector) return;
  const target = $(targetSelector);
  if (!target) return;
  const describedBy = new Set(
    (target.getAttribute("aria-describedby") || "")
      .split(/\s+/)
      .filter(Boolean),
  );
  describedBy.add("form-error");
  target.setAttribute("aria-describedby", [...describedBy].join(" "));
  target.setAttribute("aria-invalid", "true");
  target.dataset.errorLinked = "true";
  requestAnimationFrame(() => target.focus({ preventScroll: false }));
}

function validationError(message, targetSelector = null) {
  const error = new Error(message);
  error.targetSelector = targetSelector;
  return error;
}

function formatTime(seconds, milliseconds = true) {
  const safe = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(safe / 60);
  const remainder = safe - minutes * 60;
  const precision = milliseconds ? 3 : 0;
  return `${String(minutes).padStart(2, "0")}:${remainder
    .toFixed(precision)
    .padStart(milliseconds ? 6 : 2, "0")}`;
}

function candidateMeta(candidate) {
  return candidate?._review_ui || {};
}

function actionId(action) {
  return action?.action_id || action?.decision_id || "";
}

function effectiveAction(candidate) {
  const meta = candidateMeta(candidate);
  return (
    meta.effective_decision ||
    candidate?.effective_decision ||
    meta.legacy_decision ||
    meta.human_decision ||
    null
  );
}

function firstFrameIndex(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const numeric = Number(value);
    if (Number.isInteger(numeric)) return numeric;
  }
  return 0;
}

function immutableSnapshot(value) {
  const snapshot = structuredClone(value);
  const freeze = (item) => {
    if (!item || typeof item !== "object" || Object.isFrozen(item)) return item;
    Object.values(item).forEach(freeze);
    return Object.freeze(item);
  };
  return freeze(snapshot);
}

function actionHistory(candidate) {
  const meta = candidateMeta(candidate);
  const history = Array.isArray(meta.action_history)
    ? [...meta.action_history]
    : Array.isArray(candidate?.action_history)
      ? [...candidate.action_history]
      : [];
  const legacy = meta.legacy_decision || meta.human_decision;
  if (legacy && !history.some((item) => actionId(item) === actionId(legacy))) {
    history.unshift(legacy);
  }
  return history;
}

function candidateKey(candidate, fallbackIndex = 0) {
  const meta = candidateMeta(candidate);
  return String(
    meta.candidate_id ||
      candidate?.candidate_id ||
      candidate?.event_id ||
      meta.annotation_id ||
      actionId(effectiveAction(candidate)) ||
      `timeline-item-${fallbackIndex}`,
  );
}

function sourceItems() {
  if (!state.data) return [];
  if (isFinalMode()) {
    return [
      ...finalInteractionItems(),
      ...reviewAttentionEvents(),
      ...phaseContextEvents(),
    ];
  }
  const collections = [
    state.data.candidates,
    state.data.effective_annotations,
    state.data.created_annotations,
    state.data.human_annotations,
  ];
  const seen = new Set();
  const items = [];
  collections.forEach((collection) => {
    if (!Array.isArray(collection)) return;
    collection.forEach((candidate, index) => {
      // A rejected revision of a human-created event is its append-only
      // withdrawal tombstone. Keep it in the audit log, not in active views.
      if (
        candidateMeta(candidate).human_created &&
        reviewStatus(candidate) === "rejected"
      ) {
        return;
      }
      const key = candidateKey(candidate, index);
      if (seen.has(key)) return;
      seen.add(key);
      items.push(candidate);
    });
  });
  return items;
}

function isVisibleCandidate(candidate) {
  const fields = fieldsForCandidate(candidate);
  return Boolean(state.filters[TRACK_FOR_EVENT[fields.event_type]]);
}

function visibleSourceItems() {
  return sourceItems().filter(isVisibleCandidate);
}

function fieldsForCandidate(candidate) {
  const action = effectiveAction(candidate);
  const fields = action?.adjudicated_fields || {};
  const eventType =
    fields.event_type || candidate?.event_type || "implicit_tool_request";
  const compatibilityFrame = firstFrameIndex(
    fields.source_frame_idx,
    candidate?.source_frame_idx,
  );
  const startFrame =
    eventType === "implicit_tool_request"
      ? firstFrameIndex(
          fields.start_source_frame_idx,
          candidate?.start_source_frame_idx,
          compatibilityFrame,
        )
      : compatibilityFrame;
  const endFrame =
    eventType === "implicit_tool_request"
      ? firstFrameIndex(
          fields.end_source_frame_idx,
          candidate?.end_source_frame_idx,
          startFrame,
        )
      : startFrame;
  return {
    event_type: eventType,
    source_frame_idx: startFrame,
    start_source_frame_idx: startFrame,
    end_source_frame_idx: endFrame,
    tool: fields.tool ?? baseField(candidate, "tool") ?? null,
    from: fields.from ?? baseField(candidate, "from") ?? null,
    to: fields.to ?? baseField(candidate, "to") ?? null,
    phase_id: fields.phase_id ?? baseField(candidate, "phase_id") ?? null,
  };
}

function originalIntervalForCandidate(candidate, eventType) {
  const startFrame = firstFrameIndex(
    candidate?.start_source_frame_idx,
    candidate?.source_frame_idx,
  );
  const endFrame =
    eventType === "implicit_tool_request"
      ? firstFrameIndex(candidate?.end_source_frame_idx, startFrame)
      : startFrame;
  return { startFrame, endFrame };
}

function baseField(candidate, key) {
  const value = candidate?.[key];
  if (value && typeof value === "object") {
    return value.id || value.location || "";
  }
  return value ?? "";
}

function reviewStatus(candidate) {
  return (
    effectiveAction(candidate)?.review_status ||
    candidate?.review_status ||
    "unreviewed"
  );
}

function statusLabel(status) {
  if (status === "confirmed") return "확정";
  if (status === "ambiguous") return "애매";
  if (status === "rejected") return "기각";
  return "미검토";
}

function statusSymbol(status) {
  if (status === "confirmed") return "✓";
  if (status === "ambiguous") return "?";
  if (status === "rejected") return "×";
  return "○";
}

function eventTitle(candidate, fields = fieldsForCandidate(candidate)) {
  if (fields.event_type === "tool_transfer") {
    return fields.tool ? humanizeIdentifier(fields.tool) : "도구 이동";
  }
  if (fields.event_type === "phase_start") {
    return phaseDisplayLabel(fields.phase_id);
  }
  return "암묵적 손 요청";
}

function timestamps() {
  return state.data?.timestamps_sec || [];
}

function timelineStart() {
  return Number(state.data?.start_sec ?? timestamps()[0] ?? 0);
}

function timelineEnd() {
  return Number(
    state.data?.media?.duration_sec ??
      state.data?.end_sec ??
      timestamps().at(-1) ??
      0,
  );
}

function timeForFrame(frameIndex) {
  const values = timestamps();
  if (!values.length) return 0;
  const index = clamp(Math.trunc(frameIndex), 0, values.length - 1);
  return Number(values[index]);
}

function nearestFrameIndex(timeSec) {
  const values = timestamps();
  if (!values.length) return 0;
  const target = Number(timeSec);
  if (target <= values[0]) return 0;
  if (target >= values[values.length - 1]) return values.length - 1;
  let left = 0;
  let right = values.length - 1;
  while (left < right) {
    const middle = Math.floor((left + right) / 2);
    if (values[middle] < target) left = middle + 1;
    else right = middle;
  }
  const upper = left;
  const lower = upper - 1;
  return target - values[lower] <= values[upper] - target ? lower : upper;
}

function frameIndexAtOrBefore(timeSec) {
  const values = timestamps();
  if (!values.length) return 0;
  const target = Number(timeSec);
  if (target <= values[0]) return 0;
  if (target >= values[values.length - 1]) return values.length - 1;
  let left = 0;
  let right = values.length;
  while (left < right) {
    const middle = Math.floor((left + right) / 2);
    if (values[middle] <= target) left = middle + 1;
    else right = middle;
  }
  return Math.max(0, left - 1);
}

function gapAt(timeSec) {
  const target = Number(timeSec);
  return (state.data?.gaps || []).find(
    (gap) =>
      target > Number(gap.before_time_sec) &&
      target < Number(gap.after_time_sec),
  );
}

function visualEndTime() {
  return Number(
    state.data?.media?.visual_end_sec ??
      state.data?.visual_end_sec ??
      timestamps().at(-1) ??
      0,
  );
}

function visualUnavailability(timeSec = state.currentTimeSec) {
  if (gapAt(timeSec)) {
    return {
      code: "CAMERA GAP",
      message: "평가 불가",
      detail: "카메라 공백 안에는 시각 이벤트를 만들거나 옮길 수 없습니다.",
    };
  }
  if (Number(timeSec) > visualEndTime() + 1e-7) {
    return {
      code: "VIDEO OFF SCREEN",
      message: "시각 평가 불가",
      detail: "영상이 끝난 뒤의 audio-only 구간에는 시각 이벤트를 만들 수 없습니다.",
    };
  }
  return null;
}

function updateVisualAvailability() {
  if (!state.data) return;
  const unavailable = visualUnavailability();
  const blocker = $("#visual-blocker");
  blocker.hidden = !unavailable;
  if (unavailable) {
    $("#visual-blocker-code").textContent = unavailable.code;
    $("#visual-blocker-message").textContent = unavailable.message;
  }
  ["#new-event", "#new-event-empty"].forEach((selector) => {
    $(selector).disabled =
      state.saving || Boolean(unavailable) || isFinalMode();
  });
  ["#confirm", "#ambiguous", "#reject", "#withdraw"].forEach((selector) => {
    $(selector).disabled = state.saving || isFinalMode();
  });
  ["#move-to-playhead", "#set-request-start", "#set-request-end"].forEach(
    (selector) => {
      $(selector).disabled =
        state.saving || Boolean(unavailable) || isFinalMode();
    },
  );
  if (unavailable) renderVideoEventOverlay([]);
}

function snapOutOfGap(timeSec) {
  const gap = gapAt(timeSec);
  if (!gap) return Number(timeSec);
  const before = Number(gap.before_time_sec);
  const after = Number(gap.after_time_sec);
  return timeSec - before <= after - timeSec ? before : after;
}

function observableFrameSegment(frameIndex) {
  const lastVisualFrame = nearestFrameIndex(visualEndTime());
  let minimum = 0;
  let maximum = Math.min(
    Math.max(0, Number(state.data?.frame_count || 1) - 1),
    lastVisualFrame,
  );
  for (const gap of state.data?.gaps || []) {
    const beforeFrame = Number(gap.before_frame_idx);
    const afterFrame = Number(gap.after_frame_idx);
    if (frameIndex <= beforeFrame) {
      maximum = Math.min(maximum, beforeFrame);
      break;
    }
    if (frameIndex >= afterFrame) {
      minimum = Math.max(minimum, afterFrame);
    }
  }
  return { minimum, maximum };
}

function clampRequestFrameToPeer(frameIndex, peerFrameIndex) {
  const segment = observableFrameSegment(peerFrameIndex);
  return clamp(frameIndex, segment.minimum, segment.maximum);
}

function selectedCandidate() {
  if (!["candidate", "human", "final"].includes(state.selected?.kind)) {
    return null;
  }
  return (
    sourceItems().find(
      (candidate, index) =>
        candidateKey(candidate, index) === state.selected.id,
    ) || null
  );
}

function setSelectionStatus(status, fallback = "선택 없음") {
  const node = $("#selection-status");
  node.textContent = status ? statusLabel(status) : fallback;
  node.className = `status-badge ${
    status && status !== "unreviewed" ? `status-${status}` : "status-neutral"
  }`;
}

function setDraftDirty(dirty = true) {
  if (!state.draft) return;
  state.draft.dirty = dirty;
  renderInspectorTiming();
  renderTimeline();
  updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
    reason: "seek",
  });
}

function createTypeOptions() {
  const values = state.data?.vocabulary?.event_types || [
    "implicit_tool_request",
    "tool_transfer",
    "phase_start",
  ];
  const container = $("#event-type-options");
  container.replaceChildren(
    ...values.map((eventType) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "type-option";
      button.dataset.eventType = eventType;
      button.textContent = EVENT_LABELS[eventType] || eventType;
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", () => {
        if (!state.draft || state.draft.event_type === eventType) return;
        state.draft.event_type = eventType;
        if (eventType === "implicit_tool_request") {
          state.draft.start_source_frame_idx = state.draft.source_frame_idx;
          state.draft.end_source_frame_idx = state.draft.source_frame_idx;
        } else {
          state.draft.source_frame_idx =
            state.draft.start_source_frame_idx ?? state.draft.source_frame_idx;
          state.draft.start_source_frame_idx = state.draft.source_frame_idx;
          state.draft.end_source_frame_idx = state.draft.source_frame_idx;
        }
        state.draft.dirty = true;
        renderInspectorFields();
        renderInspectorTiming();
        renderTimeline();
      });
      return button;
    }),
  );
}

function fillSelect(node, values) {
  node.replaceChildren(
    ...values.map((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      return option;
    }),
  );
}

function fillToolSuggestions() {
  const values = [
    ...new Set(
      sourceItems()
        .map((candidate) => fieldsForCandidate(candidate).tool)
        .filter(Boolean),
    ),
  ].sort();
  $("#tool-id-options").replaceChildren(
    ...values.map((value) => {
      const option = document.createElement("option");
      option.value = value;
      return option;
    }),
  );
}

function hasDirtyDraft() {
  return Boolean(state.draft?.dirty);
}

function guardSaving() {
  if (!state.saving) return true;
  toast("판정을 저장하고 있습니다. 저장이 끝난 뒤 다시 시도해 주세요.");
  return false;
}

function guardDirtyDraft() {
  if (!hasDirtyDraft()) return true;
  toast("현재 이벤트 수정이 저장되지 않았습니다. 확정하거나 변경 취소 후 이동하세요.");
  return false;
}

function selectCandidate(
  candidate,
  {
    seek = true,
    bypassDirty = false,
    bypassSaving = false,
    navigatorPlacement = "preserve",
  } = {},
) {
  if (!bypassSaving && isAnySaving()) {
    toast("판정을 저장하고 있습니다. 저장이 끝난 뒤 이동해 주세요.");
    return false;
  }
  const key = candidateKey(candidate);
  const selectionKind = isFinalMode()
    ? "final"
    : candidateMeta(candidate).human_created
      ? "human"
      : "candidate";
  if (
    state.activeInspector === "interaction" &&
    state.selected?.kind === selectionKind &&
    state.selected.id === key &&
    state.draft
  ) {
    if (seek) seekToFrame(state.draft.start_source_frame_idx);
    if (navigatorPlacement === "top") {
      alignNavigatorCandidateToTop(key);
    }
    return true;
  }
  if (
    !bypassDirty &&
    (state.activeInspector !== "interaction" ||
      !state.selected ||
      state.selected.kind !== selectionKind ||
      state.selected.id !== key) &&
    !guardAnyNavigation()
  ) {
    return false;
  }
  const fields = fieldsForCandidate(candidate);
  const action = effectiveAction(candidate);
  const original = originalIntervalForCandidate(candidate, fields.event_type);
  state.activeInspector = "interaction";
  setClinicalSelectionState(null);
  state.selected = { kind: selectionKind, id: key };
  state.draft = {
    ...fields,
    original_frame_idx: original.startFrame,
    original_start_frame_idx: original.startFrame,
    original_end_frame_idx: original.endFrame,
    notes: action?.review?.notes || candidate?.review?.notes || "",
    dirty: false,
  };
  state.selectionBaseline = immutableSnapshot(state.draft);
  showFormError("");
  resetDangerArms();
  $("#candidate-alert").hidden = true;
  if (seek) seekToFrame(fields.start_source_frame_idx);
  renderAll({
    navigatorCandidateAtTop:
      navigatorPlacement === "top" ? key : null,
  });
  return true;
}

function selectSpeechEvent(event, { seek = true, bypassDirty = false } = {}) {
  if (!isFinalMode() || !event) return false;
  if (!bypassDirty && !guardAnyNavigation()) return false;
  state.activeInspector = "interaction";
  setClinicalSelectionState(null);
  const index = speechEvents().indexOf(event);
  state.selected = {
    kind: "speech",
    id: speechEventKey(event, Math.max(0, index)),
  };
  state.draft = null;
  state.selectionBaseline = null;
  showFormError("");
  resetDangerArms();
  $("#candidate-alert").hidden = true;
  if (seek) {
    seekToTime(speechCompletionSec(event));
  }
  renderAll();
  return true;
}

function createNewAnnotation() {
  if (isFinalMode()) {
    toast("최종 검수 모드는 읽기 전용입니다.");
    return;
  }
  if (!state.data || !guardAnyNavigation()) return;
  pausePlayback();
  const unavailable = visualUnavailability();
  if (unavailable) {
    toast(unavailable.detail);
    return;
  }
  const allowed = state.data.vocabulary?.event_types || [];
  const eventType = allowed.includes("implicit_tool_request")
    ? "implicit_tool_request"
    : allowed[0] || "implicit_tool_request";
  const endpoints = state.data.vocabulary?.transfer_endpoints || [];
  state.activeInspector = "interaction";
  setClinicalSelectionState(null);
  state.selected = {
    kind: "new",
    id: `new-${Date.now()}`,
  };
  state.draft = {
    event_type: eventType,
    source_frame_idx: state.currentFrame,
    start_source_frame_idx: state.currentFrame,
    end_source_frame_idx: state.currentFrame,
    original_frame_idx: null,
    original_start_frame_idx: null,
    original_end_frame_idx: null,
    client_request_id: crypto.randomUUID(),
    tool: null,
    from: endpoints[0] || "mayo_stand",
    to: endpoints[1] || "scrub_nurse",
    phase_id: null,
    notes: "",
    dirty: true,
  };
  state.selectionBaseline = null;
  showFormError("");
  resetDangerArms();
  $("#candidate-alert").hidden = true;
  renderAll();
  toast("새 이벤트 초안을 만들었습니다. 요청 구간은 시작과 종료를 각각 지정하세요.");
}

function clearSelection({ force = false } = {}) {
  if (!force && !guardAnyNavigation()) return;
  state.activeInspector = "interaction";
  setClinicalSelectionState(null);
  state.selected = null;
  state.draft = null;
  state.selectionBaseline = null;
  showFormError("");
  resetDangerArms();
  $("#candidate-alert").hidden = true;
  renderAll();
}

function discardDraft() {
  if (!state.selected || !guardSaving()) return;
  pausePlayback();
  $("#candidate-alert").hidden = true;
  if (state.selected.kind === "new") {
    clearSelection({ force: true });
    toast("저장하지 않은 새 이벤트 초안을 취소했습니다.");
    return;
  }
  if (!state.selectionBaseline) return;
  state.draft = structuredClone(state.selectionBaseline);
  state.draft.dirty = false;
  showFormError("");
  resetDangerArms();
  seekToFrame(state.draft.start_source_frame_idx);
  renderAll();
  toast("저장된 기준값으로 되돌렸습니다. 요청 구간과 모든 필드를 복원했습니다.");
}

function syncDraftFromForm() {
  if (!state.draft) return;
  state.draft.tool = $("#tool-id").value.trim() || null;
  state.draft.from = $("#from-location").value || null;
  state.draft.to = $("#to-location").value || null;
  state.draft.phase_id = $("#phase-id").value.trim() || null;
  state.draft.notes = $("#review-notes").value;
}

function dispositionText(candidate) {
  const disposition = finalMeta(candidate).disposition || {};
  const kind = disposition.kind || disposition.operation || "identity";
  const labels = {
    identity: "DT 평가에 그대로 유지",
    collapse_source: "DT 반환 연쇄의 중간 관측",
    collapsed_source: "DT 반환 연쇄의 중간 관측",
    collapsed_output: "집도의 → 메이요 1건으로 축약",
    excluded_cleanup: "스크럽 정리 행동으로 DT 평가 제외",
    excluded_unclosed: "메이요 도착 미관측으로 현재 DT 비채점",
    excluded_unclosed_direct_return:
      "메이요 도착 미관측으로 현재 DT 비채점",
    excluded_unresolved_transfer:
      "종류·수량·최종배치 미확정으로 DT 비채점",
    normalization_source:
      "관측 역할은 미확정으로 보존 · DT에서만 집도의 정규화",
    normalized_output:
      "DT 계약에서만 집도의 수령으로 정규화",
  };
  return labels[kind] || disposition.label || humanizeIdentifier(kind);
}

function renderFinalInspector(candidate) {
  const fields = fieldsForCandidate(candidate);
  const meta = finalMeta(candidate);
  const disposition = meta.disposition || {};
  const presentation = candidate?.review_presentation || {};
  const isPhaseContext =
    fields.event_type === "phase_start" && meta.context_only === true;
  const isReviewAttention = meta.review_attention === true;
  const isRequest = fields.event_type === "implicit_tool_request";
  if (isPhaseContext) {
    $("#final-mode-banner-title").textContent =
      "임시 수술 단계 문맥 · 평가 정답 아님";
    $("#final-mode-banner-detail").textContent =
      "사람 판정은 ambiguous이며 상호작용 참조와 확정 개수에 포함되지 않습니다.";
  } else if (isReviewAttention) {
    $("#final-mode-banner-title").textContent =
      "AI 재검토 애매 · 사람 최종확인 필요";
    $("#final-mode-banner-detail").textContent =
      "확정 관측과 DT 정답에서 제외된 읽기 전용 검토 항목입니다.";
  } else {
    $("#final-mode-banner-title").textContent = "읽기 전용 참조";
    $("#final-mode-banner-detail").textContent =
      "이 화면에서는 최종 참조 파일을 수정할 수 없습니다.";
  }
  const startFrame = fields.start_source_frame_idx;
  const endFrame = fields.end_source_frame_idx;
  const evidenceStart = presentation.evidence_start_source_frame_idx;
  const evidenceEnd = presentation.evidence_end_source_frame_idx;
  $("#final-event-id").textContent = candidate?.event_id || candidateKey(candidate);
  $("#final-event-type").textContent =
    EVENT_LABELS[fields.event_type] || fields.event_type;
  $("#final-event-time").textContent =
    isReviewAttention &&
    Number.isInteger(evidenceStart) &&
    Number.isInteger(evidenceEnd)
      ? `${formatTime(timeForFrame(evidenceStart))}–${formatTime(
          timeForFrame(evidenceEnd),
        )} · f${evidenceStart}–f${evidenceEnd} · 첫 명확 f${fields.source_frame_idx}`
      : isRequest
      ? `${formatTime(timeForFrame(startFrame))}–${formatTime(timeForFrame(endFrame))} · ` +
        `f${startFrame}–f${endFrame}`
      : `${formatTime(timeForFrame(fields.source_frame_idx))} · f${fields.source_frame_idx}`;
  const useDtPresentation =
    state.viewMode === "final_dt" && !isReviewAttention;
  const observation =
    (useDtPresentation ? presentation.dt_observation_ko : null) ||
    presentation.observation_ko ||
    (isPhaseContext
      ? `${phaseDisplayLabel(fields.phase_id)} · ${humanizeIdentifier(
          candidate?.phase_boundary_kind,
        )}`
      : isRequest
        ? "빈 손바닥을 펼쳐 도구 수령 자세를 유지"
        : `${humanizeIdentifier(fields.tool)} · ` +
          `${humanizeIdentifier(fields.from)} → ${humanizeIdentifier(fields.to)}`);
  const interpretation =
    (useDtPresentation ? presentation.dt_interpretation_ko : null) ||
    presentation.interpretation_ko ||
    (isPhaseContext
      ? "수술 단계 경계는 애매 문맥이며 상호작용 정답으로 사용하지 않습니다."
      : isRequest
        ? "관측된 빈 손 자세만 기록하며 요청 도구 의미는 음성 문맥과 분리합니다."
        : "도구·출발·도착의 직접 관측을 표시합니다.");
  $("#final-event-observation").textContent = observation;
  $("#final-event-interpretation").textContent = interpretation;
  const reviewer = candidate?.review?.reviewer_id || "—";
  $("#final-event-origin").textContent = isPhaseContext
    ? `사람 판정 애매 · ${reviewer}`
    : isReviewAttention
      ? `authorized assistant 재검토 · ${reviewer} · 사람 확정 전`
      : `${humanizeIdentifier(candidate?.label_origin)} · ${reviewer}`;
  $("#final-event-disposition-label").textContent =
    isPhaseContext || isReviewAttention
    ? "정답 역할"
    : "DT 처리";
  $("#final-event-disposition").textContent = isPhaseContext
    ? "provisional 문맥 전용 · 상호작용 정답/확정 개수 집계 제외"
    : isReviewAttention
      ? "ambiguous 문맥 전용 · 확정 관측/DT 정답 집계 제외"
      : dispositionText(candidate);
  $("#final-event-note").textContent =
    candidate?.review?.notes ||
    (isPhaseContext || isReviewAttention
      ? "전이 경계에 대한 별도 사람 메모가 없습니다."
      : "별도 최종 검수 메모가 없습니다.");
  $("#final-projection-kicker").textContent = isPhaseContext
    ? "수술 단계 문맥 경계"
    : isReviewAttention
      ? "비채점 근거"
      : "평가 투영 근거";
  $("#final-projection-reason").textContent = isPhaseContext
    ? "사람 검토가 끝났지만 경계는 ambiguous입니다. 현재 수술 단계 표시를 돕는 별도 문맥이며 평가 ground truth가 아닙니다."
    : isReviewAttention
      ? "가림 때문에 정확한 point를 확정할 수 없어 assistant 재검토가 ambiguous로 남았습니다. 사람 확인 전에는 ground truth로 승격되지 않습니다."
      : disposition.reason ||
        (state.viewMode === "final_dt"
          ? "확정 관측에서 결정론적으로 투영된 DT 평가 이벤트입니다."
          : "관측 사실은 보존되며 DT 평가는 별도 투영 규칙을 적용합니다.");
  const sourceIds =
    disposition.source_event_ids ||
    meta.source_event_ids ||
    [candidate?.event_id].filter(Boolean);
  $("#final-projection-sources").textContent =
    isPhaseContext
      ? `수술 단계 후보: ${candidate?.event_id || "—"}`
      : isReviewAttention
        ? `근거 범위: f${evidenceStart}–f${evidenceEnd} · ${(
            presentation.source_views || []
          ).join(" + ")}`
      : sourceIds.length > 1
      ? `원시 관측: ${sourceIds.join(" + ")}`
      : `원시 관측: ${sourceIds[0] || "—"}`;
  if (isPhaseContext) {
    $("#inspector-title").textContent = "임시 수술 단계 문맥";
  } else if (isReviewAttention) {
    $("#inspector-title").textContent = "사람 확인이 필요한 재검토";
  } else {
    $("#inspector-title").textContent =
      state.viewMode === "final_dt"
        ? "최종 DT 이벤트 근거"
        : "평가 전 원시 관측 근거";
  }
}

function formatExactSpeechTime(value) {
  const timeSec = Number(value);
  if (!Number.isFinite(timeSec)) return "—";
  return `${formatTime(timeSec)} · ${timeSec.toFixed(9)} s`;
}

function semanticHintText(value) {
  if (!value) return "원본 발화 · 의미 미분류";
  const labels = {
    tool_request: "도구 요청",
    correction: "정정",
    cancellation: "취소",
    procedure_start: "수술 시작",
    procedure_completion: "수술 종료",
    other: "기타 발화",
  };
  return labels[value] || humanizeIdentifier(value);
}

function renderSpeechInspector(event) {
  const track = speechContextTrack() || {};
  const tools = Array.isArray(event?.tool_hints) ? event.tool_hints : [];
  const availableSec = Number(
    event?.available_sec ??
      event?._review_ui?.complete_text_available_sec ??
      event?.time_sec,
  );
  $("#speech-event-id").textContent = event?.event_id || "—";
  $("#speech-event-text").textContent = event?.text || "발화 원문이 없습니다.";
  $("#speech-event-time").textContent = formatExactSpeechTime(event?.time_sec);
  $("#speech-event-end").textContent = formatExactSpeechTime(event?.end_sec);
  $("#speech-event-available").textContent =
    `${formatExactSpeechTime(availableSec)} · ` +
    (event?.available_sec === undefined
      ? "v1 legacy: 원본 시각부터"
      : "완전한 발화 텍스트 노출 가능");
  $("#speech-event-semantic").textContent = semanticHintText(
    event?.semantic_hint,
  );
  $("#speech-event-tools").textContent = tools.length
    ? tools.map(humanizeIdentifier).join(", ")
    : "원본 트랙에서 추론하지 않음";
  $("#speech-event-authority").textContent = humanizeIdentifier(
    event?.source_authority || event?.authority || track.authority,
  );
  $("#speech-event-scoring-role").textContent =
    "이 발화는 검수 문맥이며, 요청·도구 이동 정답과 진행률에는 집계되지 않습니다. " +
    `scoring_role: ${event?.scoring_role || track.scoring_role || "context_only"}`;
}

function renderInspector() {
  const speechEvent = selectedSpeechEvent();
  const hasEventSelection = Boolean(state.selected && state.draft);
  const hasSpeechSelection = Boolean(speechEvent && isFinalMode());
  const hasSelection = hasEventSelection || hasSpeechSelection;
  $("#inspector-empty").hidden = hasSelection;
  $("#review-form").hidden = !hasEventSelection || isFinalMode();
  $("#final-inspector").hidden = !hasEventSelection || !isFinalMode();
  $("#speech-inspector").hidden = !hasSpeechSelection;
  $("#new-event-empty").hidden = isFinalMode();
  $("#inspector-empty-detail").textContent = isFinalMode()
    ? "목록의 평가 이벤트·임시 수술 단계 또는 타임라인의 음성 문맥을 선택해 상세 내용을 확인할 수 있습니다."
    : "영상을 재생하면 미검토 AI 후보에서 자동으로 멈춥니다.";
  if (!hasSelection) {
    setSelectionStatus(null, isFinalMode() ? "읽기 전용" : "선택 없음");
    return;
  }

  if (hasSpeechSelection) {
    $("#inspector-title").textContent = "음성 발화 문맥";
    setSelectionStatus(null, "문맥 전용");
    renderSpeechInspector(speechEvent);
    return;
  }

  const candidate = selectedCandidate();
  if (isFinalMode()) {
    setSelectionStatus(
      finalMeta(candidate).context_only ? "ambiguous" : "confirmed",
    );
    renderFinalInspector(candidate);
    return;
  }
  const action = candidate ? effectiveAction(candidate) : null;
  setSelectionStatus(
    candidate ? reviewStatus(candidate) : null,
    state.selected.kind === "new" ? "새 이벤트" : "미검토",
  );
  $("#review-notes").value = state.draft.notes || "";
  $("#tool-id").value = state.draft.tool || "";
  $("#from-location").value = state.draft.from || "mayo_stand";
  $("#to-location").value = state.draft.to || "scrub_nurse";
  $("#phase-id").value = state.draft.phase_id || "";
  const isCandidate = state.selected.kind === "candidate";
  const isHuman = state.selected.kind === "human";
  $("#reject").hidden = !isCandidate;
  $("#withdraw").hidden = !isHuman;
  $("#review-scope-help").hidden = !isCandidate && !isHuman;
  $("#review-scope-help").textContent = isHuman
    ? "철회하면 활성 목록과 타임라인에서는 사라지지만, 누가 언제 철회했는지는 감사 이력에 남습니다."
    : "기각은 선택한 AI 후보 하나에만 적용됩니다. 주변 구간을 ‘이벤트 없음’으로 판정하지 않습니다.";
  $("#origin-kicker").textContent = isHuman
    ? "사람 생성 이벤트 기준"
    : state.selected.kind === "new"
      ? "새 이벤트 초안"
      : "AI 원래 후보";
  $("#context-summary").textContent = isHuman
    ? "판정 이력"
    : "AI 근거와 판정 이력";
  $("#candidate-origin").hidden = false;
  $("#confirm").textContent = action ? "확정으로 정정 · Enter" : "확정 · Enter";
  $("#ambiguous").textContent = action ? "애매로 정정 · A" : "애매함 · A";
  renderInspectorFields();
  renderInspectorTiming();
  renderEvidence(candidate);
  updateVisualAvailability();
}

function renderInspectorFields() {
  if (!state.draft) return;
  $$(".type-option").forEach((button) => {
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.eventType === state.draft.event_type),
    );
    button.disabled = state.selected?.kind !== "new";
  });
  $("#transfer-fields").hidden = state.draft.event_type !== "tool_transfer";
  $("#phase-fields").hidden = state.draft.event_type !== "phase_start";
  const isRequest = state.draft.event_type === "implicit_tool_request";
  $("#request-help").hidden = !isRequest;
  $("#request-timing-actions").hidden = !isRequest;
  $("#request-range-summary").hidden = !isRequest;
  $("#move-to-playhead").hidden = isRequest;
  $("#timing-actions").classList.toggle("request-mode", isRequest);
}

function renderInspectorTiming() {
  if (!state.draft) return;
  const isRequest = state.draft.event_type === "implicit_tool_request";
  const draftStartFrame = isRequest
    ? state.draft.start_source_frame_idx
    : state.draft.source_frame_idx;
  const draftEndFrame = isRequest
    ? state.draft.end_source_frame_idx
    : draftStartFrame;
  const draftStartTime = timeForFrame(draftStartFrame);
  const draftEndTime = timeForFrame(draftEndFrame);
  $("#draft-frame").textContent = isRequest
    ? `frame ${draftStartFrame}–${draftEndFrame}`
    : `frame ${draftStartFrame}`;
  $("#draft-time").textContent = isRequest
    ? `${formatTime(draftStartTime)}–${formatTime(draftEndTime)}`
    : `${formatTime(draftStartTime)} · ${draftStartTime.toFixed(9)} s`;
  $("#request-range-summary").textContent =
    `요청 구간 ${formatTime(draftStartTime)}–${formatTime(draftEndTime)} · ` +
    `f${draftStartFrame}–f${draftEndFrame} · ` +
    `${Math.max(0, draftEndTime - draftStartTime).toFixed(3)} s`;
  if (state.draft.original_frame_idx === null) {
    $("#original-frame").textContent = "사람 신규";
    $("#original-time").textContent = "AI 원안 없음";
    $("#time-offset").textContent = isRequest
      ? "시작과 종료를 직접 지정하는 새 요청 구간입니다."
      : "현재 playhead에 새 point를 생성합니다.";
    return;
  }
  const originalStartFrame =
    state.draft.original_start_frame_idx ?? state.draft.original_frame_idx;
  const originalEndFrame = isRequest
    ? state.draft.original_end_frame_idx ?? originalStartFrame
    : originalStartFrame;
  const originalStartTime = timeForFrame(originalStartFrame);
  const originalEndTime = timeForFrame(originalEndFrame);
  const startDelta = draftStartFrame - originalStartFrame;
  const endDelta = draftEndFrame - originalEndFrame;
  $("#original-frame").textContent = isRequest
    ? `frame ${originalStartFrame}–${originalEndFrame}`
    : `frame ${originalStartFrame}`;
  $("#original-time").textContent = isRequest
    ? `${formatTime(originalStartTime)}–${formatTime(originalEndTime)}`
    : `${formatTime(originalStartTime)} · ${originalStartTime.toFixed(9)} s`;
  if (startDelta === 0 && endDelta === 0) {
    $("#time-offset").textContent = state.draft.dirty
      ? "핵심 필드가 수정되었습니다."
      : isRequest && originalStartFrame === originalEndFrame
        ? "AI point 원안을 시작=종료인 0초 구간으로 표시합니다."
        : "AI 원래 시각과 같습니다.";
  } else if (isRequest) {
    const startSign = startDelta > 0 ? "+" : "";
    const endSign = endDelta > 0 ? "+" : "";
    $("#time-offset").textContent =
      `AI 원안 대비 시작 ${startSign}${startDelta} frame · ` +
      `종료 ${endSign}${endDelta} frame`;
  } else {
    const sign = startDelta > 0 ? "+" : "";
    $("#time-offset").textContent =
      `AI 원안에서 ${sign}${startDelta} frame 이동 · ` +
      `${(draftStartTime - originalStartTime).toFixed(3)} s`;
  }
}

function renderEvidence(candidate) {
  const aiReview = candidate?.ai_review || {};
  const proposal = candidate?.proposal || {};
  $("#ai-model").textContent = aiReview.reviewer_model || "—";
  $("#ai-evidence").textContent =
    aiReview.evidence || "AI review evidence가 없습니다.";
  $("#proposal-query").textContent = proposal.query || "—";

  const historyNode = $("#action-history");
  const history = candidate ? actionHistory(candidate) : [];
  historyNode.replaceChildren(
    ...history.map((action) => {
      const item = document.createElement("li");
      const fields = action.adjudicated_fields || {};
      const reviewer = action.review?.reviewer_id || "unknown";
      const frame =
        fields.event_type === "implicit_tool_request" &&
        Number.isInteger(fields.start_source_frame_idx) &&
        Number.isInteger(fields.end_source_frame_idx)
          ? `frame ${fields.start_source_frame_idx}–${fields.end_source_frame_idx}`
          : Number.isInteger(fields.source_frame_idx)
            ? `frame ${fields.source_frame_idx}`
            : "frame —";
      item.textContent =
        `${actionId(action) || "기록"} · ${statusLabel(action.review_status)} · ` +
        `${frame} · ${reviewer}`;
      return item;
    }),
  );
  if (!history.length) {
    const item = document.createElement("li");
    item.textContent = "사람 판정 이력이 없습니다.";
    historyNode.replaceChildren(item);
  }
}

function renderProgress() {
  // Provisional Phase context is navigable in final modes, but it is never an
  // interaction reference and must not change the confirmed/progress counts.
  const items = isFinalMode() ? finalInteractionItems() : sourceItems();
  const counts = { confirmed: 0, ambiguous: 0, rejected: 0 };
  items.forEach((candidate) => {
    const status = reviewStatus(candidate);
    if (status in counts) counts[status] += 1;
  });
  const reviewed = counts.confirmed + counts.ambiguous + counts.rejected;
  const total = items.length;
  const remaining = Math.max(0, total - reviewed);
  const clinical = clinicalReviewSummary();
  const displayedCounts = isFinalMode()
    ? counts
    : {
        confirmed: counts.confirmed + clinical.confirmed,
        ambiguous: counts.ambiguous + clinical.ambiguous,
        rejected: counts.rejected + clinical.rejected,
      };
  const progressReviewed = isFinalMode()
    ? reviewed
    : reviewed + clinical.reviewed;
  const progressTotal = isFinalMode() ? total : total + clinical.total;
  $("#confirmed-count").textContent = String(displayedCounts.confirmed);
  $("#ambiguous-count").textContent = String(displayedCounts.ambiguous);
  $("#rejected-count").textContent = String(displayedCounts.rejected);
  $("#remaining-count").textContent = state.clinical.loading
    ? `${isFinalMode() ? total : remaining} 이벤트 · 임상 …`
    : state.clinical.loadError
      ? `${isFinalMode() ? total : remaining} 이벤트 · 임상 !`
      : `${isFinalMode() ? total : remaining} 이벤트 · ${clinical.remaining} 임상`;
  $("#remaining-count").title = isFinalMode()
    ? `최종 수술 이벤트 ${total}건 · 미검토 임상 어노테이션 ${clinical.remaining}건`
    : `미검토 수술 이벤트 ${remaining}건 · 미검토 임상 어노테이션 ${clinical.remaining}건`;
  $("#clinical-event-count").textContent = state.clinical.loading
    ? "…"
    : state.clinical.loadError
      ? "!"
      : String(clinical.total);
  $("#clinical-track-filter").title = state.clinical.loadError
    ? `임상 어노테이션을 불러오지 못했습니다: ${state.clinical.loadError}`
    : `임상 어노테이션 ${clinical.total}건`;
  $("#review-progress-count").textContent =
    !isFinalMode() && state.clinical.loading
      ? `${reviewed} / …`
      : `${progressReviewed} / ${progressTotal}`;
  $("#review-progress-label").textContent = isFinalMode()
    ? state.viewMode === "final_dt"
      ? "최종 DT 평가본"
      : "평가 전 원시 관측"
    : state.clinical.loading
      ? "임상 검토 불러오는 중"
      : state.clinical.loadError
        ? "임상 검토 불러오기 실패"
        : progressTotal && progressReviewed === progressTotal
          ? "전체 검토 완료"
          : "전체 검토 진행률";
  const percent = progressTotal
    ? (progressReviewed / progressTotal) * 100
    : 0;
  $("#review-progress-fill").style.width = `${percent}%`;
  const bar = $(".progress-track");
  bar.setAttribute("aria-valuemax", String(progressTotal));
  bar.setAttribute("aria-valuenow", String(progressReviewed));
}

function interactionNavigatorListItem({ candidate, key, fields }) {
  const item = document.createElement("li");
  item.dataset.navigatorScope = "interaction";
  item.dataset.navigatorTimeSec = String(
    timeForFrame(fields.start_source_frame_idx),
  );
  const button = document.createElement("button");
  const status = reviewStatus(candidate);
  const track = TRACK_FOR_EVENT[fields.event_type];
  button.type = "button";
  button.className = "event-list-item";
  button.dataset.listCandidateId = key;
  button.setAttribute(
    "aria-current",
    String(
      state.activeInspector === "interaction" &&
        ["candidate", "human", "final"].includes(state.selected?.kind) &&
        state.selected.id === key,
    ),
  );
  button.addEventListener("click", () => selectCandidate(candidate));

  const icon = document.createElement("span");
  icon.className = "event-icon";
  icon.textContent =
    track === "transfer" ? "◆" : track === "phase" ? "▬" : "○";

  const text = document.createElement("span");
  text.className = "event-copy";
  const title = document.createElement("strong");
  title.textContent = eventTitle(candidate, fields);
  const detail = document.createElement("small");
  const startTime = timeForFrame(fields.start_source_frame_idx);
  const endTime = timeForFrame(fields.end_source_frame_idx);
  const contextMeta = finalMeta(candidate);
  const contextDetail = contextMeta.review_attention
    ? " · AI 재검토 애매 · 사람 확인 필요 · 정답 집계 제외"
    : " · 임시·애매 수술 단계 문맥 · 정답 집계 제외";
  detail.textContent =
    `${EVENT_LABELS[fields.event_type] || fields.event_type} · ` +
    (fields.event_type === "implicit_tool_request"
      ? `${formatTime(startTime)}–${formatTime(endTime)} · ` +
        `f${fields.start_source_frame_idx}–f${fields.end_source_frame_idx}`
      : `${formatTime(startTime)} · f${fields.source_frame_idx}`) +
    (isFinalMode()
      ? contextMeta.context_only
        ? contextDetail
        : ` · ${dispositionText(candidate)}`
      : "");
  text.append(title, detail);

  const result = document.createElement("span");
  const contextOnly = Boolean(contextMeta.context_only);
  result.className = `list-status ${status} ${
    contextOnly ? "context-only" : ""
  }`.trim();
  result.textContent = isFinalMode()
    ? contextOnly
      ? "?"
      : "▣"
    : statusSymbol(status);
  result.setAttribute(
    "aria-label",
    isFinalMode()
      ? contextOnly
        ? contextMeta.review_attention
          ? "읽기 전용 AI 재검토, 애매, 사람 확인 필요, 정답 집계 제외"
          : "읽기 전용 임시 수술 단계 문맥, 애매, 정답 집계 제외"
        : "읽기 전용 최종 이벤트"
      : statusLabel(status),
  );
  button.append(icon, text, result);
  item.append(button);
  return item;
}

function combinedNavigatorEntries({ actionableOnly = false } = {}) {
  const interactionEntries = sourceItems()
    .map((candidate, index) => {
      const fields = fieldsForCandidate(candidate);
      return {
        scope: "interaction",
        candidate,
        key: candidateKey(candidate, index),
        fields,
        timeSec: timeForFrame(fields.start_source_frame_idx),
      };
    })
    .filter(
      ({ candidate, fields }) =>
        state.filters[TRACK_FOR_EVENT[fields.event_type]] &&
        (!actionableOnly ||
          isFinalMode() ||
          reviewStatus(candidate) === "unreviewed"),
    );
  const clinicalEntries = state.filters.clinical
    ? state.clinical.items
        .filter(
          (item) =>
            !actionableOnly ||
            normalizeClinicalStatus(item.status) === "unreviewed",
        )
        .map((item) => ({
          scope: "clinical",
          item,
          key: item.id,
          timeSec: clinicalAnchorSec(item.annotation),
        }))
    : [];
  return [...interactionEntries, ...clinicalEntries].sort(
    (left, right) =>
      left.timeSec - right.timeSec ||
      (left.scope === right.scope
        ? left.key.localeCompare(right.key)
        : left.scope === "interaction"
          ? -1
          : 1),
  );
}

function renderEventList() {
  const allInteractionItems = sourceItems();
  const allAvailableCount =
    allInteractionItems.length + state.clinical.items.length;
  const items = combinedNavigatorEntries();

  $("#event-list-loading").hidden = true;
  $("#event-list-empty").hidden = items.length !== 0;
  const filteredEmpty = allAvailableCount > 0 && items.length === 0;
  $("#event-list-empty-title").textContent = filteredEmpty
    ? "선택한 Track에 이벤트가 없습니다"
    : state.clinical.loadError
      ? "표시할 수술 이벤트가 없습니다"
      : "표시할 이벤트나 임상 어노테이션이 없습니다";
  $("#event-list-empty-detail").textContent = filteredEmpty
    ? "숨긴 Track을 다시 표시하거나 아래 버튼으로 필터를 초기화하세요."
    : state.clinical.loadError
      ? `임상 데이터도 불러오지 못했습니다: ${state.clinical.loadError}`
      : "이 case에 연결된 이벤트와 임상 어노테이션을 확인해 주세요.";
  $("#reset-filters").hidden = !filteredEmpty;
  $("#reload-state").hidden = filteredEmpty;
  const list = $("#event-list");
  list.hidden = items.length === 0;
  list.replaceChildren(
    ...items.map((entry) =>
      entry.scope === "clinical"
        ? clinicalNavigatorListItem(entry.item)
        : interactionNavigatorListItem(entry),
    ),
  );
}

function timelineSpan() {
  return Math.max(0.001, timelineEnd() - timelineStart());
}

function timelineCanvasWidth() {
  return Math.max(1, timelineSpan() * state.pixelsPerSecond);
}

function trackLabelWidth() {
  return Number.parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue(
      "--track-label-width",
    ),
  ) || 126;
}

function timeToPixel(timeSec) {
  return (Number(timeSec) - timelineStart()) * state.pixelsPerSecond;
}

function clientXToTimelineTime(clientX) {
  const content = $("#timeline-content");
  const rect = content.getBoundingClientRect();
  // The content rect already includes the horizontal scroll offset.
  // Adding scrollLeft here a second time makes clicks drift after zoom/pan.
  const canvasX = clientX - rect.left - trackLabelWidth();
  return clamp(
    timelineStart() + canvasX / state.pixelsPerSecond,
    timelineStart(),
    timelineEnd(),
  );
}

function fitTimeline() {
  if (!state.data) return;
  const scrollWidth = $("#timeline-scroll").clientWidth;
  if (!scrollWidth) return;
  const available = Math.max(320, scrollWidth - trackLabelWidth());
  state.pixelsPerSecond = available / timelineSpan();
  state.fitTimeline = true;
  renderTimeline();
}

function zoomTimeline(factor, anchorClientX = null) {
  if (!state.data) return;
  const focusedControl = focusedEventControl();
  const scroll = $("#timeline-scroll");
  const scrollRect = scroll.getBoundingClientRect();
  const pointerOffset = clamp(
    anchorClientX === null
      ? scroll.clientWidth / 2
      : anchorClientX - scrollRect.left,
    trackLabelWidth(),
    scroll.clientWidth,
  );
  const anchorTime = clientXToTimelineTime(scrollRect.left + pointerOffset);
  const nextPixelsPerSecond = clamp(
    state.pixelsPerSecond * factor,
    4,
    160,
  );
  if (Math.abs(nextPixelsPerSecond - state.pixelsPerSecond) < 1e-7) return;
  state.pixelsPerSecond = nextPixelsPerSecond;
  state.fitTimeline = false;
  renderTimeline();
  const target =
    timeToPixel(anchorTime) + trackLabelWidth() - pointerOffset;
  scroll.scrollLeft = clamp(
    target,
    0,
    Math.max(0, scroll.scrollWidth - scroll.clientWidth),
  );
  restoreFocusedEventControl(focusedControl);
}

function rulerInterval() {
  const candidates = [1, 2, 5, 10, 15, 30, 60];
  return (
    candidates.find((value) => value * state.pixelsPerSecond >= 72) || 120
  );
}

function renderRuler() {
  const node = $("#time-ruler");
  const interval = rulerInterval();
  const first = Math.ceil(timelineStart() / interval) * interval;
  const ticks = [];
  for (let time = first; time <= timelineEnd() + 1e-9; time += interval) {
    const tick = document.createElement("span");
    tick.className = "ruler-tick";
    tick.style.left = `${timeToPixel(time)}px`;
    const label = document.createElement("strong");
    label.textContent = formatTime(time, false);
    tick.append(label);
    ticks.push(tick);
  }
  node.replaceChildren(...ticks);
}

function renderGaps() {
  const gapTrack = $("#gap-track");
  const overlay = $("#gap-overlay");
  const gapNodes = [];
  const overlayNodes = [];
  (state.data?.gaps || []).forEach((gap) => {
    const start = Number(gap.before_time_sec);
    const end = Number(gap.after_time_sec);
    const left = timeToPixel(start);
    const width = Math.max(2, timeToPixel(end) - left);

    const range = document.createElement("div");
    range.className = "gap-range";
    range.style.left = `${left}px`;
    range.style.width = `${width}px`;
    range.textContent = `${(end - start).toFixed(2)} s 공백`;
    gapNodes.push(range);

    const shade = document.createElement("div");
    shade.className = "gap-overlay-range";
    shade.style.left = `${trackLabelWidth() + left}px`;
    shade.style.width = `${width}px`;
    overlayNodes.push(shade);
  });
  if (timelineEnd() > visualEndTime() + 1e-7) {
    const start = visualEndTime();
    const end = timelineEnd();
    const left = timeToPixel(start);
    const width = Math.max(2, timeToPixel(end) - left);
    const range = document.createElement("div");
    range.className = "gap-range";
    range.style.left = `${left}px`;
    range.style.width = `${width}px`;
    range.textContent = "VIDEO OFF SCREEN";
    gapNodes.push(range);

    const shade = document.createElement("div");
    shade.className = "gap-overlay-range";
    shade.style.left = `${trackLabelWidth() + left}px`;
    shade.style.width = `${width}px`;
    overlayNodes.push(shade);
  }
  gapTrack.replaceChildren(...gapNodes);
  overlay.replaceChildren(...overlayNodes);
}

function markerClass(eventType, status, selected, draft = false) {
  const track = TRACK_FOR_EVENT[eventType] || "request";
  return [
    "event-marker",
    track,
    status !== "unreviewed" ? status : "",
    selected ? "selected" : "",
    draft ? "draft" : "",
  ]
    .filter(Boolean)
    .join(" ");
}

function requestIntervalForCandidate(candidate, index, fields, status, selected) {
  const startFrame = fields.start_source_frame_idx;
  const endFrame = fields.end_source_frame_idx;
  const startX = timeToPixel(timeForFrame(startFrame));
  const endX = timeToPixel(timeForFrame(endFrame));
  const interval = document.createElement("div");
  interval.className = [
    "request-interval",
    status !== "unreviewed" ? status : "",
    selected ? "selected" : "",
    selected && state.draft?.dirty ? "draft" : "",
    startFrame === endFrame ? "zero-range" : "",
  ]
    .filter(Boolean)
    .join(" ");
  interval.style.left = `${startX}px`;
  interval.style.width = `${Math.max(0, endX - startX)}px`;
  interval.dataset.candidateId = candidate
    ? candidateKey(candidate, index)
    : state.selected?.id || "new-request";
  interval.setAttribute(
    "aria-label",
    `암묵적 손 요청, ${formatTime(timeForFrame(startFrame))}부터 ` +
      `${formatTime(timeForFrame(endFrame))}까지, ${statusLabel(status)}`,
  );
  interval.title =
    `암묵적 손 요청 · frame ${startFrame}–${endFrame} · ${statusLabel(status)}`;

  const fill = document.createElement("span");
  fill.className = "request-interval-fill";
  interval.append(fill);

  ["start", "end"].forEach((boundary) => {
    const handle = document.createElement(isFinalMode() ? "span" : "button");
    if (!isFinalMode()) handle.type = "button";
    handle.className = `request-handle ${boundary}`;
    handle.dataset.boundary = boundary;
    handle.setAttribute(
      "aria-label",
      `요청 ${boundary === "start" ? "시작" : "종료"} frame ` +
        `${boundary === "start" ? startFrame : endFrame}`,
    );
    if (!isFinalMode()) {
      handle.addEventListener("pointerdown", (event) => {
        startMarkerDrag(event, candidate, boundary);
      });
      handle.addEventListener("click", (event) => {
        event.stopPropagation();
        if (state.suppressMarkerClick) {
          event.preventDefault();
          return;
        }
        if (candidate) {
          selectCandidate(candidate, { navigatorPlacement: "top" });
        }
      });
    } else {
      handle.setAttribute("aria-hidden", "true");
    }
    interval.append(handle);
  });

  interval.addEventListener("click", () => {
    if (candidate && !state.suppressMarkerClick) {
      selectCandidate(candidate, { navigatorPlacement: "top" });
    }
  });
  if (isFinalMode()) {
    interval.classList.add("read-only");
    interval.tabIndex = 0;
    interval.setAttribute("role", "button");
    interval.addEventListener("keydown", (event) => {
      if (!["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      if (candidate) {
        selectCandidate(candidate, { navigatorPlacement: "top" });
      }
    });
  }
  if (selected || state.pixelsPerSecond >= 42) {
    const label = document.createElement("span");
    label.className = "request-interval-label";
    label.textContent = `${formatTime(timeForFrame(startFrame))}–${formatTime(
      timeForFrame(endFrame),
    )}`;
    interval.append(label);
  }
  return interval;
}

function markerForCandidate(candidate, index) {
  const key = candidateKey(candidate, index);
  const selected =
    state.activeInspector === "interaction" &&
    ["candidate", "human", "final"].includes(state.selected?.kind) &&
    state.selected.id === key;
  const fields =
    selected && state.draft ? state.draft : fieldsForCandidate(candidate);
  const status = reviewStatus(candidate);
  if (fields.event_type === "implicit_tool_request") {
    return {
      marker: requestIntervalForCandidate(
        candidate,
        index,
        fields,
        status,
        selected,
      ),
      fields,
      key,
    };
  }
  const marker = document.createElement("button");
  marker.type = "button";
  marker.className = markerClass(
    fields.event_type,
    status,
    selected,
    selected && state.draft?.dirty,
  );
  marker.style.left = `${timeToPixel(timeForFrame(fields.source_frame_idx))}px`;
  marker.dataset.candidateId = key;
  marker.setAttribute(
    "aria-label",
    `${eventTitle(candidate, fields)}, ${formatTime(
      timeForFrame(fields.source_frame_idx),
    )}, ${statusLabel(status)}`,
  );
  marker.title =
    `${eventTitle(candidate, fields)} · frame ${fields.source_frame_idx} · ` +
    statusLabel(status);
  marker.addEventListener("click", (event) => {
    event.stopPropagation();
    if (state.suppressMarkerClick) {
      event.preventDefault();
      return;
    }
    selectCandidate(candidate, { navigatorPlacement: "top" });
  });
  if (!isFinalMode()) {
    marker.addEventListener("pointerdown", (event) => {
      startMarkerDrag(event, candidate);
    });
  } else {
    marker.classList.add("read-only");
  }

  if (selected || state.pixelsPerSecond >= 42) {
    const label = document.createElement("span");
    label.className = "event-marker-label";
    label.textContent = eventTitle(candidate, fields);
    marker.append(label);
  }
  return { marker, fields, key };
}

function ghostMarker(eventType, frameIndex) {
  const marker = document.createElement("span");
  marker.className =
    `${markerClass(eventType, "unreviewed", false)} original-ghost`;
  marker.style.left = `${timeToPixel(timeForFrame(frameIndex))}px`;
  marker.setAttribute("aria-hidden", "true");
  return marker;
}

function ghostRequestInterval(startFrame, endFrame) {
  const startX = timeToPixel(timeForFrame(startFrame));
  const endX = timeToPixel(timeForFrame(endFrame));
  const interval = document.createElement("span");
  interval.className = [
    "request-interval",
    "original-ghost",
    startFrame === endFrame ? "zero-range" : "",
  ]
    .filter(Boolean)
    .join(" ");
  interval.style.left = `${startX}px`;
  interval.style.width = `${Math.max(0, endX - startX)}px`;
  interval.setAttribute("aria-hidden", "true");
  const fill = document.createElement("span");
  fill.className = "request-interval-fill";
  const start = document.createElement("span");
  start.className = "request-handle start";
  const end = document.createElement("span");
  end.className = "request-handle end";
  interval.append(fill, start, end);
  return interval;
}

function renderPhaseIntervals(phaseEntries) {
  const canvas = $("#phase-track");
  const active = phaseEntries
    .filter(({ candidate }) => reviewStatus(candidate) !== "rejected")
    .sort(
      (left, right) =>
        left.fields.source_frame_idx - right.fields.source_frame_idx,
    );
  active.forEach((entry, index) => {
    const start = timeForFrame(entry.fields.source_frame_idx);
    const end =
      index + 1 < active.length
        ? timeForFrame(active[index + 1].fields.source_frame_idx)
        : timelineEnd();
    const interval = document.createElement("span");
    const provisional = Boolean(finalMeta(entry.candidate).context_only);
    interval.className = [
      "phase-interval",
      provisional ? "provisional-context" : "",
    ]
      .filter(Boolean)
      .join(" ");
    interval.style.left = `${timeToPixel(start)}px`;
    interval.style.width =
      `${Math.max(4, timeToPixel(end) - timeToPixel(start))}px`;
    const phaseLabel = phaseDisplayLabel(entry.fields.phase_id);
    interval.textContent =
      `${phaseLabel}${provisional ? " · 임시·애매" : ""}`;
    interval.title =
      `${phaseLabel} · ${formatTime(start)}–${formatTime(end)}` +
      (provisional ? " · provisional ambiguous · 정답 집계 제외" : "");
    canvas.append(interval);
  });
}

function updatePhaseCatalogCurrent(
  phaseEntries = activePhaseTimelineEntries(),
) {
  const current = currentPhaseTimelineEntry(phaseEntries);
  const currentPhaseId = current?.fields?.phase_id || "";
  const currentLabel = currentPhaseId
    ? phaseDisplayLabel(currentPhaseId)
    : "현재 영상 구간에 지정된 수술 단계 없음";
  const currentNode = $("#phase-catalog-current");
  if (currentNode) currentNode.textContent = `현재 · ${currentLabel}`;
  $$("[data-phase-catalog-id]").forEach((node) => {
    const isCurrent = node.dataset.phaseCatalogId === currentPhaseId;
    node.dataset.current = String(isCurrent);
    if (isCurrent) {
      node.setAttribute("aria-current", "step");
    } else {
      node.removeAttribute("aria-current");
    }
  });
}

function renderPhaseCatalog(
  phaseEntries = activePhaseTimelineEntries(),
) {
  const panel = $("#phase-catalog-panel");
  const list = $("#phase-catalog-list");
  if (!panel || !list) return;
  const phases = phaseCatalogEntries();
  panel.hidden = phases.length === 0;
  if (!phases.length) {
    list.replaceChildren();
    return;
  }
  if (panel.dataset.disclosureInitialized !== "true") {
    panel.open = !window.matchMedia(
      "(min-width: 1261px) and (max-height: 820px)",
    ).matches;
    panel.dataset.disclosureInitialized = "true";
  }

  const firstTimelineEntryByPhase = new Map();
  phaseEntries.forEach((entry) => {
    const phaseId = entry.fields.phase_id;
    if (phaseId && !firstTimelineEntryByPhase.has(phaseId)) {
      firstTimelineEntryByPhase.set(phaseId, entry);
    }
  });
  const labeledIds = [...firstTimelineEntryByPhase.keys()];
  $("#phase-catalog-count").textContent =
    `${phases.length}단계 · 이 영상 구간 ${labeledIds.length}개`;
  $("#phase-catalog-note").textContent = labeledIds.length
    ? `${labeledIds.join("–")}은 현재 영상에 구간 라벨이 있습니다. ` +
      "나머지는 전체 절차 순서 참고용이며 수술 단계 문맥은 평가 정답에 포함되지 않습니다."
    : "현재 영상에 지정된 수술 단계 구간이 없습니다. 전체 절차 순서 참고용 카탈로그입니다.";

  list.replaceChildren(
    ...phases.map((phase) => {
      const item = document.createElement("li");
      item.className = "phase-catalog-item";
      const timelineEntry = firstTimelineEntryByPhase.get(phase.phase_id);
      const card = document.createElement(timelineEntry ? "button" : "div");
      if (timelineEntry) card.type = "button";
      card.className = "phase-catalog-card";
      card.dataset.phaseCatalogId = phase.phase_id;
      card.dataset.labeled = String(Boolean(timelineEntry));
      card.dataset.observed = String(Boolean(phase.observed_in_case));

      const code = document.createElement("span");
      code.className = "phase-catalog-code";
      code.textContent = phase.phase_id;
      const name = document.createElement("strong");
      name.className = "phase-catalog-name";
      name.textContent =
        phase.name_ko || phase.name || "명칭 미지정";
      const status = document.createElement("small");
      status.className = "phase-catalog-status";
      status.textContent = timelineEntry
        ? "영상 구간"
        : phase.observed_in_case
          ? "영상 관측 · 구간 없음"
          : "절차 참고";
      card.append(code, name, status);

      if (timelineEntry) {
        const startTime = timeForFrame(
          timelineEntry.fields.source_frame_idx,
        );
        card.setAttribute(
          "aria-label",
          `${phaseDisplayLabel(phase.phase_id)} 시작 ${formatTime(startTime)}로 이동`,
        );
        card.title =
          `${phaseDisplayLabel(phase.phase_id)} · ${formatTime(startTime)}부터`;
        card.addEventListener("click", () => {
          selectCandidate(timelineEntry.candidate, {
            navigatorPlacement: "top",
          });
        });
      }
      item.append(card);
      return item;
    }),
  );
  updatePhaseCatalogCurrent(phaseEntries);
}

function speechMarker(event, index) {
  const key = speechEventKey(event, index);
  const selected =
    state.activeInspector === "interaction" &&
    state.selected?.kind === "speech" && state.selected.id === key;
  const timeSec = speechCompletionSec(event);
  const marker = document.createElement("button");
  marker.type = "button";
  marker.className = [
    "event-marker",
    "speech",
    "context-only",
    "read-only",
    selected ? "selected" : "",
  ]
    .filter(Boolean)
    .join(" ");
  marker.style.left = `${timeToPixel(
    Number.isFinite(timeSec) ? timeSec : timelineStart(),
  )}px`;
  marker.dataset.speechEventId = key;
  marker.setAttribute(
    "aria-label",
    `음성 문맥, 발화 완료 ${formatExactSpeechTime(timeSec)}, ${event?.text || "발화 원문 없음"}, 평가 정답 아님`,
  );
  marker.title =
    `${formatTime(timeSec)} 발화 완료 · ${event?.text || "발화 원문 없음"} · 문맥 전용`;
  marker.addEventListener("click", (clickEvent) => {
    clickEvent.stopPropagation();
    const pointerEvent = Number(clickEvent.detail) > 0;
    const nearest = pointerEvent
      ? nearestSpeechEventToTime(
          clientXToTimelineTime(clickEvent.clientX),
        )
      : null;
    selectSpeechEvent(nearest || event);
  });
  const kindSymbol = document.createElement("span");
  kindSymbol.className = "speech-marker-kind";
  kindSymbol.setAttribute("aria-hidden", "true");
  kindSymbol.append(createTimelineSemanticIcon("speaker"));
  marker.append(kindSymbol);
  if (selected || state.pixelsPerSecond >= 42) {
    const label = document.createElement("span");
    label.className = "event-marker-label speech-marker-label";
    label.textContent = event?.text || "발화";
    marker.append(label);
  }
  return marker;
}

function clinicalTimelineMarker(item) {
  const annotation =
    item.id === state.clinical.selectedId && state.clinical.draft
      ? state.clinical.draft
      : item.annotation;
  const anchor = clinicalAnchorSec(annotation);
  const evidenceStart = clinicalEvidenceStartSec(annotation);
  const evidenceEnd = clinicalEvidenceEndSec(annotation);
  const selected =
    state.activeInspector === "clinical" &&
    item.id === state.clinical.selectedId;
  const status = normalizeClinicalStatus(item.status);
  const marker = document.createElement("button");
  marker.type = "button";
  marker.className = [
    "clinical-timeline-marker",
    "point-marker",
    selected ? "selected" : "",
  ]
    .filter(Boolean)
    .join(" ");
  marker.dataset.clinicalAnnotationId = item.id;
  marker.dataset.status = status;
  marker.tabIndex =
    selected || (!state.clinical.selectedId && item === state.clinical.items[0])
      ? 0
      : -1;
  marker.setAttribute("aria-current", String(selected));
  marker.setAttribute(
    "aria-label",
    `임상 ${clinicalItemTitle(item)}, anchor ${formatTime(anchor)}, ` +
      `근거 ${formatClinicalRange(evidenceStart, evidenceEnd)}, ` +
      clinicalStatusLabel(status),
  );
  marker.title =
    `임상 · ${clinicalItemTitle(item)} · ` +
    `${formatClinicalRange(evidenceStart, evidenceEnd)} · ` +
    clinicalStatusLabel(status);

  marker.style.left = `${timeToPixel(anchor) - 22}px`;
  marker.style.width = "44px";

  const kindSymbol = document.createElement("span");
  kindSymbol.className = "clinical-marker-kind";
  kindSymbol.setAttribute("aria-hidden", "true");
  kindSymbol.append(createTimelineSemanticIcon("stethoscope"));
  const statusSymbol = document.createElement("span");
  statusSymbol.className = `clinical-marker-status status-${status}`;
  statusSymbol.textContent = clinicalStatusSymbol(status);
  statusSymbol.setAttribute("aria-hidden", "true");
  marker.append(kindSymbol, statusSymbol);
  marker.addEventListener("click", (event) => {
    event.stopPropagation();
    selectClinicalItem(item.id);
  });
  marker.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    event.stopPropagation();
    navigateCombinedItem(event.key === "ArrowLeft" ? -1 : 1);
  });
  return marker;
}

function renderClinicalTimelineTracks() {
  $("#clinical-track").replaceChildren();
  const visibleItems = state.filters.clinical
    ? state.clinical.items.map((item) => ({
        item,
        annotation:
          item.id === state.clinical.selectedId && state.clinical.draft
            ? state.clinical.draft
            : item.annotation,
      }))
    : [];
  $(`.timeline-track[data-track="clinical"]`).hidden =
    visibleItems.length === 0;
  visibleItems.forEach(({ item }) => {
    $("#clinical-track").append(clinicalTimelineMarker(item));
  });
  $$(".clinical-legend").forEach((node) => {
    node.hidden = visibleItems.length === 0;
  });
}

function renderTimeline() {
  if (!state.data) return;
  $(".gap-overlay").hidden = false;
  $$(".interaction-timeline-track").forEach((track) => {
    track.hidden = false;
  });
  const width = timelineCanvasWidth();
  document.documentElement.style.setProperty("--timeline-width", `${width}px`);
  $("#timeline-content").style.width =
    `${trackLabelWidth() + width}px`;

  renderRuler();
  renderGaps();
  ["request", "transfer", "phase", "speech"].forEach((track) => {
    $(`#${track}-track`).replaceChildren();
    const hiddenForMode =
      (!isFinalMode() && track === "speech") ||
      (track === "speech" && !speechContextTrack()) ||
      (isFinalMode() && track === "phase" && !phaseContextTrack());
    $(`.timeline-track[data-track="${track}"]`).hidden =
      !state.filters[track] || hiddenForMode;
  });

  const phaseEntries = [];
  sourceItems().forEach((candidate, index) => {
    const { marker, fields } = markerForCandidate(candidate, index);
    const track = TRACK_FOR_EVENT[fields.event_type] || "request";
    if (!state.filters[track]) return;
    const canvas = $(`#${track}-track`);
    const original = originalIntervalForCandidate(candidate, fields.event_type);
    if (
      original.startFrame !== fields.start_source_frame_idx ||
      original.endFrame !== fields.end_source_frame_idx
    ) {
      canvas.append(
        fields.event_type === "implicit_tool_request"
          ? ghostRequestInterval(original.startFrame, original.endFrame)
          : ghostMarker(fields.event_type, original.startFrame),
      );
    }
    canvas.append(marker);
    if (fields.event_type === "phase_start") {
      phaseEntries.push({ candidate, fields });
    }
  });

  if (state.selected?.kind === "new" && state.draft) {
    const track = TRACK_FOR_EVENT[state.draft.event_type] || "request";
    if (state.filters[track]) {
      if (state.draft.event_type === "implicit_tool_request") {
        const interval = requestIntervalForCandidate(
          null,
          0,
          state.draft,
          "unreviewed",
          true,
        );
        $(`#${track}-track`).append(interval);
      } else {
        const marker = document.createElement("button");
        marker.type = "button";
        marker.className = markerClass(
          state.draft.event_type,
          "unreviewed",
          true,
          true,
        );
        marker.style.left =
          `${timeToPixel(timeForFrame(state.draft.source_frame_idx))}px`;
        marker.setAttribute("aria-label", "새 사람 이벤트 draft");
        marker.addEventListener("pointerdown", (event) => {
          startMarkerDrag(event, null);
        });
        marker.addEventListener("click", (event) => event.stopPropagation());
        $(`#${track}-track`).append(marker);
      }
    }
  }

  if (isFinalMode() && state.filters.speech && speechContextTrack()) {
    $("#speech-track").append(
      ...speechEvents().map((event, index) => speechMarker(event, index)),
    );
  }

  if (state.filters.phase) renderPhaseIntervals(phaseEntries);
  renderPhaseCatalog(phaseEntries);
  renderClinicalTimelineTracks();
  updatePlayhead();
}

function focusedEventControl() {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) return null;
  if (active.matches("[data-clinical-annotation-id]")) {
    return {
      scope: active.closest("#event-list")
        ? "clinical-list"
        : "clinical-timeline",
      clinicalAnnotationId: active.dataset.clinicalAnnotationId,
    };
  }
  if (active.matches("[data-speech-event-id]")) {
    return {
      scope: "speech",
      speechEventId: active.dataset.speechEventId,
    };
  }
  if (active.matches(".event-list-item")) {
    return {
      scope: "list",
      candidateId: active.dataset.listCandidateId,
    };
  }
  const marker = active.closest("[data-candidate-id]");
  if (!marker) return null;
  return {
    scope: "timeline",
    candidateId: marker.dataset.candidateId,
    boundary: active.dataset.boundary || null,
  };
}

function restoreFocusedEventControl(descriptor) {
  if (descriptor?.clinicalAnnotationId) {
    const selector =
      descriptor.scope === "clinical-list"
        ? `#event-list [data-clinical-annotation-id="${CSS.escape(
            descriptor.clinicalAnnotationId,
          )}"]`
        : `.clinical-timeline-marker[data-clinical-annotation-id="${CSS.escape(
            descriptor.clinicalAnnotationId,
          )}"]`;
    $(selector)?.focus({ preventScroll: true });
    return;
  }
  if (descriptor?.scope === "speech" && descriptor.speechEventId) {
    $$("[data-speech-event-id]")
      .find((node) => node.dataset.speechEventId === descriptor.speechEventId)
      ?.focus({ preventScroll: true });
    return;
  }
  if (!descriptor?.candidateId) return;
  const candidates =
    descriptor.scope === "list"
      ? $$(".event-list-item")
      : $$("[data-candidate-id]");
  const container = candidates.find((node) => {
    const id =
      descriptor.scope === "list"
        ? node.dataset.listCandidateId
        : node.dataset.candidateId;
    return id === descriptor.candidateId;
  });
  if (!container) return;
  const target = descriptor.boundary
    ? container.querySelector(`[data-boundary="${descriptor.boundary}"]`)
    : container;
  target?.focus({ preventScroll: true });
}

function alignNavigatorCandidateToTop(candidateId) {
  if (!candidateId) return false;
  const navigator = $(".navigator-panel");
  const item = $$(".event-list-item").find(
    (node) => node.dataset.listCandidateId === candidateId,
  );
  if (!navigator || !item) return false;
  const navigatorRect = navigator.getBoundingClientRect();
  const itemRect = item.getBoundingClientRect();
  const paddingTop =
    Number.parseFloat(getComputedStyle(navigator).paddingTop) || 0;
  const targetScrollTop =
    navigator.scrollTop + itemRect.top - navigatorRect.top - paddingTop;
  const list = $("#event-list");
  const currentMaxScrollTop =
    navigator.scrollHeight - navigator.clientHeight;
  const missingRunway = targetScrollTop - currentMaxScrollTop;
  if (list && missingRunway > 0.5) {
    // Give the last few events enough trailing space to reach the literal top.
    const currentPaddingBottom =
      Number.parseFloat(getComputedStyle(list).paddingBottom) || 0;
    list.style.paddingBottom =
      `${currentPaddingBottom + missingRunway}px`;
  }
  navigator.scrollTop = clamp(
    targetScrollTop,
    0,
    Math.max(0, navigator.scrollHeight - navigator.clientHeight),
  );
  return true;
}

function renderClinicalOverlay() {
  const region = $("#clinical-event-overlay");
  if (
    !region ||
    !state.overlayEnabled ||
    state.activeInspector !== "clinical" ||
    !state.clinical.draft ||
    !selectedClinicalItem()
  ) {
    if (region) {
      region.replaceChildren();
      region.hidden = true;
    }
    return;
  }
  const annotation = state.clinical.draft;
  const item = selectedClinicalItem();
  const observation = String(annotation.observation || "").trim();
  const interpretation = String(annotation.interpretation || "").trim();

  const card = document.createElement("article");
  card.className = "event-overlay-card clinical-evidence-card";
  const badge = document.createElement("span");
  badge.className = "event-overlay-badge";
  badge.textContent =
    normalizeClinicalStatus(item.status) === "unreviewed"
      ? "임상 라벨 · AI 초안 · 사람 검수 필요"
      : `임상 라벨 · ${clinicalStatusLabel(item.status)}`;
  const title = document.createElement("strong");
  title.textContent = observation || "관찰 내용을 확인하세요.";
  const detail = document.createElement("p");
  detail.textContent =
    (interpretation ? `해석 · ${interpretation}` : "") ||
    `${formatClinicalRange(
      clinicalEvidenceStartSec(annotation),
      clinicalEvidenceEndSec(annotation),
    )} 근거를 확인하세요.`;
  card.append(badge, title, detail);
  if (interpretation) {
    const boundary = document.createElement("small");
    boundary.className = "clinical-overlay-boundary";
    boundary.textContent = "임상 해석은 사람 확정 전 정답이 아닙니다.";
    card.append(boundary);
  }
  region.replaceChildren(card);
  region.hidden = false;
}

function renderSelectionChrome() {
  const clinical = state.activeInspector === "clinical";
  $("#interaction-inspector").hidden = clinical;
  $("#clinical-inspector").hidden = !clinical;
  $("#auto-pause-control").hidden = clinical || isFinalMode();
  $("#reviewer-control").hidden = !clinical && isFinalMode();
  $("#reviewer-id").disabled =
    isAnySaving() || (!clinical && isFinalMode());
  $("#new-event").hidden = isFinalMode();
  $("#candidate-alert").hidden = clinical || $("#candidate-alert").hidden;
  $("#previous-candidate").textContent = "이전 항목";
  $("#next-candidate").textContent = "다음 항목";
  $("#previous-candidate").setAttribute(
    "aria-label",
    isFinalMode()
      ? "이전 수술 이벤트 또는 미검토 임상 어노테이션"
      : "이전 미검토 수술 이벤트 또는 임상 어노테이션",
  );
  $("#next-candidate").setAttribute(
    "aria-label",
    isFinalMode()
      ? "다음 수술 이벤트 또는 미검토 임상 어노테이션"
      : "다음 미검토 수술 이벤트 또는 임상 어노테이션",
  );
  $("#event-overlay-stack").setAttribute(
    "aria-label",
    "동기화 수술 영상과 주변의 수술 단계, 음성, 도구 요청, 도구 이동, 임상 근거 알림",
  );
  [
    "#phase-event-overlay",
    "#speech-event-overlay",
    "#request-event-overlay",
    "#transfer-event-overlay",
  ].forEach((selector) => {
    const region = $(selector);
    if (region) region.hidden = false;
  });
  $("#case-selector").disabled =
    isAnySaving() ||
    !state.data?.case_selector_enabled ||
    $("#case-selector").options.length < 2;
  if (!clinical) $("#clinical-event-overlay").hidden = true;
}

function renderModeChrome() {
  const finalAvailable = Boolean(state.finalReview?.available);
  const speechTrack = speechContextTrack();
  const speechAvailable = Boolean(speechTrack);
  const phaseTrack = phaseContextTrack();
  const phaseAvailable = Boolean(phaseTrack);
  $$("[data-review-mode]").forEach((button) => {
    const mode = button.dataset.reviewMode;
    button.setAttribute("aria-pressed", String(mode === state.viewMode));
    button.disabled = mode !== "edit" && !finalAvailable;
  });
  const final = isFinalMode();
  document.documentElement.dataset.reviewMode = state.viewMode;
  $("#workspace-kicker").textContent =
    state.viewMode === "final_dt"
      ? "EVALUATION REFERENCE · READ-ONLY FINAL"
      : state.viewMode === "final_observed"
        ? "SOURCE OBSERVATIONS · BEFORE DT PROJECTION"
        : "EVALUATION REFERENCE · HUMAN GATE";
  $("#auto-pause-control").hidden = final;
  $("#reviewer-control").hidden = final;
  $("#new-event").hidden = final;
  $("#new-event-empty").hidden = final;
  $$("[data-edit-legend]").forEach((node) => {
    node.hidden = final;
  });
  $("#final-legend").hidden = !final;
  $("#speech-legend").hidden = !final || !speechAvailable;
  $("#phase-context-legend").hidden = !final || !phaseAvailable;
  const phaseLabel = $('[data-track-filter="phase"]')?.closest("label");
  if (phaseLabel) phaseLabel.hidden = final && !phaseAvailable;
  $("#speech-track-filter").hidden = !final || !speechAvailable;
  $("#speech-event-count").textContent = String(
    Number.isFinite(Number(speechTrack?.event_count))
      ? Number(speechTrack.event_count)
      : speechEvents().length,
  );
  $("#timeline-title").textContent =
    state.viewMode === "final_dt"
      ? "최종 DT 평가본"
      : state.viewMode === "final_observed"
        ? "원시 관측 참고 · 평가 전"
        : "관측 이벤트";
  $("#navigator-title").textContent =
    state.viewMode === "final_dt"
      ? phaseAvailable
        ? "최종 DT · 임시 수술 단계 · 임상"
        : "최종 DT 이벤트 · 임상"
      : state.viewMode === "final_observed"
        ? phaseAvailable
          ? "원시 관측 · 임시 수술 단계 · 임상"
          : "원시 관측 · 임상"
        : "이벤트 · 임상 탐색";
  $("#inspector-title").textContent =
    state.viewMode === "final_dt"
      ? "최종 DT 이벤트 근거"
      : state.viewMode === "final_observed"
        ? "원시 관측과 DT 처리"
        : "이벤트 판정";
  if (final) {
    $("#final-mode-banner-title").textContent =
      state.viewMode === "final_dt"
        ? "Taskplanner 최종 평가본"
        : "원시 관측 참고본 · 평가 입력 아님";
    $("#final-mode-banner-detail").textContent =
      state.viewMode === "final_dt"
        ? "스크럽 정리는 제외하고 연속 반환은 집도의 → 메이요로 축약했습니다."
        : "스크럽 정리와 축약 전 중간 전이를 포함한 감사용 관측입니다.";
  }
  $("#previous-candidate").textContent = final ? "이전 이벤트" : "이전";
  $("#next-candidate").textContent = final ? "다음 이벤트" : "다음";
  $("#previous-candidate").setAttribute(
    "aria-label",
    final ? "이전 최종 이벤트" : "이전 미검토 후보",
  );
  $("#next-candidate").setAttribute(
    "aria-label",
    final ? "다음 최종 이벤트" : "다음 미검토 후보",
  );
}

function reviewModeFromUrl() {
  const raw = new URLSearchParams(window.location.search).get("mode");
  const aliases = {
    "final-observed": "final_observed",
    "final-dt": "final_dt",
  };
  const normalized = aliases[raw] || raw;
  return REVIEW_MODES.has(normalized) ? normalized : null;
}

function updateReviewModeUrl(mode) {
  const url = new URL(window.location.href);
  url.searchParams.set(
    "mode",
    mode === "final_dt"
      ? "final-dt"
      : mode === "final_observed"
        ? "final-observed"
        : "edit",
  );
  window.history.replaceState({}, "", url);
}

function setReviewMode(mode, { announce = true, updateUrl = true } = {}) {
  if (!REVIEW_MODES.has(mode) || mode === state.viewMode) return true;
  if (mode !== "edit" && !state.finalReview?.available) {
    toast("최종 참조를 불러오지 못했습니다.");
    return false;
  }
  if (!guardAnyNavigation()) return false;
  pausePlayback();
  state.viewMode = mode;
  state.activeInspector = "interaction";
  state.selected = null;
  state.draft = null;
  state.selectionBaseline = null;
  setClinicalSelectionState(null);
  state.pointOverlayExpiry.clear();
  state.overlayFingerprint = null;
  state.overlayKeys.clear();
  $("#candidate-alert").hidden = true;
  if (updateUrl) {
    updateReviewModeUrl(mode);
  }
  renderModeChrome();
  renderAll();
  updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
    reason: "seek",
  });
  if (announce) {
    toast(
      mode === "final_dt"
        ? "최종 DT 읽기 전용 평가본을 열었습니다."
        : mode === "final_observed"
          ? "평가 전 원시 관측 참고본을 열었습니다."
          : "편집 검수 모드로 돌아왔습니다.",
    );
  }
  return true;
}

function renderAll({ navigatorCandidateAtTop = null } = {}) {
  const focusedControl = focusedEventControl();
  const navigator = $(".navigator-panel");
  const navigatorScrollTop = navigator?.scrollTop || 0;
  renderModeChrome();
  renderProgress();
  renderEventList();
  renderInspector();
  renderClinicalInspector();
  renderSelectionChrome();
  renderTimeline();
  renderClinicalOverlay();
  const alignedToCandidate =
    navigatorCandidateAtTop &&
    alignNavigatorCandidateToTop(navigatorCandidateAtTop);
  if (navigator && !alignedToCandidate) {
    navigator.scrollTop = clamp(
      navigatorScrollTop,
      0,
      Math.max(0, navigator.scrollHeight - navigator.clientHeight),
    );
  }
  restoreFocusedEventControl(focusedControl);
}

function activeCaseId() {
  return String(
    state.data?.active_case_id ||
      state.data?.case_id ||
      requestedCaseFromUrl() ||
      "",
  );
}

async function loadPhaseCatalog(caseId) {
  const sequence = ++state.phaseCatalogLoadSequence;
  state.phaseCatalog = null;
  const embedded = state.finalReview?.context_tracks?.phase?.catalog;
  if (embedded && Array.isArray(embedded.phases)) {
    state.phaseCatalog = embedded;
    return;
  }
  if (!caseId) return;
  try {
    const response = await fetch(
      apiUrl(`/phase_catalogs/${encodeURIComponent(caseId)}.json`),
      { cache: "no-store" },
    );
    if (response.status === 404) return;
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(
        payload.error || "수술 단계 카탈로그를 불러오지 못했습니다.",
      );
    }
    if (
      sequence !== state.phaseCatalogLoadSequence ||
      String(payload.case_id || "") !== String(caseId) ||
      !Array.isArray(payload.phases)
    ) {
      return;
    }
    state.phaseCatalog = payload;
  } catch (error) {
    if (sequence !== state.phaseCatalogLoadSequence) return;
    state.phaseCatalog = null;
    console.warn("Phase catalog unavailable", error);
  }
}

function setClinicalSelectionState(item) {
  if (!item) {
    state.clinical.selectedId = null;
    state.clinical.draft = null;
    state.clinical.baseline = null;
    state.clinical.reviewNotes = "";
    return;
  }
  state.clinical.selectedId = item.id;
  state.clinical.draft = clinicalClone(item.annotation);
  state.clinical.reviewNotes = String(
    item.review?.review?.notes ||
      item.review?.notes ||
      item.review?.review_notes ||
      "",
  );
  state.clinical.baseline = clinicalSnapshotSignature(
    state.clinical.draft,
    state.clinical.reviewNotes,
  );
}

async function loadClinicalState({ preserveSelection = true } = {}) {
  if (state.clinical.loading) return;
  const previousId =
    preserveSelection && state.activeInspector === "clinical"
      ? state.clinical.selectedId
      : null;
  state.clinical.loading = true;
  state.clinical.loadError = null;
  renderAll();
  try {
    const response = await fetch(apiUrl("/api/clinical-review"), {
      cache: "no-store",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(
        payload?.error ||
          payload?.message ||
          "임상 검수 데이터를 불러오지 못했습니다.",
      );
    }
    if (!clinicalIsObject(payload) || payload.ok === false) {
      throw new Error(
        payload?.error || "임상 검수 응답 형식이 올바르지 않습니다.",
      );
    }
    const expectedCase = activeCaseId();
    if (
      expectedCase &&
      payload.case_id &&
      String(payload.case_id) !== expectedCase
    ) {
      throw new Error(
        `요청 case ${expectedCase}와 임상 응답 case ${payload.case_id}가 다릅니다.`,
      );
    }
    state.clinical.data = payload;
    state.clinical.items = buildClinicalItems();
    const selection =
      previousId
        ? state.clinical.items.find((item) => item.id === previousId) || null
        : null;
    setClinicalSelectionState(selection);
    if (!selection && state.activeInspector === "clinical") {
      state.activeInspector = "interaction";
    }
  } catch (error) {
    state.clinical.data = null;
    state.clinical.items = [];
    setClinicalSelectionState(null);
    state.clinical.loadError = error.message;
    if (state.activeInspector === "clinical") {
      state.activeInspector = "interaction";
    }
  } finally {
    state.clinical.loading = false;
    renderAll();
    requestAnimationFrame(fitTimeline);
  }
}

function setClinicalSaving(saving) {
  state.clinical.saving = saving;
  $("#clinical-confirm").disabled = saving;
  $("#clinical-ambiguous").disabled = saving;
  $("#clinical-reject").disabled = saving;
  $("#clinical-discard-draft").disabled =
    saving || !hasDirtyClinicalDraft();
  $("#clinical-reviewer-role").disabled =
    saving || state.clinical.viewMode === "final";
  $("#reviewer-id").disabled =
    saving ||
    (state.activeInspector !== "clinical" && isFinalMode());
  $("#clinical-form")
    .querySelectorAll("input, select, textarea")
    .forEach((control) => {
      control.disabled =
        saving || state.clinical.viewMode === "final";
    });
  renderSelectionChrome();
}

function stripClinicalReviewMetadata(annotation) {
  const clean = clinicalClone(annotation);
  delete clean._clinical_review;
  delete clean._review_ui;
  delete clean.candidate_sha256;
  return clean;
}

function clinicalClientRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `clinical-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function saveClinicalReview(reviewStatus) {
  if (
    state.activeInspector !== "clinical" ||
    state.clinical.viewMode !== "draft" ||
    state.clinical.saving
  ) {
    return;
  }
  const item = selectedClinicalItem();
  if (!item || !state.clinical.draft) {
    toast("먼저 임상 후보를 선택해 주세요.");
    return;
  }
  syncClinicalDraftFromForm();
  const validationError = validateClinicalForm();
  if (validationError) {
    validationError.control.setAttribute("aria-invalid", "true");
    $("#clinical-form-error").textContent = validationError.message;
    $("#clinical-form-error").hidden = false;
    validationError.control.focus();
    return;
  }
  const reviewerId = $("#reviewer-id").value.trim();
  if (!reviewerId) {
    $("#reviewer-id").setAttribute("aria-invalid", "true");
    $("#clinical-form-error").textContent =
      "검토자 ID를 입력한 뒤 임상 판정을 저장해 주세요.";
    $("#clinical-form-error").hidden = false;
    $("#reviewer-id").focus();
    return;
  }
  const reviewerRole = Object.hasOwn(
    CLINICAL_REVIEWER_ROLE_LABELS,
    $("#clinical-reviewer-role").value,
  )
    ? $("#clinical-reviewer-role").value
    : "clinical_reviewer";
  if (!item.digest) {
    $("#clinical-form-error").textContent =
      "후보별 SHA-256이 없어 저장할 수 없습니다. 임상 상태를 다시 불러와 주세요.";
    $("#clinical-form-error").hidden = false;
    return;
  }
  const start = clinicalEvidenceStartSec(state.clinical.draft);
  const anchor = clinicalAnchorSec(state.clinical.draft);
  const end = clinicalEvidenceEndSec(state.clinical.draft);
  if (!(start <= anchor && anchor <= end)) {
    $("#clinical-form-error").textContent =
      "근거 구간은 시작 ≤ anchor ≤ 종료 순서여야 합니다.";
    $("#clinical-form-error").hidden = false;
    return;
  }

  $("#clinical-form-error").hidden = true;
  setClinicalSaving(true);
  const body = {
    case_id: activeCaseId(),
    revision: state.clinical.data?.revision,
    annotation_id: item.id,
    candidate_sha256: item.digest,
    review_status: reviewStatus,
    reviewer_id: reviewerId,
    reviewer_role: reviewerRole,
    notes: state.clinical.reviewNotes.trim(),
    adjudicated_annotation: stripClinicalReviewMetadata(
      state.clinical.draft,
    ),
    client_request_id: clinicalClientRequestId(),
  };
  const supersedes =
    item.review?.action_id ||
    item.review?.id ||
    item.review?.review_action_id ||
    "";
  if (supersedes) body.supersedes_action_id = supersedes;

  try {
    const response = await fetch(apiUrl("/api/clinical-action"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(
        response.status === 409
          ? "다른 검토가 먼저 저장되어 revision이 바뀌었습니다. 다시 불러온 뒤 재검토해 주세요."
          : payload?.error ||
              payload?.message ||
              "임상 판정을 저장하지 못했습니다.",
      );
    }
    const nextState = clinicalIsObject(payload.state)
      ? payload.state
      : null;
    if (nextState) {
      if (
        nextState.case_id &&
        String(nextState.case_id) !== activeCaseId()
      ) {
        throw new Error("저장 응답의 case가 현재 영상과 다릅니다.");
      }
      state.clinical.data = nextState;
    } else {
      const refresh = await fetch(apiUrl("/api/clinical-review"), {
        cache: "no-store",
      });
      const refreshedState = await refresh.json().catch(() => ({}));
      if (!refresh.ok) {
        throw new Error(
          refreshedState?.error || "저장 후 임상 상태를 갱신하지 못했습니다.",
        );
      }
      state.clinical.data = refreshedState;
    }
    state.clinical.items = buildClinicalItems();
    const selected =
      state.clinical.items.find((entry) => entry.id === item.id) ||
      state.clinical.items[0] ||
      null;
    setClinicalSelectionState(selected);
    renderAll();
    toast(
      `${clinicalStatusLabel(
        reviewStatus,
      )} 판정을 append-only 임상 검수 이력에 저장했습니다.`,
    );
  } catch (error) {
    $("#clinical-form-error").textContent = error.message;
    $("#clinical-form-error").hidden = false;
  } finally {
    setClinicalSaving(false);
  }
}

function overlaySourceItems() {
  return sourceItems().filter((candidate) => {
    const fields = fieldsForCandidate(candidate);
    if (!state.filters[TRACK_FOR_EVENT[fields.event_type]]) return false;
    const status = reviewStatus(candidate);
    return status !== "rejected" && (isFinalMode() || status !== "unreviewed");
  });
}

function fieldsForOverlay(candidate) {
  if (
    !isFinalMode() &&
    state.draft &&
    state.selected?.id === candidateKey(candidate)
  ) {
    return state.draft;
  }
  return fieldsForCandidate(candidate);
}

function overlayBadge(candidate, { interval = false, reason = "playback" } = {}) {
  if (interval) {
    if (state.viewMode === "final_dt") return "최종 DT · 요청 중";
    if (state.viewMode === "final_observed") return "원시 관측 · 요청 중";
    const status = reviewStatus(candidate);
    return status === "unreviewed"
      ? "AI 후보 · 요청 중"
      : status === "ambiguous"
        ? "애매 · 요청 중"
        : "확정 · 요청 중";
  }
  const occurrence = reason === "playback" ? "방금 발생" : "이 시점";
  const kind =
    finalMeta(candidate).disposition?.kind ||
    finalMeta(candidate).disposition?.operation ||
    "identity";
  if (state.viewMode === "final_dt") {
    return kind === "collapsed_output"
      ? `최종 DT · 축약 결과 · ${occurrence}`
      : `최종 DT 이벤트 · ${occurrence}`;
  }
  if (state.viewMode === "final_observed") {
    const prefixes = {
      excluded_cleanup: "관측됨 · DT 제외",
      excluded_unclosed: "관측됨 · DT 비채점",
      excluded_unclosed_direct_return: "관측됨 · DT 비채점",
      collapse_source: "관측됨 · DT 축약 연쇄",
      collapsed_source: "관측됨 · DT 축약 연쇄",
      collapsed_output: "관측됨 · DT 축약 도착",
      identity: "원시 관측",
    };
    return `${prefixes[kind] || "원시 관측"} · ${occurrence}`;
  }
  const status = reviewStatus(candidate);
  return `${
    status === "unreviewed"
      ? "AI 후보"
      : status === "ambiguous"
        ? "애매"
        : "사람 확정"
  } · ${occurrence}`;
}

function interactionOverlayCard(
  candidate,
  { interval = false, reason = "playback" } = {},
) {
  const fields = fieldsForOverlay(candidate);
  const card = document.createElement("article");
  const dispositionKind =
    finalMeta(candidate).disposition?.kind ||
    finalMeta(candidate).disposition?.operation ||
    "identity";
  card.className = [
    "event-overlay-card",
    interval ? "request-active" : "transfer-point",
    isFinalMode() ? "final-event" : "",
    dispositionKind.startsWith("excluded") ? "excluded-event" : "",
    dispositionKind.includes("collapse") ? "collapsed-event" : "",
  ]
    .filter(Boolean)
    .join(" ");

  const badge = document.createElement("span");
  badge.className = "event-overlay-badge";
  badge.textContent = overlayBadge(candidate, { interval, reason });

  const title = document.createElement("strong");
  title.textContent = interval
    ? "암묵적 손 요청"
    : `${humanizeIdentifier(fields.tool)} 이동`;

  const detail = document.createElement("p");
  detail.textContent = interval
    ? `손바닥을 펼쳐 도구 수령 자세를 유지 중 · ` +
      `f${fields.start_source_frame_idx}–f${fields.end_source_frame_idx}`
    : `${humanizeIdentifier(fields.from)} → ${humanizeIdentifier(fields.to)} · ` +
      `f${fields.source_frame_idx}`;
  card.append(badge, title, detail);
  return card;
}

function currentPhaseContext() {
  if (!state.filters.phase) {
    return null;
  }
  const events = isFinalMode()
    ? phaseContextEvents()
    : sourceItems().filter((candidate) => {
        const fields = fieldsForOverlay(candidate);
        const status = reviewStatus(candidate);
        return (
          fields.event_type === "phase_start" &&
          status !== "rejected" &&
          status !== "unreviewed"
        );
      });
  return (
    events
      .filter(
        (event) => {
          const frame = fieldsForOverlay(event).source_frame_idx;
          return (
            timeForFrame(frame) <=
            state.currentTimeSec + PHASE_BOUNDARY_TIME_EPSILON_SEC
          );
        },
      )
      .sort(
        (left, right) =>
          fieldsForOverlay(right).source_frame_idx -
          fieldsForOverlay(left).source_frame_idx,
      )[0] || null
  );
}

function speechAvailabilitySec(event) {
  const value = Number(
    event?.available_sec ??
      event?._review_ui?.complete_text_available_sec ??
      event?.time_sec,
  );
  return Number.isFinite(value) ? value : Number(event?.time_sec || 0);
}

function speechOverlayStage(event, timeSec = state.currentTimeSec) {
  const now = Number(timeSec);
  const endSec = Number(event?.end_sec);
  const availableSec = speechAvailabilitySec(event);
  if (now + 1e-7 >= availableSec) return "text_available";
  if (Number.isFinite(endSec) && now <= endSec + 1e-7) return "speaking";
  return "awaiting_text";
}

function currentSpeechContext() {
  if (!state.filters.speech || !speechContextTrack()) {
    return null;
  }
  const now = Number(state.currentTimeSec);
  const holdSec = 2.5;
  return (
    speechEvents()
      .filter((event) => {
        const startSec = Number(event?.time_sec);
        const endSec = Math.max(startSec, Number(event?.end_sec));
        const availableSec = speechAvailabilitySec(event);
        return (
          Number.isFinite(startSec) &&
          Number.isFinite(endSec) &&
          now + 1e-7 >= startSec &&
          now <= Math.max(endSec, availableSec) + holdSec + 1e-7
        );
      })
      .sort(
        (left, right) =>
          Number(right?.time_sec) - Number(left?.time_sec) ||
          speechAvailabilitySec(right) - speechAvailabilitySec(left),
      )[0] || null
  );
}

function phaseOverlayCard(event) {
  const fields = fieldsForCandidate(event);
  const card = document.createElement("article");
  card.className =
    "event-overlay-card phase-current provisional-context";
  const badge = document.createElement("span");
  badge.className = "event-overlay-badge";
  badge.textContent = "임시 수술 단계 · 애매 · 정답 아님";
  const title = document.createElement("strong");
  title.textContent = phaseDisplayLabel(fields.phase_id);
  const detail = document.createElement("p");
  detail.textContent =
    `${humanizeIdentifier(event?.phase_boundary_kind)} · ` +
    `${formatTime(timeForFrame(fields.source_frame_idx))}부터`;
  card.append(badge, title, detail);
  return card;
}

function speechOverlayCard(
  event,
  stage = speechOverlayStage(event),
) {
  const textAvailable = stage === "text_available";
  const speaking = stage === "speaking";
  const card = document.createElement("article");
  card.className = [
    "event-overlay-card",
    "speech-utterance",
    "context-only",
    `speech-${stage}`,
  ].join(" ");
  const badge = document.createElement("span");
  badge.className = "event-overlay-badge";
  badge.textContent = textAvailable
    ? "음성 발화 문맥 · 정답 아님"
    : speaking
      ? "음성 발화 중 · 정답 아님"
      : "음성 텍스트 처리 중 · 정답 아님";
  const title = document.createElement("strong");
  title.textContent = textAvailable
    ? event?.text || "발화 원문 없음"
    : speaking
      ? "음성 발화 중"
      : "발화 종료 · 원문 준비 중";
  const detail = document.createElement("p");
  const availableSec = speechAvailabilitySec(event);
  detail.textContent = textAvailable
    ? `${formatTime(event?.time_sec)}–${formatTime(event?.end_sec)} 발화` +
      (event?.available_sec === undefined
        ? ""
        : ` · ${formatTime(availableSec)}부터 텍스트 이용 가능`)
    : speaking
      ? `${formatTime(event?.time_sec)}부터 발화 중 · ` +
        `원문은 ${formatTime(availableSec)}부터 표시`
      : `${formatTime(event?.end_sec)} 발화 종료 · ` +
        `원문은 ${formatTime(availableSec)}부터 표시`;
  card.append(badge, title, detail);
  return card;
}

const OVERLAY_REGIONS = {
  phase: "#phase-event-overlay",
  speech: "#speech-event-overlay",
  request: "#request-event-overlay",
  transfer: "#transfer-event-overlay",
};

function clearVideoEventOverlay() {
  Object.values(OVERLAY_REGIONS).forEach((selector) => {
    $(selector)?.replaceChildren();
  });
  const announcer = $("#event-overlay-announcer");
  if (announcer) announcer.textContent = "";
  state.overlayFingerprint = "";
  state.overlayKeys.clear();
}

function renderVideoEventOverlay(activeIntervals = []) {
  if (
    !state.overlayEnabled ||
    visualUnavailability() ||
    !state.data
  ) {
    if (state.overlayFingerprint !== "") clearVideoEventOverlay();
    return;
  }
  const now = performance.now();
  for (const [key, value] of state.pointOverlayExpiry.entries()) {
    if (value.expiresAt <= now) state.pointOverlayExpiry.delete(key);
  }
  const points = [...state.pointOverlayExpiry.values()]
    .sort(
      (left, right) =>
        right.triggeredAt - left.triggeredAt ||
        right.eventFrame - left.eventFrame,
    )
    .slice(0, 1);
  const phase = currentPhaseContext();
  const speech = currentSpeechContext();
  const speechStage = speech ? speechOverlayStage(speech) : null;
  const cards = [
    ...(phase
      ? [
          {
            key: `phase:${candidateKey(phase)}`,
            region: "phase",
            candidate: phase,
            create: () => phaseOverlayCard(phase),
            announce:
              `현재 임시 수술 단계 ${fieldsForCandidate(phase).phase_id || "미상"}, ` +
              "애매 문맥이며 정답 집계 제외",
          },
        ]
      : []),
    ...(speech
      ? [
          {
            key: `speech:${speechEventKey(speech)}:${speechStage}`,
            region: "speech",
            candidate: speech,
            create: () => speechOverlayCard(speech, speechStage),
            announce:
              speechStage === "text_available"
                ? `음성 발화 문맥, ${speech?.text || "발화 원문 없음"}`
                : speechStage === "speaking"
                  ? "음성 발화가 시작되었습니다. 원문은 발화 종료 후 표시됩니다."
                  : "음성 발화가 종료되어 원문을 준비하고 있습니다.",
          },
        ]
      : []),
    ...activeIntervals.slice(0, 1).map((candidate) => ({
      key: `request:${candidateKey(candidate)}`,
      region: "request",
      candidate,
      interval: true,
      reason: "active",
      create: () =>
        interactionOverlayCard(candidate, {
          interval: true,
          reason: "active",
        }),
      announce: `${overlayBadge(candidate, {
        interval: true,
        reason: "active",
      })}, 암묵적 손 요청`,
    })),
    ...points.map((value) => ({
      key: `transfer:${candidateKey(value.candidate)}`,
      region: "transfer",
      candidate: value.candidate,
      interval: false,
      reason: value.reason,
      create: () =>
        interactionOverlayCard(value.candidate, {
          interval: false,
          reason: value.reason,
        }),
      announce: (() => {
        const fields = fieldsForOverlay(value.candidate);
        return (
          `${overlayBadge(value.candidate, {
            interval: false,
            reason: value.reason,
          })}, ${humanizeIdentifier(fields.tool)} 이동, ` +
          `${humanizeIdentifier(fields.from)}에서 ${humanizeIdentifier(
            fields.to,
          )}로`
        );
      })(),
    })),
  ];
  const cardKeys = cards.map(({ key }) => key);
  const newCards = cards.filter(
    (_card, index) => !state.overlayKeys.has(cardKeys[index]),
  );
  const fingerprint = cards
    .map(({ key, region, candidate, interval, reason }) => {
      const fields =
        region === "speech" ? {} : fieldsForOverlay(candidate);
      return [
        key,
        region,
        interval,
        reason,
        fields.start_source_frame_idx,
        fields.end_source_frame_idx,
        fields.source_frame_idx,
        fields.tool,
        fields.from,
        fields.to,
        fields.phase_id,
        candidate?.text,
        candidate?.available_sec,
      ].join(":");
    })
    .join("|");
  const overlayChanged = fingerprint !== state.overlayFingerprint;
  if (overlayChanged) {
    Object.entries(OVERLAY_REGIONS).forEach(([region, selector]) => {
      const entry = cards.find((card) => card.region === region);
      $(selector)?.replaceChildren(...(entry ? [entry.create()] : []));
    });
    state.overlayFingerprint = fingerprint;
  }
  if (newCards.length) {
    const announcement = newCards.map(({ announce }) => announce).join(". ");
    const announcer = $("#event-overlay-announcer");
    announcer.textContent = "";
    requestAnimationFrame(() => {
      announcer.textContent = announcement;
    });
  } else if (overlayChanged) {
    $("#event-overlay-announcer").textContent = "";
  }
  state.overlayKeys = new Set(cardKeys);
  clearTimeout(state.overlayTimer);
  if (state.pointOverlayExpiry.size) {
    const nextExpiry = Math.min(
      ...[...state.pointOverlayExpiry.values()].map((value) => value.expiresAt),
    );
    state.overlayTimer = setTimeout(
      () => renderVideoEventOverlay(activeIntervals),
      Math.max(16, nextExpiry - performance.now() + 20),
    );
  }
}

function updateVideoEventOverlay(
  previousFrame,
  currentFrame,
  { reason = "seek" } = {},
) {
  if (!state.data) return;
  if (reason !== "playback" && currentFrame !== previousFrame) {
    state.pointOverlayExpiry.clear();
  }
  if (!state.overlayEnabled || visualUnavailability()) {
    renderVideoEventOverlay([]);
    return;
  }
  const items = overlaySourceItems();
  const activeIntervals = items.filter((candidate) => {
    const fields = fieldsForOverlay(candidate);
    return (
      fields.event_type === "implicit_tool_request" &&
      currentFrame >= fields.start_source_frame_idx &&
      currentFrame <= fields.end_source_frame_idx
    );
  });
  const points = items.filter((candidate) => {
    const fields = fieldsForOverlay(candidate);
    if (fields.event_type !== "tool_transfer") return false;
    const frame = fields.source_frame_idx;
    if (reason === "playback") {
      return (
        currentFrame >= previousFrame &&
        currentFrame - previousFrame <= 60 &&
        frame > previousFrame &&
        frame <= currentFrame
      );
    }
    return frame === currentFrame;
  });
  const now = performance.now();
  points.forEach((candidate) => {
    state.pointOverlayExpiry.set(candidateKey(candidate), {
      candidate,
      reason,
      eventFrame: fieldsForOverlay(candidate).source_frame_idx,
      triggeredAt: now,
      expiresAt: now + 3600,
    });
  });
  renderVideoEventOverlay(activeIntervals);
}

function containRect(containerWidth, containerHeight, mediaWidth, mediaHeight) {
  if (
    containerWidth <= 0 ||
    containerHeight <= 0 ||
    mediaWidth <= 0 ||
    mediaHeight <= 0
  ) {
    return null;
  }
  const scale = Math.min(
    containerWidth / mediaWidth,
    containerHeight / mediaHeight,
  );
  const width = mediaWidth * scale;
  const height = mediaHeight * scale;
  return {
    x: (containerWidth - width) / 2,
    y: (containerHeight - height) / 2,
    width,
    height,
    scale,
  };
}

function recognitionContentRect(view, viewData, width, height) {
  const image = fallbackImage(view);
  if (!image.hidden) {
    if (
      image.dataset.sourceFrameIndex !== String(state.currentFrame) ||
      !image.complete ||
      image.naturalWidth <= 0
    ) {
      return null;
    }
    return containRect(
      width,
      height,
      viewData.source_width,
      viewData.source_height,
    );
  }

  const element = videos[view];
  if (element.hidden) return null;
  const proxy = viewData.continuous_proxy;
  const proxyWidth = element.videoWidth || proxy.width;
  const proxyHeight = element.videoHeight || proxy.height;
  const outer = containRect(width, height, proxyWidth, proxyHeight);
  if (!outer) return null;
  const [contentX, contentY, contentWidth, contentHeight] =
    proxy.content_rect;
  const proxyScaleX = proxyWidth / proxy.width;
  const proxyScaleY = proxyHeight / proxy.height;
  return {
    x: outer.x + contentX * proxyScaleX * outer.scale,
    y: outer.y + contentY * proxyScaleY * outer.scale,
    width: contentWidth * proxyScaleX * outer.scale,
    height: contentHeight * proxyScaleY * outer.scale,
  };
}

function recognitionColorToken(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
}

function clippedRecognitionLabel(context, label, maximumWidth) {
  if (context.measureText(label).width <= maximumWidth) return label;
  let clipped = label;
  while (
    clipped.length > 1 &&
    context.measureText(`${clipped}…`).width > maximumWidth
  ) {
    clipped = clipped.slice(0, -1);
  }
  return `${clipped}…`;
}

function drawRecognitionInstance(
  context,
  instance,
  viewData,
  content,
  colors,
) {
  const bbox = instance?.bbox_xyxy;
  if (!Array.isArray(bbox) || bbox.length !== 4) return;
  const coordinates = bbox.map(Number);
  if (!coordinates.every(Number.isFinite)) return;
  const [xMin, yMin, xMax, yMax] = coordinates;
  const left =
    content.x + (xMin / viewData.source_width) * content.width;
  const top =
    content.y + (yMin / viewData.source_height) * content.height;
  const right =
    content.x + (xMax / viewData.source_width) * content.width;
  const bottom =
    content.y + (yMax / viewData.source_height) * content.height;
  const boxWidth = Math.max(1, right - left);
  const boxHeight = Math.max(1, bottom - top);
  const handRequest = instance.class_name === "Hand_request";
  const color = handRequest ? colors.hand : colors.tool;

  context.save();
  context.setLineDash(handRequest ? [6, 4] : []);
  context.strokeStyle = colors.outline;
  context.lineWidth = 5;
  context.strokeRect(left, top, boxWidth, boxHeight);
  context.strokeStyle = color;
  context.lineWidth = 2.25;
  context.strokeRect(left, top, boxWidth, boxHeight);
  context.restore();

  const confidence = Number(instance.confidence);
  const confidenceText = Number.isFinite(confidence)
    ? ` ${Math.round(confidence * 100)}%`
    : "";
  const rawLabel = `${instance.class_name || "RF-DETR"}${confidenceText}`;
  const fontSize = content.width < 260 ? 10 : 11;
  context.font =
    `700 ${fontSize}px Inter, Pretendard, "Noto Sans KR", sans-serif`;
  context.textBaseline = "top";
  const paddingX = 5;
  const paddingY = 3;
  const maximumLabelWidth = Math.max(32, content.width - paddingX * 4);
  const label = clippedRecognitionLabel(
    context,
    rawLabel,
    maximumLabelWidth - paddingX * 2,
  );
  const labelWidth = Math.min(
    maximumLabelWidth,
    context.measureText(label).width + paddingX * 2,
  );
  const labelHeight = fontSize + paddingY * 2;
  const labelX = clamp(
    left,
    content.x,
    Math.max(content.x, content.x + content.width - labelWidth),
  );
  const preferredY = top - labelHeight - 2;
  const labelY =
    preferredY >= content.y
      ? preferredY
      : clamp(
          top + 2,
          content.y,
          Math.max(content.y, content.y + content.height - labelHeight),
        );
  context.fillStyle = color;
  context.globalAlpha = 0.94;
  context.fillRect(labelX, labelY, labelWidth, labelHeight);
  context.globalAlpha = 1;
  context.fillStyle = colors.text;
  context.fillText(label, labelX + paddingX, labelY + paddingY);
}

function drawRecognitionView(view, viewData, instances) {
  const canvas = recognitionCanvases[view];
  const bounds = videoTile(view).getBoundingClientRect();
  if (bounds.width <= 0 || bounds.height <= 0) {
    clearRecognitionCanvas(view);
    return;
  }
  const deviceScale = Math.max(1, window.devicePixelRatio || 1);
  const targetWidth = Math.round(bounds.width * deviceScale);
  const targetHeight = Math.round(bounds.height * deviceScale);
  if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
    canvas.width = targetWidth;
    canvas.height = targetHeight;
  }
  const context = canvas.getContext("2d");
  context.setTransform(deviceScale, 0, 0, deviceScale, 0, 0);
  context.clearRect(0, 0, bounds.width, bounds.height);
  const content = recognitionContentRect(
    view,
    viewData,
    bounds.width,
    bounds.height,
  );
  if (!content) {
    canvas.hidden = true;
    return;
  }
  canvas.hidden = false;
  const tile = videoTile(view);
  tile.dataset.recognitionActive = "true";
  tile.dataset.recognitionCount = String(instances.length);
  const colors = {
    tool: recognitionColorToken("--brand"),
    hand: recognitionColorToken("--warning"),
    outline: recognitionColorToken("--surface-inverse"),
    text: recognitionColorToken("--text-inverse"),
  };
  context.save();
  context.beginPath();
  context.rect(content.x, content.y, content.width, content.height);
  context.clip();
  instances.forEach((instance) =>
    drawRecognitionInstance(context, instance, viewData, content, colors),
  );
  context.restore();
}

function recognitionRenderFingerprint() {
  const surfaces = RECOGNITION_VIDEO_VIEWS.map((view) => {
    const image = fallbackImage(view);
    const bounds = videoTile(view).getBoundingClientRect();
    return [
      view,
      Math.round(bounds.width * 10),
      Math.round(bounds.height * 10),
      image.hidden ? "video" : `frame-${image.dataset.sourceFrameIndex || ""}`,
    ].join(":");
  }).join("|");
  return [
    state.recognition.data?.case_id || "",
    state.recognition.enabled,
    state.currentFrame,
    gapAt(state.currentTimeSec) ? "gap" : "visual",
    surfaces,
  ].join("|");
}

function renderRecognitionOverlays({ force = false } = {}) {
  const data = state.recognition.data;
  if (
    !data ||
    !state.recognition.enabled ||
    gapAt(state.currentTimeSec) ||
    state.currentTimeSec > visualEndTime() + 1e-7
  ) {
    clearRecognitionOverlays();
    return;
  }
  const fingerprint = recognitionRenderFingerprint();
  if (!force && state.recognition.renderFingerprint === fingerprint) return;
  state.recognition.renderFingerprint = fingerprint;
  RECOGNITION_VIDEO_VIEWS.forEach((view) => {
    const viewData = data.views[view];
    const instances = viewData.frames[state.currentFrame];
    drawRecognitionView(
      view,
      viewData,
      Array.isArray(instances) ? instances : [],
    );
  });
}

function installRecognitionResizeObserver() {
  if (typeof ResizeObserver !== "function") return;
  const observer = new ResizeObserver(() => {
    state.recognition.renderFingerprint = null;
    renderRecognitionOverlays({ force: true });
  });
  RECOGNITION_VIDEO_VIEWS.forEach((view) => observer.observe(videoTile(view)));
  state.recognition.resizeObserver = observer;
}

function updateReadout() {
  const exactTime = timeForFrame(state.currentFrame);
  const gap = gapAt(state.currentTimeSec);
  $("#frame-output").textContent =
    `frame ${state.currentFrame}${gap ? " · nearest" : ""}`;
  $("#time-output").textContent = gap
    ? `${formatTime(state.currentTimeSec)} · 영상 공백`
    : state.currentTimeSec > visualEndTime() + 1e-7
      ? `${formatTime(state.currentTimeSec)} · VIDEO OFF SCREEN`
    : `${formatTime(exactTime)} · ${exactTime.toFixed(9)} s`;
  updateVisualAvailability();
  renderRecognitionOverlays();
}

function updatePlayhead() {
  if (!state.data) return;
  const node = $("#playhead");
  const x = trackLabelWidth() + timeToPixel(state.currentTimeSec);
  node.style.left = `${x}px`;
  node.setAttribute(
    "aria-label",
    `현재 재생 위치 ${formatTime(state.currentTimeSec)}`,
  );
  node.setAttribute("aria-valuemin", "0");
  node.setAttribute(
    "aria-valuemax",
    String(Math.max(0, Number(state.data.frame_count || 1) - 1)),
  );
  node.setAttribute("aria-valuenow", String(state.currentFrame));
  node.setAttribute(
    "aria-valuetext",
    `frame ${state.currentFrame}, ${formatTime(state.currentTimeSec)}`,
  );
  updatePhaseCatalogCurrent();
  if (state.followPlayhead && state.playing) {
    const scroll = $("#timeline-scroll");
    const margin = 96;
    const viewportLeft = scroll.scrollLeft;
    const viewportRight = viewportLeft + scroll.clientWidth;
    if (x < viewportLeft + margin || x > viewportRight - margin) {
      scroll.scrollLeft = Math.max(0, x - scroll.clientWidth * 0.36);
    }
  }
}

function videoTile(view) {
  return $(`[data-camera-view="${view}"]`);
}

function fallbackImage(view) {
  return $(`#${view}-fallback`);
}

function setVideoViewStatus(view, label, streamState = "loading") {
  const status = $(`#${view}-stream-status`);
  const tile = videoTile(view);
  if (status) status.textContent = label;
  if (tile) tile.dataset.streamState = streamState;
}

function videoViewDescriptor(media, view) {
  const entry = media?.video_views?.[view] ?? media?.video_urls?.[view];
  if (typeof entry === "string" && entry) {
    return { video_url: entry, has_audio: view === "cam4" };
  }
  if (entry && typeof entry === "object" && entry.video_url) {
    return entry;
  }
  if (
    view === "cam4" &&
    media?.master_view === "cam4" &&
    media?.video_url
  ) {
    return { video_url: media.video_url, has_audio: true };
  }
  return null;
}

function sourceFrameDurationSec() {
  const fps = Number(state.data?.source_fps || state.data?.media?.source_fps);
  return Number.isFinite(fps) && fps > 0 ? 1 / fps : 1 / 15;
}

function videoViewReady(view) {
  const element = videos[view];
  return (
    Boolean(element) &&
    state.readyVideoViews.has(view) &&
    element.readyState >= HTMLMediaElement.HAVE_METADATA
  );
}

function boundedVideoTime(element, timeSec) {
  const target = Math.max(0, Number(timeSec) || 0);
  return Number.isFinite(element.duration)
    ? Math.min(target, Math.max(0, element.duration))
    : target;
}

function setVideoViewTime(view, timeSec, { force = false } = {}) {
  const element = videos[view];
  if (!element || !videoViewReady(view)) return false;
  const target = boundedVideoTime(element, timeSec);
  const drift = Math.abs(element.currentTime - target);
  if (!force && drift <= sourceFrameDurationSec() + 1e-7) return false;
  try {
    element.currentTime = target;
    return true;
  } catch {
    setVideoViewStatus(view, "동기화 재시도", "waiting");
    return false;
  }
}

function syncFollowerVideo(
  view,
  { force = false, play = state.playing && !video.paused } = {},
) {
  const follower = videos[view];
  if (!follower || !videoViewReady(view) || !videoViewReady("cam4")) return;
  follower.muted = true;
  follower.playbackRate = video.playbackRate;
  setVideoViewTime(view, video.currentTime, { force });
  if (!play) {
    if (!follower.paused) follower.pause();
    return;
  }
  if (!follower.paused || follower.dataset.playPending === "true") return;
  follower.dataset.playPending = "true";
  follower
    .play()
    .catch(() => {
      setVideoViewStatus(view, "재생 동기화 대기", "waiting");
    })
    .finally(() => {
      delete follower.dataset.playPending;
    });
}

function syncFollowerVideos(options = {}) {
  FOLLOWER_VIDEO_VIEWS.forEach((view) => syncFollowerVideo(view, options));
}

function seekContinuousVideos(timeSec) {
  setVideoViewTime("cam4", timeSec, { force: true });
  FOLLOWER_VIDEO_VIEWS.forEach((view) => {
    setVideoViewTime(view, timeSec, { force: true });
  });
}

function pauseFollowerVideos() {
  FOLLOWER_VIDEO_VIEWS.forEach((view) => {
    const follower = videos[view];
    if (follower && !follower.paused) follower.pause();
  });
}

function setCurrentTime(
  timeSec,
  {
    fromVideo = false,
    overlayReason = "seek",
    frameSelection = "nearest",
  } = {},
) {
  if (!state.data) return;
  const previousFrame = state.currentFrame;
  const target = clamp(Number(timeSec) || 0, timelineStart(), timelineEnd());
  state.currentTimeSec = target;
  state.currentFrame =
    frameSelection === "floor"
      ? frameIndexAtOrBefore(target)
      : nearestFrameIndex(target);
  updateReadout();
  updatePlayhead();
  updateVideoEventOverlay(previousFrame, state.currentFrame, {
    reason: overlayReason,
  });
  if (!fromVideo && state.videoReady && !state.fallbackMode) {
    state.pendingSeekFrameSelection = frameSelection;
    seekContinuousVideos(target);
  }
}

function seekToFrame(frameIndex) {
  if (!state.data) return;
  const previousFrame = state.currentFrame;
  const frame = clamp(
    Math.trunc(Number(frameIndex) || 0),
    0,
    state.data.frame_count - 1,
  );
  pausePlayback();
  state.currentFrame = frame;
  state.currentTimeSec = timeForFrame(frame);
  if (state.videoReady && !state.fallbackMode) {
    state.pendingSeekFrameSelection = "nearest";
    seekContinuousVideos(state.currentTimeSec);
  }
  updateReadout();
  updatePlayhead();
  updateVideoEventOverlay(previousFrame, state.currentFrame, {
    reason: "seek",
  });
  refreshFallbackFrame();
}

function frameSelectionForVisibleMedia() {
  return state.videoReady && !state.fallbackMode ? "floor" : "nearest";
}

function seekToTime(
  timeSec,
  { frameSelection = frameSelectionForVisibleMedia() } = {},
) {
  pausePlayback();
  setCurrentTime(timeSec, { frameSelection });
  refreshFallbackFrame();
}

function stepFrame(amount) {
  if (!state.data || !guardSaving()) return;
  seekToFrame(state.currentFrame + amount);
}

async function fetchExactFrame(view, frameIndex) {
  const response = await fetch(
    apiUrl("/api/frame", {
      view,
      source_frame_idx: frameIndex,
    }),
    { cache: "force-cache" },
  );
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `${view} 프레임을 읽지 못했습니다.`);
  }
  return response.blob();
}

function assignBlobImage(image, blob, sourceFrameIndex) {
  const objectUrl = URL.createObjectURL(blob);
  const previous = image.dataset.objectUrl;
  image.dataset.sourceFrameIndex = String(sourceFrameIndex);
  image.src = objectUrl;
  image.dataset.objectUrl = objectUrl;
  image.onload = () => {
    if (previous) URL.revokeObjectURL(previous);
    state.recognition.renderFingerprint = null;
    renderRecognitionOverlays({ force: true });
  };
}

async function refreshFallbackFrame() {
  if (!state.data || state.playing) return;
  const views = state.fallbackMode
    ? REVIEW_VIDEO_VIEWS
    : REVIEW_VIDEO_VIEWS.filter((view) =>
        state.fallbackVideoViews.has(view),
      );
  if (!views.length) return;
  const sequence = ++state.frameSequence;
  const requestedFrame = state.currentFrame;
  const requestedGeneration = state.mediaGeneration;
  const requestedCaseId = activeCaseId();
  const requestIsCurrent = () =>
    sequence === state.frameSequence &&
    requestedGeneration === state.mediaGeneration &&
    requestedCaseId === activeCaseId() &&
    requestedFrame === state.currentFrame &&
    !state.playing;
  await Promise.all(
    views.map(async (view) => {
      fallbackImage(view).dataset.sourceFrameIndex = "";
      clearRecognitionCanvas(view);
      try {
        const blob = await fetchExactFrame(view, requestedFrame);
        if (
          !requestIsCurrent() ||
          (!state.fallbackMode && !state.fallbackVideoViews.has(view))
        ) {
          return;
        }
        assignBlobImage(fallbackImage(view), blob, requestedFrame);
        fallbackImage(view).hidden = false;
        setVideoViewStatus(view, "정확 프레임", "fallback");
      } catch (failure) {
        if (!requestIsCurrent()) return;
        setVideoViewStatus(view, "프레임 불러오기 실패", "error");
        if (view === "cam4") {
          $("#video-error").hidden = false;
          $("#video-error p").textContent = failure.message;
        }
      }
    }),
  );
}

function enterFallbackMode(reason = "") {
  state.mediaGeneration += 1;
  state.mediaRecoveryGeneration = null;
  state.videoReady = false;
  state.fallbackMode = true;
  state.playing = false;
  state.readyVideoViews.clear();
  state.fallbackVideoViews = new Set(REVIEW_VIDEO_VIEWS);
  REVIEW_VIDEO_VIEWS.forEach((view) => {
    const element = videos[view];
    if (!element.paused) element.pause();
    element.removeAttribute("src");
    element.load();
    element.hidden = true;
    fallbackImage(view).hidden = false;
    setVideoViewStatus(view, "정확 프레임", "fallback");
  });
  $("#video-loading").hidden = true;
  $("#video-error").hidden = false;
  $("#playback-mode-badge").textContent = "정확 프레임 모드";
  $("#play-toggle").disabled = true;
  $("#play-toggle").textContent = "▶";
  if (reason) $("#video-error p").textContent = reason;
  refreshFallbackFrame();
}

function enterViewFallback(view, reason = "정확 프레임") {
  if (view === "cam4") {
    enterFallbackMode(reason);
    return;
  }
  const element = videos[view];
  state.readyVideoViews.delete(view);
  state.fallbackVideoViews.add(view);
  if (!element.paused) element.pause();
  element.hidden = true;
  fallbackImage(view).hidden = false;
  setVideoViewStatus(view, reason, "fallback");
  refreshFallbackFrame();
  finishVideoRecoveryIfReady();
}

function setVideoLoadingMessage(message = DEFAULT_VIDEO_LOADING_MESSAGE) {
  const node = $("#video-loading-message");
  if (node) node.textContent = message;
}

function finishVideoRecoveryIfReady() {
  if (
    state.mediaRecoveryGeneration !== state.mediaGeneration ||
    !state.videoReady
  ) {
    return;
  }
  const allViewsSettled = REVIEW_VIDEO_VIEWS.every(
    (view) =>
      state.readyVideoViews.has(view) ||
      state.fallbackVideoViews.has(view),
  );
  if (!allViewsSettled) return;
  state.mediaRecoveryGeneration = null;
  setVideoLoadingMessage();
  toast("중단된 영상 화면을 자동으로 복구했습니다. 같은 위치에서 계속 검토할 수 있습니다.");
}

function configureVideo({ recovery = false } = {}) {
  const media = state.data?.media;
  clearRecognitionOverlays();
  const descriptors = Object.fromEntries(
    REVIEW_VIDEO_VIEWS.map((view) => [
      view,
      videoViewDescriptor(media, view),
    ]),
  );
  if (!descriptors.cam4) {
    enterFallbackMode(
      "독립 CAM4 영상 URL이 없어 네 카메라의 정확 프레임 모드로 전환했습니다.",
    );
    return;
  }
  pausePlayback();
  const generation = ++state.mediaGeneration;
  state.mediaRecoveryGeneration = recovery ? generation : null;
  state.fallbackMode = false;
  state.videoReady = false;
  state.readyVideoViews.clear();
  state.fallbackVideoViews.clear();
  setVideoLoadingMessage(
    recovery
      ? "백그라운드에서 중단된 4-view 영상을 복구하고 있습니다"
      : DEFAULT_VIDEO_LOADING_MESSAGE,
  );
  $("#video-loading").hidden = false;
  $("#video-error").hidden = true;
  $("#play-toggle").disabled = true;
  $("#playback-mode-badge").textContent = recovery
    ? "4-view 복구 중"
    : "4-view 준비 중";
  REVIEW_VIDEO_VIEWS.forEach((view) => {
    const element = videos[view];
    const descriptor = descriptors[view];
    element.pause();
    element.removeAttribute("src");
    // Force Chromium to release a decoder surface discarded while the page
    // was backgrounded before attaching the same stream again.
    element.load();
    element.dataset.mediaGeneration = String(generation);
    element.dataset.playPending = "";
    element.muted = view !== "cam4";
    fallbackImage(view).hidden = true;
    element.hidden = false;
    setVideoViewStatus(
      view,
      view === "cam4" ? "기준 영상 준비 중" : "동기화 준비 중",
      "loading",
    );
    if (!descriptor?.video_url) {
      state.fallbackVideoViews.add(view);
      element.hidden = true;
      fallbackImage(view).hidden = false;
      setVideoViewStatus(view, "정확 프레임", "fallback");
      return;
    }
    element.src = apiUrl(descriptor.video_url);
    element.load();
  });
  refreshFallbackFrame();
  finishVideoRecoveryIfReady();
}

function markVideoPageAway() {
  if (state.mediaAwaySince === null) {
    state.mediaAwaySince = Date.now();
  }
  state.mediaWasPlayingBeforeAway =
    state.mediaWasPlayingBeforeAway ||
    state.playing ||
    (state.videoReady && !video.paused);
  if (state.mediaRecoveryTimer !== null) {
    clearTimeout(state.mediaRecoveryTimer);
    state.mediaRecoveryTimer = null;
  }
  if (state.data && state.videoReady && !state.fallbackMode) {
    pausePlayback();
  }
}

function resumeVideoAfterBriefAway() {
  const shouldResume = state.mediaWasPlayingBeforeAway;
  state.mediaWasPlayingBeforeAway = false;
  if (
    !shouldResume ||
    !state.videoReady ||
    state.fallbackMode ||
    document.hidden
  ) {
    return;
  }
  video.play().catch(() => {
    toast("영상은 같은 위치에 일시정지했습니다. 재생 버튼을 눌러 이어서 확인하세요.");
  });
}

function scheduleVideoRecovery(reason, { force = false } = {}) {
  if (document.hidden) return;
  const awaySince = state.mediaAwaySince;
  const awayDuration =
    awaySince === null ? 0 : Math.max(0, Date.now() - awaySince);
  if (awaySince === null && !force) return;
  state.mediaAwaySince = null;

  if (!force && awayDuration < MEDIA_RECOVERY_MIN_AWAY_MS) {
    resumeVideoAfterBriefAway();
    return;
  }
  state.mediaWasPlayingBeforeAway = false;
  if (!state.data || state.fallbackMode) return;
  if (state.mediaRecoveryTimer !== null) {
    clearTimeout(state.mediaRecoveryTimer);
  }
  state.mediaRecoveryTimer = window.setTimeout(() => {
    state.mediaRecoveryTimer = null;
    if (document.hidden || !state.data || state.fallbackMode) return;
    configureVideo({ recovery: true });
    $("#video-shell").dataset.recoveryReason = reason;
  }, MEDIA_RECOVERY_DEBOUNCE_MS);
}

function pausePlayback() {
  if (!video.paused) video.pause();
  pauseFollowerVideos();
  state.playing = false;
  $("#play-toggle").textContent = "▶";
  $("#play-toggle").setAttribute("aria-label", "재생");
  if (state.playbackFrame !== null) {
    cancelAnimationFrame(state.playbackFrame);
    state.playbackFrame = null;
  }
}

function updateAudioControls() {
  const muted = video.muted || video.volume === 0;
  $("#mute-toggle").setAttribute("aria-pressed", String(muted));
  $("#mute-toggle").setAttribute(
    "aria-label",
    muted ? "음소거 해제" : "음소거",
  );
  $("#mute-toggle").textContent = muted ? "음소거" : "소리";
  $("#volume-range").value = String(video.volume);
}

async function togglePlayback() {
  if (!guardSaving()) return;
  if (state.fallbackMode || !state.videoReady) {
    toast("연속 영상이 없어 재생할 수 없습니다. 프레임 버튼으로 검토하세요.");
    return;
  }
  if (!video.paused) {
    pausePlayback();
    refreshFallbackFrame();
    return;
  }
  $("#candidate-alert").hidden = true;
  try {
    syncFollowerVideos({ force: true, play: false });
    await video.play();
  } catch (error) {
    enterFallbackMode(error.message || "브라우저가 영상을 재생하지 못했습니다.");
  }
}

function unresolvedCandidatesCrossed(previousTime, currentTime) {
  if (currentTime < previousTime || currentTime - previousTime > 3) return [];
  return visibleSourceItems()
    .filter((candidate) => reviewStatus(candidate) === "unreviewed")
    .filter((candidate) => {
      const frame = fieldsForCandidate(candidate).source_frame_idx;
      const time = timeForFrame(frame);
      return time > previousTime + 1e-7 && time <= currentTime + 0.035;
    })
    .sort(
      (left, right) =>
        fieldsForCandidate(left).source_frame_idx -
        fieldsForCandidate(right).source_frame_idx,
    );
}

function showCandidateAlert(candidates) {
  if (!candidates.length) return;
  const first = candidates[0];
  const fields = fieldsForCandidate(first);
  const countText = candidates.length > 1 ? ` 외 ${candidates.length - 1}건` : "";
  $("#candidate-alert-title").textContent =
    `${eventTitle(first, fields)}${countText} · ` +
    (fields.event_type === "implicit_tool_request"
      ? `${formatTime(timeForFrame(fields.start_source_frame_idx))}–` +
        `${formatTime(timeForFrame(fields.end_source_frame_idx))}`
      : formatTime(timeForFrame(fields.source_frame_idx)));
  $("#candidate-alert-detail").textContent =
    fields.event_type === "implicit_tool_request"
      ? "AI point 원안은 시작=종료입니다. 영상을 확인해 시작과 종료를 각각 지정하세요."
      : "AI 시각이 이르면 영상을 더 본 뒤 ‘현재 프레임으로 이동’하세요.";
  $("#candidate-alert").hidden = false;
}

function playbackTick() {
  if (!state.playing || video.paused) {
    state.playbackFrame = null;
    return;
  }
  const now = clamp(video.currentTime, timelineStart(), timelineEnd());
  syncFollowerVideos({ force: false, play: true });
  setCurrentTime(now, {
    fromVideo: true,
    overlayReason: "playback",
    frameSelection: "floor",
  });
  if (
    state.activeInspector === "interaction" &&
    !isFinalMode() &&
    $("#auto-pause").checked
  ) {
    const crossed = unresolvedCandidatesCrossed(state.lastPlaybackTime, now);
    if (crossed.length) {
      const target = crossed[0];
      const targetFrame = fieldsForCandidate(target).source_frame_idx;
      const preserveDraft = hasDirtyDraft();
      pausePlayback();
      seekContinuousVideos(timeForFrame(targetFrame));
      state.currentTimeSec = timeForFrame(targetFrame);
      state.currentFrame = targetFrame;
      if (!preserveDraft) {
        selectCandidate(target, {
          seek: false,
          bypassDirty: true,
          bypassSaving: true,
        });
      }
      updateReadout();
      updatePlayhead();
      updateVideoEventOverlay(targetFrame, targetFrame, { reason: "seek" });
      refreshFallbackFrame();
      showCandidateAlert(
        crossed.filter(
          (candidate) =>
            fieldsForCandidate(candidate).source_frame_idx === targetFrame,
        ),
      );
      if (preserveDraft) {
        toast(
          "후보에서 일시정지했습니다. 현재 미저장 이벤트 수정은 그대로 유지했습니다.",
          5000,
        );
      }
      return;
    }
  }
  state.lastPlaybackTime = now;
  state.playbackFrame = requestAnimationFrame(playbackTick);
}

function moveDraftToPlayhead() {
  if (isFinalMode()) {
    toast("최종 검수 모드는 읽기 전용입니다.");
    return;
  }
  if (!guardSaving()) return;
  pausePlayback();
  $("#candidate-alert").hidden = true;
  if (!state.draft) {
    createNewAnnotation();
    return;
  }
  if (state.draft.event_type === "implicit_tool_request") {
    toast("요청 구간은 ‘현재 프레임을 시작으로/종료로’ 버튼을 사용해 주세요.");
    return;
  }
  const unavailable = visualUnavailability();
  if (unavailable) {
    toast(unavailable.detail);
    return;
  }
  state.draft.source_frame_idx = state.currentFrame;
  state.draft.start_source_frame_idx = state.currentFrame;
  state.draft.end_source_frame_idx = state.currentFrame;
  state.draft.dirty = true;
  renderInspectorTiming();
  renderTimeline();
  updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
    reason: "seek",
  });
  toast(`이벤트 시각을 frame ${state.currentFrame}로 이동했습니다.`);
}

function setRequestBoundary(boundary) {
  if (isFinalMode()) return;
  if (
    !state.draft ||
    state.draft.event_type !== "implicit_tool_request" ||
    !guardSaving()
  ) {
    return;
  }
  pausePlayback();
  $("#candidate-alert").hidden = true;
  const unavailable = visualUnavailability();
  if (unavailable) {
    toast(unavailable.detail);
    return;
  }
  let frame = state.currentFrame;
  const peerFrame =
    boundary === "start"
      ? state.draft.end_source_frame_idx
      : state.draft.start_source_frame_idx;
  const segmentFrame = clampRequestFrameToPeer(frame, peerFrame);
  const crossedGap = segmentFrame !== frame;
  frame = segmentFrame;
  if (boundary === "start") {
    state.draft.start_source_frame_idx = frame;
    state.draft.source_frame_idx = frame;
    if (frame > state.draft.end_source_frame_idx) {
      state.draft.end_source_frame_idx = frame;
      toast("시작이 기존 종료보다 늦어 종료도 같은 프레임으로 맞췄습니다.");
    } else {
      toast(
        crossedGap
          ? `카메라 공백을 넘지 않도록 요청 시작을 frame ${frame}에 맞췄습니다.`
          : `요청 시작을 frame ${frame}로 지정했습니다.`,
      );
    }
  } else {
    state.draft.end_source_frame_idx = frame;
    if (frame < state.draft.start_source_frame_idx) {
      state.draft.start_source_frame_idx = frame;
      state.draft.source_frame_idx = frame;
      toast("종료가 기존 시작보다 빨라 시작도 같은 프레임으로 맞췄습니다.");
    } else {
      toast(
        crossedGap
          ? `카메라 공백을 넘지 않도록 요청 종료를 frame ${frame}에 맞췄습니다.`
          : `요청 종료를 frame ${frame}로 지정했습니다.`,
      );
    }
  }
  state.draft.dirty = true;
  renderInspectorTiming();
  renderTimeline();
  updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
    reason: "seek",
  });
}

function startMarkerDrag(event, candidate, boundary = "point") {
  event.preventDefault();
  event.stopPropagation();
  if (isFinalMode()) return;
  if (!guardSaving()) return;
  pausePlayback();
  $("#candidate-alert").hidden = true;
  if (candidate) {
    const key = candidateKey(candidate);
    if (
      !["candidate", "human"].includes(state.selected?.kind) ||
      state.selected.id !== key
    ) {
      if (!selectCandidate(candidate, { seek: false })) return;
    }
  } else if (state.selected?.kind !== "new") {
    return;
  }
  state.markerDrag = {
    pointerId: event.pointerId,
    candidate,
    boundary,
    snappedGap: false,
    moved: false,
    draftBaseline: structuredClone(state.draft),
    frameBaseline: state.currentFrame,
    timeBaseline: state.currentTimeSec,
  };
}

function updateMarkerDrag(event) {
  if (!state.markerDrag || !state.draft) return;
  let time = clientXToTimelineTime(event.clientX);
  if (time > visualEndTime() + 1e-7) {
    state.markerDrag.blockedRegion = "VIDEO OFF SCREEN";
    return;
  }
  state.markerDrag.blockedRegion = null;
  if (gapAt(time)) {
    time = snapOutOfGap(time);
    state.markerDrag.snappedGap = true;
  }
  const frame = nearestFrameIndex(time);
  const boundary = state.markerDrag.boundary;
  let nextFrame = frame;
  if (boundary === "start") {
    nextFrame = Math.min(frame, state.draft.end_source_frame_idx);
    const clampedFrame = clampRequestFrameToPeer(
      nextFrame,
      state.draft.end_source_frame_idx,
    );
    state.markerDrag.snappedGap ||= clampedFrame !== nextFrame;
    nextFrame = clampedFrame;
  } else if (boundary === "end") {
    nextFrame = Math.max(frame, state.draft.start_source_frame_idx);
    const clampedFrame = clampRequestFrameToPeer(
      nextFrame,
      state.draft.start_source_frame_idx,
    );
    state.markerDrag.snappedGap ||= clampedFrame !== nextFrame;
    nextFrame = clampedFrame;
  }
  const currentBoundaryFrame =
    boundary === "start"
      ? state.draft.start_source_frame_idx
      : boundary === "end"
        ? state.draft.end_source_frame_idx
        : state.draft.source_frame_idx;
  if (nextFrame === currentBoundaryFrame) return;
  state.markerDrag.moved = true;
  if (boundary === "start") {
    state.draft.start_source_frame_idx = nextFrame;
    state.draft.source_frame_idx = nextFrame;
  } else if (boundary === "end") {
    state.draft.end_source_frame_idx = nextFrame;
  } else {
    state.draft.source_frame_idx = nextFrame;
    state.draft.start_source_frame_idx = nextFrame;
    state.draft.end_source_frame_idx = nextFrame;
  }
  state.draft.dirty = true;
  state.currentFrame = nextFrame;
  state.currentTimeSec = timeForFrame(nextFrame);
  if (state.videoReady && !state.fallbackMode) {
    seekContinuousVideos(state.currentTimeSec);
  }
  updateReadout();
  renderInspectorTiming();
  renderTimeline();
  updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
    reason: "seek",
  });
}

function finishMarkerDrag(event) {
  if (!state.markerDrag) return;
  const drag = state.markerDrag;
  const snappedGap = drag.snappedGap;
  const blockedRegion = drag.blockedRegion;
  const moved = drag.moved;
  state.markerDrag = null;
  if (event.type === "pointercancel") {
    state.draft = structuredClone(drag.draftBaseline);
    state.currentFrame = drag.frameBaseline;
    state.currentTimeSec = drag.timeBaseline;
    if (state.videoReady && !state.fallbackMode) {
      seekContinuousVideos(state.currentTimeSec);
    }
    updateReadout();
    renderAll();
    refreshFallbackFrame();
    toast("타임라인 드래그를 취소하고 원래 위치로 복원했습니다.");
    return;
  }
  if (!moved && drag.candidate) {
    // pointerdown may have replaced the marker while selecting it, so its
    // native click never fires. Explicitly seek on pointerup.
    selectCandidate(drag.candidate, { navigatorPlacement: "top" });
  }
  if (moved) {
    state.suppressMarkerClick = true;
    setTimeout(() => {
      state.suppressMarkerClick = false;
    }, 0);
  }
  refreshFallbackFrame();
  if (snappedGap) {
    toast("영상 공백을 건너 가장 가까운 유효 프레임에 맞췄습니다.");
  } else if (blockedRegion) {
    toast("VIDEO OFF SCREEN 구간에는 시각 이벤트를 옮길 수 없습니다.");
  }
}

function startPlayheadDrag(event) {
  event.preventDefault();
  if (!guardSaving()) return;
  pausePlayback();
  state.playheadDrag = {
    frameBaseline: state.currentFrame,
    timeBaseline: state.currentTimeSec,
  };
  try {
    event.currentTarget.setPointerCapture(event.pointerId);
  } catch {
    // Synthetic QA events and a pointer lost during dispatch may not be capturable.
  }
  seekToTime(clientXToTimelineTime(event.clientX));
}

function updatePlayheadDrag(event) {
  if (!state.playheadDrag) return;
  const target = clientXToTimelineTime(event.clientX);
  setCurrentTime(target, {
    frameSelection: frameSelectionForVisibleMedia(),
  });
}

function finishPlayheadDrag(event) {
  if (!state.playheadDrag) return;
  const drag = state.playheadDrag;
  state.playheadDrag = false;
  try {
    event.currentTarget.releasePointerCapture(event.pointerId);
  } catch {
    // The pointer may already have been released by the browser.
  }
  if (event.type === "pointercancel") {
    state.currentFrame = drag.frameBaseline;
    state.currentTimeSec = drag.timeBaseline;
    if (state.videoReady && !state.fallbackMode) {
      seekContinuousVideos(state.currentTimeSec);
    }
    updateReadout();
    updatePlayhead();
    toast("재생 위치 드래그를 취소하고 원래 위치로 복원했습니다.");
  }
  refreshFallbackFrame();
}

function adjudicatedFields() {
  syncDraftFromForm();
  if (!state.draft) throw new Error("검토할 이벤트가 선택되지 않았습니다.");
  const eventType = state.draft.event_type;
  if (eventType === "implicit_tool_request") {
    const startFrame = state.draft.start_source_frame_idx;
    const endFrame = state.draft.end_source_frame_idx;
    return {
      event_type: eventType,
      source_frame_idx: startFrame,
      time_sec: timeForFrame(startFrame),
      start_source_frame_idx: startFrame,
      end_source_frame_idx: endFrame,
      start_sec: timeForFrame(startFrame),
      end_sec: timeForFrame(endFrame),
      tool: null,
      from: null,
      to: null,
      phase_id: null,
    };
  }
  return {
    event_type: eventType,
    source_frame_idx: state.draft.source_frame_idx,
    time_sec: timeForFrame(state.draft.source_frame_idx),
    tool: eventType === "tool_transfer" ? state.draft.tool : null,
    from: eventType === "tool_transfer" ? state.draft.from : null,
    to: eventType === "tool_transfer" ? state.draft.to : null,
    phase_id: eventType === "phase_start" ? state.draft.phase_id : null,
  };
}

function validateDraft(fields) {
  const unavailable = visualUnavailability(
    timeForFrame(fields.source_frame_idx),
  );
  if (unavailable) {
    throw validationError(unavailable.detail);
  }
  if (gapAt(timeForFrame(fields.source_frame_idx))) {
    throw validationError("영상 공백 안에는 이벤트를 저장할 수 없습니다.");
  }
  if (fields.event_type === "implicit_tool_request") {
    if (
      !Number.isInteger(fields.start_source_frame_idx) ||
      !Number.isInteger(fields.end_source_frame_idx)
    ) {
      throw validationError(
        "요청 시작과 종료 frame을 모두 지정해 주세요.",
        "#set-request-start",
      );
    }
    if (fields.end_source_frame_idx < fields.start_source_frame_idx) {
      throw validationError(
        "요청 종료는 시작보다 빠를 수 없습니다.",
        "#set-request-end",
      );
    }
    const startTime = timeForFrame(fields.start_source_frame_idx);
    const endTime = timeForFrame(fields.end_source_frame_idx);
    if (endTime > visualEndTime() + 1e-7) {
      throw validationError(
        "VIDEO OFF SCREEN 구간에는 요청 종료를 둘 수 없습니다.",
        "#set-request-end",
      );
    }
    const crossesGap = (state.data?.gaps || []).some(
      (gap) =>
        startTime < Number(gap.after_time_sec) &&
        endTime > Number(gap.before_time_sec),
    );
    if (crossesGap) {
      throw validationError(
        "암묵적 요청 구간은 카메라 공백을 가로지를 수 없습니다.",
        "#set-request-end",
      );
    }
  }
  if (fields.event_type === "tool_transfer") {
    if (!fields.tool || !/^[a-z][a-z0-9_]*$/.test(fields.tool)) {
      throw validationError(
        "도구 이동에는 canonical tool ID가 필요합니다.",
        "#tool-id",
      );
    }
    if (!fields.from || !fields.to || fields.from === fields.to) {
      throw validationError(
        "서로 다른 From과 To 위치를 선택해 주세요.",
        "#to-location",
      );
    }
  }
  if (
    fields.event_type === "phase_start" &&
    (!fields.phase_id || !/^P[0-9]{2,}$/.test(fields.phase_id))
  ) {
    throw validationError(
      "수술 단계 ID는 P13과 같은 형식이어야 합니다.",
      "#phase-id",
    );
  }
}

function setSaving(saving) {
  state.saving = saving;
  $("#case-selector").disabled =
    saving ||
    !state.data?.case_selector_enabled ||
    $("#case-selector").options.length < 2;
  $("#review-form").setAttribute("aria-busy", String(saving));
  $("#editor").classList.toggle("saving", saving);
  [
    "#confirm",
    "#ambiguous",
    "#reject",
    "#withdraw",
    "#move-to-playhead",
    "#set-request-start",
    "#set-request-end",
    "#discard-draft",
    "#new-event",
    "#new-event-empty",
    "#previous-candidate",
    "#next-candidate",
    "#zoom-out",
    "#zoom-fit",
    "#zoom-in",
  ].forEach(
    (selector) => {
      $(selector).disabled = saving;
    },
  );
  $$(
    "[data-frame-step], [data-track-filter], .type-option, #play-toggle, #playback-rate",
  ).forEach((control) => {
    control.disabled = saving;
  });
  if (saving) {
    setSelectionStatus(null, "저장 중…");
  }
  if (!saving) {
    updateVisualAvailability();
    if (state.selected && state.draft) {
      renderInspectorFields();
      const candidate = selectedCandidate();
      setSelectionStatus(
        candidate ? reviewStatus(candidate) : null,
        state.selected.kind === "new" ? "새 이벤트" : "미검토",
      );
    }
    $("#play-toggle").disabled = state.fallbackMode || !state.videoReady;
  }
}

async function saveAction(reviewStatus, { withdrawal = false } = {}) {
  if (isFinalMode()) {
    toast("최종 검수 모드에서는 판정을 수정할 수 없습니다.");
    return;
  }
  if (!state.selected || !state.draft || !guardSaving()) return;
  if (withdrawal && !guardDirtyDraft()) return;
  pausePlayback();
  $("#candidate-alert").hidden = true;
  const reviewerId = $("#reviewer-id").value.trim();
  if (!reviewerId) {
    $("#reviewer-id").setAttribute("aria-invalid", "true");
    $("#reviewer-id").focus();
    toast("검토자 ID를 먼저 입력해 주세요.");
    return;
  }
  $("#reviewer-id").removeAttribute("aria-invalid");
  showFormError("");
  resetDangerArms();
  try {
    const selectionSnapshot = structuredClone(state.selected);
    const candidate = selectedCandidate();
    const currentAction = candidate ? effectiveAction(candidate) : null;
    if (
      withdrawal &&
      (selectionSnapshot.kind !== "human" ||
        !currentAction?.adjudicated_fields)
    ) {
      throw new Error("사람이 직접 만든 활성 이벤트만 철회할 수 있습니다.");
    }
    const fields = withdrawal
      ? structuredClone(currentAction.adjudicated_fields)
      : adjudicatedFields();
    validateDraft(fields);
    const meta = candidateMeta(candidate);
    const payload = {
      case_id: state.data.case_id,
      revision: state.data.revision,
      operation:
        selectionSnapshot.kind === "new"
          ? "create_annotation"
          : selectionSnapshot.kind === "human"
            ? "revise_annotation"
            : "review_candidate",
      candidate_id:
        selectionSnapshot.kind === "candidate"
          ? meta.candidate_id || candidateKey(candidate)
          : null,
      candidate_sha256:
        selectionSnapshot.kind === "candidate"
          ? meta.candidate_sha256 || null
          : null,
      annotation_id:
        selectionSnapshot.kind === "human"
          ? meta.annotation_id || candidate.event_id
          : null,
      reviewer_id: reviewerId,
      review_status: reviewStatus,
      notes: withdrawal
        ? "사용자가 직접 만든 이벤트를 활성 타임라인에서 철회함."
        : $("#review-notes").value,
      adjudicated_fields: fields,
      supersedes_action_id: currentAction ? actionId(currentAction) : null,
      playhead_time_sec:
        withdrawal || visualUnavailability() ? undefined : state.currentTimeSec,
      client_request_id:
        selectionSnapshot.kind === "new"
          ? state.draft.client_request_id
          : undefined,
    };

    setSaving(true);
    const response = await fetch(apiUrl("/api/annotation-action"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Review-Mode": state.viewMode,
      },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || "판정을 저장하지 못했습니다.");
    }

    const previousSelection = selectionSnapshot;
    state.data = result.state;
    const returnedActionId = actionId(result.action);
    createTypeOptions();
    fillToolSuggestions();
    const refreshedItems = sourceItems();
    let target = null;
    if (["candidate", "human"].includes(previousSelection.kind)) {
      target = refreshedItems.find(
        (item, index) =>
          candidateKey(item, index) === previousSelection.id,
      );
    } else if (returnedActionId) {
      target = refreshedItems.find((item) =>
        actionHistory(item).some(
          (action) => actionId(action) === returnedActionId,
        ),
      );
    }

    state.draft = null;
    state.selected = null;
    state.selectionBaseline = null;
    if (target) {
      selectCandidate(target, {
        seek: false,
        bypassDirty: true,
        bypassSaving: true,
      });
    } else {
      renderAll();
    }
    if (withdrawal) {
      toast(
        `${returnedActionId || "새 action"} · 이벤트를 철회했습니다. 감사 이력은 유지됩니다.`,
        5000,
      );
      $("#selection-status").focus({ preventScroll: true });
    } else {
      toast(
        `${returnedActionId || "새 action"} · ${statusLabel(reviewStatus)} 저장됨`,
      );
      selectSameFrameUnreviewed();
    }
  } catch (error) {
    showFormError(
      error.message || "판정을 저장하지 못했습니다.",
      error.targetSelector || null,
    );
  } finally {
    setSaving(false);
  }
}

function selectSameFrameUnreviewed() {
  const sameFrame = visibleSourceItems().filter(
    (candidate) =>
      reviewStatus(candidate) === "unreviewed" &&
      fieldsForCandidate(candidate).source_frame_idx === state.currentFrame,
  );
  if (!sameFrame.length || hasDirtyDraft()) return;
  selectCandidate(sameFrame[0], {
    seek: false,
    bypassDirty: true,
    bypassSaving: true,
  });
  showCandidateAlert(sameFrame);
}

function resetRejectArm() {
  state.rejectArmedFor = null;
  clearTimeout(state.rejectTimer);
  if ($("#reject")) {
    $("#reject").textContent = "이 후보만 기각 · Shift+R";
  }
}

function resetWithdrawArm() {
  state.withdrawArmedFor = null;
  clearTimeout(state.withdrawTimer);
  if ($("#withdraw")) {
    $("#withdraw").textContent = "이 이벤트 철회";
  }
}

function resetDangerArms() {
  resetRejectArm();
  resetWithdrawArm();
}

function requestReject() {
  if (state.selected?.kind !== "candidate" || !guardSaving()) return;
  const id = state.selected.id;
  if (state.rejectArmedFor === id) {
    saveAction("rejected");
    return;
  }
  state.rejectArmedFor = id;
  $("#reject").textContent = "한 번 더 눌러 이 후보만 기각";
  toast(
    "주변 구간이 아니라 선택한 AI 후보 하나만 기각합니다. 확실하면 한 번 더 누르세요.",
    5000,
  );
  clearTimeout(state.rejectTimer);
  state.rejectTimer = setTimeout(resetRejectArm, 5000);
}

function requestWithdraw() {
  if (
    state.selected?.kind !== "human" ||
    !guardSaving() ||
    !guardDirtyDraft()
  ) {
    return;
  }
  if (!$("#reviewer-id").value.trim()) {
    $("#reviewer-id").setAttribute("aria-invalid", "true");
    $("#reviewer-id").focus();
    toast("검토자 ID를 먼저 입력해 주세요.");
    return;
  }
  const id = state.selected.id;
  if (state.withdrawArmedFor === id) {
    saveAction("rejected", { withdrawal: true });
    return;
  }
  resetRejectArm();
  state.withdrawArmedFor = id;
  $("#withdraw").textContent = "한 번 더 눌러 이벤트 철회";
  toast(
    "선택한 사람 생성 이벤트가 활성 목록과 타임라인에서 사라집니다. 감사 이력은 남습니다. 확실하면 한 번 더 누르세요.",
    5000,
  );
  clearTimeout(state.withdrawTimer);
  state.withdrawTimer = setTimeout(resetWithdrawArm, 5000);
}

function navigateCombinedItem(direction) {
  if (!guardAnyNavigation()) return;
  const entries = combinedNavigatorEntries({ actionableOnly: true });
  if (!entries.length) {
    toast(
      isFinalMode()
        ? "표시 중인 수술 이벤트나 미검토 임상 어노테이션이 없습니다."
        : "표시 중인 미검토 이벤트나 임상 어노테이션이 없습니다.",
    );
    return;
  }
  const currentIndex = entries.findIndex((entry) =>
    entry.scope === "clinical"
      ? state.activeInspector === "clinical" &&
        state.clinical.selectedId === entry.key
      : state.activeInspector === "interaction" &&
        state.selected?.id === entry.key,
  );
  const nextIndex =
    currentIndex < 0
      ? direction > 0
        ? 0
        : entries.length - 1
      : (currentIndex + direction + entries.length) % entries.length;
  const entry = entries[nextIndex];
  if (entry.scope === "clinical") {
    selectClinicalItem(entry.item.id, {
      bypassDirty: true,
      focus: true,
    });
    return;
  }
  selectCandidate(entry.candidate, {
    bypassDirty: true,
    navigatorPlacement: "top",
  });
  $(
    `[data-list-candidate-id="${CSS.escape(entry.key)}"]`,
  )?.focus({ preventScroll: true });
}

function navigateCandidate(direction) {
  if (!guardSaving() || !guardDirtyDraft()) return;
  const candidates = visibleSourceItems()
    .filter(
      (candidate) =>
        isFinalMode() || reviewStatus(candidate) === "unreviewed",
    )
    .sort(
      (left, right) =>
        fieldsForCandidate(left).source_frame_idx -
        fieldsForCandidate(right).source_frame_idx,
    );
  if (!candidates.length) {
    toast(
      isFinalMode()
        ? "현재 표시 중인 Track에는 최종 이벤트가 없습니다."
        : "현재 표시 중인 Track에는 미검토 후보가 없습니다.",
    );
    return;
  }
  const currentIndex = candidates.findIndex(
    (candidate, index) =>
      (isFinalMode()
        ? state.selected?.kind === "final"
        : state.selected?.kind === "candidate") &&
      candidateKey(candidate, index) === state.selected.id,
  );
  const nextIndex =
    currentIndex < 0
      ? direction > 0
        ? 0
        : candidates.length - 1
      : (currentIndex + direction + candidates.length) % candidates.length;
  selectCandidate(candidates[nextIndex]);
}

function installTimelinePointerHandlers() {
  [
    "#speech-track",
    "#request-track",
    "#transfer-track",
    "#phase-track",
    "#gap-track",
    "#clinical-track",
    "#time-ruler",
  ].forEach(
    (selector) => {
      $(selector).addEventListener("click", (event) => {
        if (event.target.closest(".event-marker")) return;
        if (!guardSaving()) return;
        seekToTime(clientXToTimelineTime(event.clientX));
      });
    },
  );
  $("#playhead").addEventListener("pointerdown", startPlayheadDrag);
  $("#playhead").addEventListener("pointermove", updatePlayheadDrag);
  $("#playhead").addEventListener("pointerup", finishPlayheadDrag);
  $("#playhead").addEventListener("pointercancel", finishPlayheadDrag);
  document.addEventListener("pointermove", updateMarkerDrag);
  document.addEventListener("pointerup", finishMarkerDrag);
  document.addEventListener("pointercancel", finishMarkerDrag);
  $("#timeline-scroll").addEventListener(
    "wheel",
    (event) => {
      const scroll = $("#timeline-scroll");
      if (event.shiftKey) {
        event.preventDefault();
        const delta = Math.abs(event.deltaY) >= Math.abs(event.deltaX)
          ? event.deltaY
          : event.deltaX;
        scroll.scrollLeft += delta;
        return;
      }
      if (
        !state.data ||
        state.saving ||
        state.markerDrag ||
        state.playheadDrag ||
        event.ctrlKey ||
        event.metaKey ||
        Math.abs(event.deltaY) < 0.01
      ) {
        return;
      }
      const normalizedDelta =
        event.deltaMode === WheelEvent.DOM_DELTA_LINE
          ? event.deltaY * 16
          : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
            ? event.deltaY * scroll.clientHeight
            : event.deltaY;
      const factor = Math.exp(-normalizedDelta * 0.002);
      const nextPixelsPerSecond = clamp(
        state.pixelsPerSecond * factor,
        4,
        160,
      );
      if (Math.abs(nextPixelsPerSecond - state.pixelsPerSecond) < 1e-7) {
        return;
      }
      event.preventDefault();
      zoomTimeline(factor, event.clientX);
    },
    { passive: false },
  );
}

function installEventHandlers() {
  document.addEventListener(
    "pointerdown",
    () => {
      state.focusModality = "pointer";
    },
    true,
  );
  $("#play-toggle").addEventListener("click", togglePlayback);
  $("#case-selector").addEventListener("change", () => {
    switchCase($("#case-selector").value);
  });
  $$("[data-review-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      setReviewMode(button.dataset.reviewMode);
    });
  });
  $("#event-overlay-enabled").addEventListener("change", () => {
    state.overlayEnabled = $("#event-overlay-enabled").checked;
    localStorage.setItem(
      "surgery-review-event-overlay",
      String(state.overlayEnabled),
    );
    state.overlayFingerprint = null;
    if (!state.overlayEnabled) {
      state.pointOverlayExpiry.clear();
      clearVideoEventOverlay();
      renderClinicalOverlay();
      toast("영상 오버레이를 껐습니다.");
    } else {
      updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
        reason: "seek",
      });
      renderClinicalOverlay();
      toast("영상 오버레이를 켰습니다.");
    }
  });
  $("#recognition-overlay-enabled").addEventListener("change", () => {
    if (!state.recognition.data) return;
    state.recognition.enabled = $("#recognition-overlay-enabled").checked;
    localStorage.setItem(
      recognitionStorageKey(state.recognition.data.case_id),
      String(state.recognition.enabled),
    );
    state.recognition.renderFingerprint = null;
    if (state.recognition.enabled) {
      renderRecognitionOverlays({ force: true });
      toast("CAM4·FLIR AI 인식 오버레이를 켰습니다.");
    } else {
      clearRecognitionOverlays();
      toast("AI 인식 오버레이를 껐습니다.");
    }
  });
  $("#playback-rate").addEventListener("change", () => {
    const rate = Number($("#playback-rate").value);
    REVIEW_VIDEO_VIEWS.forEach((view) => {
      videos[view].playbackRate = rate;
    });
    syncFollowerVideos({ force: true });
  });
  $("#mute-toggle").addEventListener("click", () => {
    if (video.muted || video.volume === 0) {
      if (video.volume === 0) video.volume = 0.5;
      video.muted = false;
    } else {
      video.muted = true;
    }
    updateAudioControls();
  });
  $("#volume-range").addEventListener("input", () => {
    video.volume = Number($("#volume-range").value);
    if (video.volume > 0) video.muted = false;
    localStorage.setItem("surgery-review-volume", String(video.volume));
    updateAudioControls();
  });
  $$("[data-frame-step]").forEach((button) => {
    button.addEventListener("click", (event) => {
      stepFrame(Number(button.dataset.frameStep));
      if (event.detail > 0) button.blur();
    });
  });
  $$("[data-track-filter]").forEach((input) => {
    input.addEventListener("change", () => {
      const track = input.dataset.trackFilter;
      if (isAnySaving()) {
        toast("판정을 저장하고 있습니다. 저장이 끝난 뒤 다시 시도해 주세요.");
        input.checked = state.filters[track];
        return;
      }
      if (
        track === "clinical" &&
        !input.checked &&
        state.activeInspector === "clinical"
      ) {
        if (!guardAnyNavigation()) {
          input.checked = true;
          return;
        }
        state.filters.clinical = false;
        setClinicalSelectionState(null);
        state.activeInspector = "interaction";
        renderAll();
        toast("임상 Track을 숨기고 선택을 해제했습니다.");
        return;
      }
      if (
        track === "speech" &&
        !input.checked &&
        state.selected?.kind === "speech"
      ) {
        state.filters.speech = false;
        clearSelection({ force: true });
        toast("음성 문맥 Track을 숨기고 선택을 해제했습니다.");
        return;
      }
      const selectedTrack =
        state.activeInspector === "interaction" && state.draft
        ? TRACK_FOR_EVENT[state.draft.event_type]
        : null;
      if (!input.checked && selectedTrack === track) {
        if (hasDirtyDraft()) {
          input.checked = true;
          toast("수정 중인 이벤트의 Track은 숨길 수 없습니다. 먼저 확정하거나 취소하세요.");
          return;
        }
        state.filters[track] = false;
        clearSelection({ force: true });
        state.overlayFingerprint = null;
        updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
          reason: "seek",
        });
        toast("숨긴 Track의 선택을 해제했습니다.");
        return;
      }
      state.filters[track] = input.checked;
      renderAll();
      state.overlayFingerprint = null;
      updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
        reason: "seek",
      });
    });
  });
  $("#reset-filters").addEventListener("click", () => {
    if (isAnySaving()) {
      toast("판정을 저장하고 있습니다. 저장이 끝난 뒤 다시 시도해 주세요.");
      return;
    }
    Object.keys(state.filters).forEach((track) => {
      state.filters[track] = true;
      const input = $(`[data-track-filter="${track}"]`);
      if (input) input.checked = true;
    });
    renderAll();
    state.overlayFingerprint = null;
    updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
      reason: "seek",
    });
    toast("모든 Track을 다시 표시했습니다.");
  });
  $("#move-to-playhead").addEventListener("click", moveDraftToPlayhead);
  $("#set-request-start").addEventListener("click", () => {
    setRequestBoundary("start");
  });
  $("#set-request-end").addEventListener("click", () => {
    setRequestBoundary("end");
  });
  $("#discard-draft").addEventListener("click", discardDraft);
  $("#confirm").addEventListener("click", () => saveAction("confirmed"));
  $("#ambiguous").addEventListener("click", () => saveAction("ambiguous"));
  $("#reject").addEventListener("click", requestReject);
  $("#withdraw").addEventListener("click", requestWithdraw);
  $("#new-event").addEventListener("click", createNewAnnotation);
  $("#new-event-empty").addEventListener("click", createNewAnnotation);
  $("#previous-candidate").addEventListener("click", () => {
    navigateCombinedItem(-1);
  });
  $("#next-candidate").addEventListener("click", () => {
    navigateCombinedItem(1);
  });
  $("#zoom-out").addEventListener("click", () => zoomTimeline(0.75));
  $("#zoom-in").addEventListener("click", () => zoomTimeline(1.35));
  $("#zoom-fit").addEventListener("click", fitTimeline);
  $("#dismiss-alert").addEventListener("click", () => {
    $("#candidate-alert").hidden = true;
  });
  $("#reload-state").addEventListener("click", () => {
    if (!guardAnyNavigation()) return;
    loadState({ preserveSelection: false }).catch((error) => toast(error.message));
  });
  $("#retry-video").addEventListener("click", () => configureVideo());
  $("#reviewer-id").addEventListener("change", () => {
    localStorage.setItem("surgery-reviewer-id", $("#reviewer-id").value.trim());
  });
  $("#reviewer-id").addEventListener("input", () => {
    if ($("#reviewer-id").value.trim()) {
      $("#reviewer-id").removeAttribute("aria-invalid");
    }
  });
  $("#clinical-reviewer-role").addEventListener("change", () => {
    localStorage.setItem(
      "surgery-clinical-reviewer-role",
      $("#clinical-reviewer-role").value,
    );
  });
  $("#clinical-discard-draft").addEventListener(
    "click",
    discardClinicalDraft,
  );
  $("#clinical-confirm").addEventListener("click", () => {
    saveClinicalReview("confirmed");
  });
  $("#clinical-ambiguous").addEventListener("click", () => {
    saveClinicalReview("ambiguous");
  });
  $("#clinical-reject").addEventListener("click", () => {
    saveClinicalReview("rejected");
  });
  $("#clinical-form")
    .querySelectorAll("input, select, textarea")
    .forEach((control) => {
      const handleClinicalDraftInput = () => {
        control.removeAttribute("aria-invalid");
        $("#clinical-form-error").hidden = true;
        syncClinicalDraftFromForm();
      };
      control.addEventListener("input", handleClinicalDraftInput);
      control.addEventListener("change", handleClinicalDraftInput);
    });

  ["#tool-id", "#from-location", "#to-location", "#phase-id", "#review-notes"].forEach(
    (selector) => {
      $(selector).addEventListener("input", () => {
        if (!state.draft) return;
        syncDraftFromForm();
        state.draft.dirty = true;
        renderInspectorTiming();
        renderTimeline();
        updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
          reason: "seek",
        });
      });
    },
  );

  REVIEW_VIDEO_VIEWS.forEach((view) => {
    const element = videos[view];
    const currentGeneration = () =>
      Number(element.dataset.mediaGeneration) === state.mediaGeneration;

    element.addEventListener("loadedmetadata", () => {
      if (!currentGeneration()) return;
      state.readyVideoViews.add(view);
      state.fallbackVideoViews.delete(view);
      fallbackImage(view).hidden = true;
      element.hidden = false;
      element.muted = view !== "cam4" ? true : element.muted;
      element.playbackRate = Number($("#playback-rate").value);
      setVideoViewStatus(
        view,
        view === "cam4" ? "기준 · 음성" : "동기화 준비",
        "ready",
      );
      setVideoViewTime(view, state.currentTimeSec, { force: true });
      state.recognition.renderFingerprint = null;
      renderRecognitionOverlays({ force: true });

      if (view === "cam4") {
        state.videoReady = true;
        state.fallbackMode = false;
        $("#video-loading").hidden = true;
        $("#video-error").hidden = true;
        $("#play-toggle").disabled = state.saving;
        $("#playback-mode-badge").textContent = "독립 4-view";
        syncFollowerVideos({ force: true, play: false });
      } else {
        syncFollowerVideo(view, {
          force: true,
          play: state.playing && !video.paused,
        });
      }
      finishVideoRecoveryIfReady();
    });

    element.addEventListener("error", () => {
      if (!currentGeneration() || !element.getAttribute("src")) return;
      if (view === "cam4") {
        enterFallbackMode("CAM4 기준 영상을 불러오지 못했습니다.");
      } else {
        enterViewFallback(view, "영상 오류 · 정확 프레임");
      }
    });

    element.addEventListener("waiting", () => {
      if (!currentGeneration()) return;
      setVideoViewStatus(
        view,
        view === "cam4" ? "버퍼링" : "동기화 중",
        "waiting",
      );
      if (view === "cam4" && state.videoReady) {
        $("#playback-mode-badge").textContent = "CAM4 버퍼링";
      }
    });

    element.addEventListener("playing", () => {
      if (!currentGeneration()) return;
      setVideoViewStatus(
        view,
        view === "cam4" ? "기준 · 음성" : "동기 재생",
        "ready",
      );
      if (view !== "cam4") return;
      state.playing = true;
      $("#play-toggle").textContent = "Ⅱ";
      $("#play-toggle").setAttribute("aria-label", "일시정지");
      $("#playback-mode-badge").textContent = "4-view 재생 중";
      // Auto-pause is edge-triggered: resuming on a candidate must not
      // immediately stop on that same candidate again.
      state.lastPlaybackTime = video.currentTime;
      syncFollowerVideos({ force: true, play: true });
      if (state.playbackFrame === null) {
        state.playbackFrame = requestAnimationFrame(playbackTick);
      }
    });

    element.addEventListener("pause", () => {
      if (!currentGeneration()) return;
      if (view !== "cam4") {
        if (video.paused && videoViewReady(view)) {
          setVideoViewStatus(view, "동기 정지", "ready");
        }
        return;
      }
      state.playing = false;
      pauseFollowerVideos();
      $("#play-toggle").textContent = "▶";
      $("#play-toggle").setAttribute("aria-label", "재생");
      if (state.videoReady) $("#playback-mode-badge").textContent = "일시정지";
    });

    element.addEventListener("ended", () => {
      if (!currentGeneration() || view !== "cam4") return;
      pausePlayback();
      $("#playback-mode-badge").textContent = "영상 끝";
      refreshFallbackFrame();
    });

    element.addEventListener("seeked", () => {
      if (!currentGeneration()) return;
      if (view === "cam4") {
        syncFollowerVideos({ force: true });
        if (!state.playing) {
          const frameSelection =
            state.pendingSeekFrameSelection ||
            frameSelectionForVisibleMedia();
          state.pendingSeekFrameSelection = null;
          setCurrentTime(video.currentTime, {
            fromVideo: true,
            frameSelection,
          });
          refreshFallbackFrame();
        }
      } else if (videoViewReady(view)) {
        setVideoViewStatus(
          view,
          state.playing ? "동기 재생" : "동기 정지",
          "ready",
        );
      }
      state.recognition.renderFingerprint = null;
      renderRecognitionOverlays({ force: true });
    });
  });

  window.addEventListener("resize", () => {
    if (state.fitTimeline) fitTimeline();
    state.recognition.renderFingerprint = null;
    renderRecognitionOverlays({ force: true });
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      markVideoPageAway();
    } else {
      scheduleVideoRecovery("visibility");
    }
  });
  window.addEventListener("blur", markVideoPageAway);
  window.addEventListener("focus", () => {
    scheduleVideoRecovery("focus");
  });
  window.addEventListener("pagehide", markVideoPageAway);
  window.addEventListener("pageshow", (event) => {
    scheduleVideoRecovery("pageshow", { force: event.persisted });
  });
  document.addEventListener("freeze", markVideoPageAway);
  document.addEventListener("resume", () => {
    scheduleVideoRecovery("resume", { force: true });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Tab") state.focusModality = "keyboard";
    const active = document.activeElement;
    const tag = active?.tagName || "";
    const inputType = tag === "INPUT" ? active.type : "";
    const isTextEditing =
      active &&
      (tag === "SELECT" ||
        tag === "TEXTAREA" ||
        (tag === "INPUT" && !["checkbox", "radio", "button"].includes(inputType)) ||
        active.isContentEditable);
    if (isTextEditing) {
      return;
    }
    const isToggleControl =
      active &&
      (tag === "SUMMARY" ||
        (tag === "INPUT" && ["checkbox", "radio"].includes(inputType)));
    if (event.code === "Space" && isToggleControl) {
      return;
    }
    if (
      event.code === "Space" &&
      tag === "BUTTON" &&
      state.focusModality === "keyboard"
    ) {
      return;
    }
    if (
      event.key === "Enter" &&
      active &&
      (tag === "BUTTON" || isToggleControl)
    ) {
      return;
    }
    if (
      (event.key === "Home" || event.key === "End") &&
      (active === $("#timeline-scroll") || active === $("#playhead"))
    ) {
      if (!state.data) return;
      event.preventDefault();
      const atEnd = event.key === "End";
      seekToFrame(atEnd ? state.data.frame_count - 1 : 0);
      $("#timeline-scroll").scrollLeft = atEnd
        ? $("#timeline-scroll").scrollWidth
        : 0;
      return;
    }
    if (state.activeInspector === "clinical") {
      if (event.code === "Space") {
        event.preventDefault();
        togglePlayback();
      } else if (
        event.key === "ArrowLeft" ||
        event.key === "ArrowRight"
      ) {
        event.preventDefault();
        const direction = event.key === "ArrowLeft" ? -1 : 1;
        stepFrame(direction * (event.shiftKey ? 5 : 1));
      } else if (event.key.toLowerCase() === "n") {
        event.preventDefault();
        navigateCombinedItem(1);
      } else if (event.key.toLowerCase() === "p") {
        event.preventDefault();
        navigateCombinedItem(-1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        saveClinicalReview("confirmed");
      } else if (event.key.toLowerCase() === "a") {
        event.preventDefault();
        saveClinicalReview("ambiguous");
      } else if (
        event.key.toLowerCase() === "r" &&
        event.shiftKey
      ) {
        event.preventDefault();
        saveClinicalReview("rejected");
      } else if (
        event.key === "Escape" &&
        hasDirtyClinicalDraft()
      ) {
        event.preventDefault();
        discardClinicalDraft();
      } else if (event.key === "Escape") {
        event.preventDefault();
        setClinicalSelectionState(null);
        state.activeInspector = "interaction";
        renderAll();
      }
      return;
    }
    if (event.code === "Space") {
      event.preventDefault();
      togglePlayback();
    } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const direction = event.key === "ArrowLeft" ? -1 : 1;
      stepFrame(direction * (event.shiftKey ? 5 : 1));
    } else if (event.key.toLowerCase() === "n") {
      event.preventDefault();
      navigateCombinedItem(1);
    } else if (event.key.toLowerCase() === "p") {
      event.preventDefault();
      navigateCombinedItem(-1);
    } else if (event.key.toLowerCase() === "m") {
      event.preventDefault();
      if (!isFinalMode()) moveDraftToPlayhead();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (!isFinalMode()) saveAction("confirmed");
    } else if (event.key.toLowerCase() === "a") {
      event.preventDefault();
      if (!isFinalMode()) saveAction("ambiguous");
    } else if (event.key.toLowerCase() === "r" && event.shiftKey) {
      event.preventDefault();
      if (!isFinalMode()) requestReject();
    } else if (event.key === "Escape") {
      event.preventDefault();
      if (isFinalMode()) {
        clearSelection({ force: true });
        return;
      }
      if (state.rejectArmedFor || state.withdrawArmedFor) {
        resetDangerArms();
        toast("기각 또는 철회 확인을 취소했습니다.");
        return;
      }
      discardDraft();
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (!hasUnsavedWork() && !isAnySaving()) return;
    event.preventDefault();
    event.returnValue = "";
  });

  installRecognitionResizeObserver();
  installTimelinePointerHandlers();
}

async function loadState({ preserveSelection = true } = {}) {
  $("#editor").setAttribute("aria-busy", "true");
  try {
    const previous = preserveSelection ? state.selected : null;
    const response = await fetch(apiUrl("/api/state"), { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "검토 상태를 불러오지 못했습니다.");
    }
    state.data = payload;
    const finalReviewUrl =
      payload.final_review_url ||
      (payload.final_review_available ? "/api/final-review" : null);
    if (finalReviewUrl) {
      const finalResponse = await fetch(apiUrl(finalReviewUrl), {
        cache: "no-store",
      });
      const finalPayload = await finalResponse.json().catch(() => ({}));
      if (!finalResponse.ok) {
        throw new Error(
          finalPayload.error || "최종 참조를 불러오지 못했습니다.",
        );
      }
      state.finalReview = finalPayload;
    } else {
      state.finalReview = null;
    }
    await loadPhaseCatalog(payload.active_case_id || payload.case_id);
    if (!preserveSelection) {
      const urlMode = reviewModeFromUrl();
      const requestedMode = urlMode || payload.default_review_mode || "edit";
      state.viewMode =
        requestedMode === "edit" || state.finalReview?.available
          ? requestedMode
          : "edit";
      if (!urlMode) updateReviewModeUrl(state.viewMode);
    } else if (isFinalMode() && !state.finalReview?.available) {
      state.viewMode = "edit";
    }
    renderCaseSelector(payload);
    createTypeOptions();
    fillSelect(
      $("#from-location"),
      payload.vocabulary?.transfer_endpoints || [],
    );
    fillSelect($("#to-location"), payload.vocabulary?.transfer_endpoints || []);
    fillToolSuggestions();
    state.currentFrame = clamp(
      state.currentFrame,
      0,
      Math.max(0, payload.frame_count - 1),
    );
    state.currentTimeSec = timeForFrame(state.currentFrame);
    updateReadout();
    updatePlayhead();

    if (["candidate", "human", "final"].includes(previous?.kind)) {
      const candidate = sourceItems().find(
        (item, index) => candidateKey(item, index) === previous.id,
      );
      if (candidate) {
        state.selected = null;
        state.draft = null;
        selectCandidate(candidate, { seek: false, bypassDirty: true });
      }
    } else if (previous?.kind === "speech") {
      const event = speechEvents().find(
        (item, index) => speechEventKey(item, index) === previous.id,
      );
      state.selected = event
        ? { kind: "speech", id: previous.id }
        : null;
      state.draft = null;
      state.selectionBaseline = null;
    }

    renderAll();
    configureVideo();
    updateVideoEventOverlay(state.currentFrame, state.currentFrame, {
      reason: "seek",
    });
    const recognitionLoad = loadRecognitionOverlay(
      payload.active_case_id || payload.case_id,
    );
    await loadClinicalState({ preserveSelection });
    await recognitionLoad;
    requestAnimationFrame(fitTimeline);
  } finally {
    $("#editor").setAttribute("aria-busy", "false");
  }
}

const savedReviewer = localStorage.getItem("surgery-reviewer-id");
const savedClinicalReviewer = localStorage.getItem(
  "surgery-clinical-reviewer-id",
);
if (savedReviewer || savedClinicalReviewer) {
  $("#reviewer-id").value = savedReviewer || savedClinicalReviewer;
}
const savedClinicalReviewerRole = localStorage.getItem(
  "surgery-clinical-reviewer-role",
);
if (
  Object.hasOwn(
    CLINICAL_REVIEWER_ROLE_LABELS,
    savedClinicalReviewerRole,
  )
) {
  $("#clinical-reviewer-role").value = savedClinicalReviewerRole;
}
removeLegacyWorkspaceParameters();
state.clinical.viewMode = "draft";
const savedOverlay = localStorage.getItem("surgery-review-event-overlay");
if (savedOverlay !== null) {
  state.overlayEnabled = savedOverlay !== "false";
  $("#event-overlay-enabled").checked = state.overlayEnabled;
}
const savedVolumeValue = localStorage.getItem("surgery-review-volume");
const savedVolume = Number(savedVolumeValue);
if (
  savedVolumeValue !== null &&
  Number.isFinite(savedVolume) &&
  savedVolume >= 0 &&
  savedVolume <= 1
) {
  video.volume = savedVolume;
}
updateAudioControls();
installEventHandlers();
loadState({ preserveSelection: false }).catch((error) => {
  $("#event-list-loading").hidden = true;
  $("#event-list-empty").hidden = false;
  $("#event-list-empty-title").textContent = "검토 상태를 불러오지 못했습니다";
  $("#event-list-empty-detail").textContent = error.message;
  $("#reset-filters").hidden = true;
  $("#reload-state").hidden = false;
  $("#video-loading").hidden = true;
  $("#video-error").hidden = false;
  $("#video-error p").textContent = error.message;
});
