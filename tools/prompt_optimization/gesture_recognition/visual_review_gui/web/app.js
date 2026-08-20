"use strict";

const state = {
  session: null,
  currentIndex: 0,
  saving: false,
  showCompletedSamples: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const humanLabel = (openHand) => (openHand ? "열린 손 · true" : "열린 손 아님 · false");
const humanDecision = (decision) => {
  if (decision === "open_hand") return "열린 손";
  if (decision === "not_open_hand") return "열린 손 아님";
  return "판정 애매함";
};

function currentSample() {
  return state.session?.samples?.[state.currentIndex] ?? null;
}

function decisions() {
  return state.session?.decisions ?? {};
}

function announce(message) {
  $("#status-message").textContent = message;
}

function setError(message) {
  $("#error-message").textContent = message;
  $("#error-state").hidden = false;
}

function clearError() {
  $("#error-state").hidden = true;
  $("#error-message").textContent = "";
}

function reviewedCount() {
  return Object.keys(decisions()).length;
}

function renderProgress() {
  const total = state.session.sample_count;
  const reviewed = reviewedCount();
  const progress = total ? Math.round((reviewed / total) * 100) : 0;
  $("#progress-text").textContent = `${reviewed} / ${total}`;
  $("#progress-track").setAttribute("aria-valuemax", String(total));
  $("#progress-track").setAttribute("aria-valuenow", String(reviewed));
  $("#progress-fill").style.width = `${progress}%`;
}

function renderMetadata() {
  const metadata = state.session?.metadata ?? {};
  const title = metadata.title || "오른쪽 위 집도의 손 검토";
  const subtitle = metadata.subtitle || (
    "이미지에서 열린 손을 내밀고 있는지만 판정합니다. 기존 이벤트 라벨은 이 화면에서 수정되지 않습니다."
  );
  const completionTitle = metadata.completion_title || "현재 검토 샘플을 모두 확인했습니다.";
  $("#page-title").textContent = title;
  $(".subtitle").textContent = subtitle;
  $("#completion-title").textContent = completionTitle;
  document.title = title;
}

function updateHistory(sample) {
  const url = new URL(window.location.href);
  url.searchParams.set("sample", sample.sample_id);
  window.history.replaceState({}, "", url);
}

function sourceTime(timeSec) {
  const minutes = Math.floor(timeSec / 60);
  const seconds = timeSec - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(2).padStart(5, "0")}`;
}

function setControlBusy(isBusy) {
  state.saving = isBusy;
  $$(".decision-button, .primary-button, .secondary-button").forEach((button) => {
    button.disabled = isBusy;
  });
}

function render() {
  if (!state.session) return;
  renderProgress();
  const sample = currentSample();
  const allReviewed = reviewedCount() === state.session.sample_count;
  const showCompletion = allReviewed && !state.showCompletedSamples;
  $("#loading-state").hidden = true;
  $("#review-content").hidden = !sample || showCompletion;
  $("#completion-state").hidden = !showCompletion;
  if (!sample || showCompletion) {
    return;
  }

  const existing = decisions()[sample.sample_id];
  $("#sample-position").textContent = `SAMPLE ${sample.index} / ${state.session.sample_count}`;
  $("#sample-title").textContent = `${sample.case_id} · frame ${sample.frame_idx} · ${sourceTime(sample.time_sec)}`;
  $("#comparison-state").textContent = sample.comparison_group === "agreement"
    ? "VLM · 기존 이벤트 참조 일치"
    : "VLM · 기존 이벤트 참조 불일치";
  $("#original-image").src = sample.original_url;
  $("#original-image").alt = `${sample.case_id} frame ${sample.frame_idx} 원본 CAM4 프레임`;
  $("#original-link").href = sample.original_url;
  $("#vlm-input-image").src = sample.vlm_input_url;
  $("#vlm-input-image").alt = `${sample.case_id} frame ${sample.frame_idx} VLM 입력 crop`;
  $("#vlm-input-link").href = sample.vlm_input_url;
  $("#event-proxy-value").textContent = humanLabel(sample.existing_event_proxy_open_hand);
  $("#vlm-answer-value").textContent = humanLabel(sample.vlm_predicted_open_hand);
  $("#raw-model-text").textContent = sample.raw_model_text;
  $("#decision-state").textContent = existing
    ? `${existing.origin === "seed" ? "기존 시각 판정" : "저장됨"} · ${humanDecision(existing.decision)}`
    : "미검토";
  $("#review-note").value = existing?.note ?? "";
  $("#previous-button").disabled = state.saving || state.currentIndex === 0;
  $("#next-button").disabled = state.saving || state.currentIndex >= state.session.samples.length - 1;
  $("#next-unreviewed-button").disabled = state.saving || state.session.unreviewed_count === 0;
  updateHistory(sample);
}

function moveTo(index) {
  const total = state.session?.samples?.length ?? 0;
  if (!total) return;
  state.currentIndex = Math.max(0, Math.min(total - 1, index));
  render();
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ? "auto"
    : "smooth";
  $("#review-panel").scrollIntoView({ behavior, block: "start" });
}

function firstUnreviewed(startIndex = -1) {
  const samples = state.session.samples;
  for (let offset = 1; offset <= samples.length; offset += 1) {
    const candidateIndex = (startIndex + offset + samples.length) % samples.length;
    if (!decisions()[samples[candidateIndex].sample_id]) {
      return candidateIndex;
    }
  }
  return -1;
}

function findInitialIndex() {
  const requested = new URLSearchParams(window.location.search).get("sample");
  const directIndex = state.session.samples.findIndex((sample) => sample.sample_id === requested);
  if (directIndex >= 0) return directIndex;
  const first = firstUnreviewed(-1);
  return first >= 0 ? first : 0;
}

async function saveDecision(decision) {
  const sample = currentSample();
  if (!sample || state.saving) return;
  const note = $("#review-note").value.trim();
  setControlBusy(true);
  clearError();
  try {
    const response = await fetch("/api/decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sample_id: sample.sample_id, decision, note }),
    });
    const body = await response.json();
    if (!response.ok || !body.ok) {
      throw new Error(body.error || "판정을 저장하지 못했습니다.");
    }
    state.session.decisions[sample.sample_id] = body.decision;
    state.session.reviewed_count = reviewedCount();
    state.session.unreviewed_count = state.session.sample_count - state.session.reviewed_count;
    announce(`${sample.index}번 샘플을 ${humanDecision(decision)}으로 저장했습니다.`);
    const next = firstUnreviewed(state.currentIndex);
    if (next >= 0) {
      state.currentIndex = next;
    }
  } catch (error) {
    setError(error instanceof Error ? error.message : "판정을 저장하지 못했습니다.");
  } finally {
    setControlBusy(false);
    render();
  }
}

async function loadSession() {
  clearError();
  $("#loading-state").hidden = false;
  $("#review-content").hidden = true;
  $("#completion-state").hidden = true;
  try {
    const response = await fetch("/api/session", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok || body.schema !== "taskplanner.gesture_visual_review_session.v1") {
      throw new Error(body.error || "검토 session을 읽지 못했습니다.");
    }
    state.session = body;
    state.showCompletedSamples = false;
    state.currentIndex = findInitialIndex();
    renderMetadata();
    announce(`${body.sample_count}개 시각 검토 샘플을 불러왔습니다.`);
    render();
  } catch (error) {
    $("#loading-state").hidden = true;
    setError(error instanceof Error ? error.message : "검토 session을 읽지 못했습니다.");
  }
}

function onKeyboard(event) {
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
  const target = event.target;
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement) return;
  if (state.saving) return;
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    moveTo(state.currentIndex - 1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    moveTo(state.currentIndex + 1);
  } else if (event.key.toLowerCase() === "o") {
    event.preventDefault();
    saveDecision("open_hand");
  } else if (event.key.toLowerCase() === "x") {
    event.preventDefault();
    saveDecision("not_open_hand");
  } else if (event.key.toLowerCase() === "a") {
    event.preventDefault();
    saveDecision("ambiguous");
  }
}

function wireEvents() {
  $$(".decision-button").forEach((button) => {
    button.addEventListener("click", () => saveDecision(button.dataset.decision));
  });
  $("#previous-button").addEventListener("click", () => moveTo(state.currentIndex - 1));
  $("#next-button").addEventListener("click", () => moveTo(state.currentIndex + 1));
  $("#next-unreviewed-button").addEventListener("click", () => {
    const next = firstUnreviewed(state.currentIndex);
    if (next >= 0) moveTo(next);
  });
  $("#restart-button").addEventListener("click", () => {
    state.showCompletedSamples = true;
    moveTo(0);
  });
  $("#reload-button").addEventListener("click", loadSession);
  window.addEventListener("keydown", onKeyboard);
}

wireEvents();
loadSession();
