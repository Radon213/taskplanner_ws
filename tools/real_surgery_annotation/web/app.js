const state = {
  data: null,
  selected: null,
  currentTime: 0,
  toastTimer: null,
  frameAbort: null,
};

const $ = (selector) => document.querySelector(selector);
const queue = $("#queue");
const form = $("#event-form");
const timeline = $("#timeline");

function toast(message, duration = 3000) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    node.hidden = true;
  }, duration);
}

function fillSelect(node, values, label = (value) => value) {
  node.replaceChildren(
    ...values.map((value) => {
      const option = document.createElement("option");
      option.value = typeof value === "string" ? value : value.id;
      option.textContent = label(value);
      return option;
    }),
  );
}

async function loadState() {
  document.querySelector("main").setAttribute("aria-busy", "true");
  const response = await fetch("/api/state", { cache: "no-store" });
  if (!response.ok) throw new Error("검토 상태를 불러오지 못했습니다.");
  state.data = await response.json();
  timeline.max = state.data.duration_sec;
  fillSelect($("#event-type"), state.data.vocabulary.event_types);
  fillSelect($("#tool-id"), state.data.tools, (tool) => tool.name);
  fillSelect($("#from-holder"), state.data.vocabulary.holders);
  fillSelect($("#to-holder"), state.data.vocabulary.holders);
  fillSelect($("#from-location"), state.data.vocabulary.locations);
  fillSelect($("#to-location"), state.data.vocabulary.locations);
  fillSelect($("#visibility"), state.data.vocabulary.visibility);
  renderQueue();
  const current =
    state.data.candidates.find((item) => item.event_id === state.selected?.event_id) ||
    state.data.candidates[0] ||
    state.data.events[0] ||
    null;
  if (current) selectEvent(current);
  document.querySelector("main").setAttribute("aria-busy", "false");
}

function renderQueue() {
  const candidates = state.data.candidates;
  $("#queue-loading").hidden = true;
  $("#queue-count").innerHTML = `${candidates.length}<span>건</span>`;
  $("#queue-empty").hidden = candidates.length !== 0;
  queue.hidden = candidates.length === 0;
  queue.replaceChildren(
    ...candidates.map((event) => {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "queue-item";
      button.dataset.eventId = event.event_id;
      button.setAttribute(
        "aria-current",
        String(state.selected?.event_id === event.event_id),
      );
      const main = document.createElement("span");
      const title = document.createElement("strong");
      title.textContent = event.tool.name;
      const meta = document.createElement("span");
      meta.textContent = `${event.event_type} · ${event.time_sec.toFixed(3)} s`;
      main.append(title, meta);
      const id = document.createElement("code");
      id.textContent = event.event_id;
      button.append(main, id);
      button.addEventListener("click", () => selectEvent(event));
      li.append(button);
      return li;
    }),
  );
}

function setValue(selector, value) {
  $(selector).value = value ?? "";
}

function ensureSelectedToolOption(tool) {
  const select = $("#tool-id");
  select
    .querySelectorAll("option[data-proposal-tool]")
    .forEach((option) => option.remove());
  if (state.data.tools.some((item) => item.id === tool.id)) return;

  const option = document.createElement("option");
  option.value = tool.id;
  option.textContent = `${tool.name} · 미확정`;
  option.dataset.proposalTool = "true";
  select.append(option);
}

function selectEvent(event) {
  state.selected = structuredClone(event);
  state.currentTime = Number(event.time_sec);
  setValue("#event-type", event.event_type);
  setValue("#event-time", event.time_sec);
  ensureSelectedToolOption(event.tool);
  setValue("#tool-id", event.tool.id);
  setValue("#instance-id", event.tool.instance_id);
  setValue("#from-holder", event.from?.holder ?? "unknown");
  setValue("#from-location", event.from?.location ?? "unknown");
  setValue("#to-holder", event.to.holder);
  setValue("#to-location", event.to.location);
  setValue("#visibility", event.visibility);
  $("#view-cam4").checked = event.source_views.includes("cam4");
  $("#view-flir").checked = event.source_views.includes("flir");
  $("#status-badge").textContent = event.review_status;
  $("#review-notes").value = event.review?.notes || event.notes || "";
  timeline.value = state.currentTime;
  renderQueue();
  updateTime();
  refreshFrames();
}

function newEvent() {
  const index = state.data.events.length + state.data.candidates.length + 1;
  selectEvent({
    schema: "taskplanner.observable_tool_event.v1",
    case_id: state.data.case_id,
    event_id: "NEW",
    event_type: "tool_transfer",
    time_sec: state.currentTime,
    tool: {
      id: state.data.tools[0].id,
      name: state.data.tools[0].name,
      instance_id: `${state.data.case_id}-tool-manual_${String(index).padStart(4, "0")}`,
    },
    from: { holder: "unknown", location: "unknown" },
    to: { holder: "unknown", location: "unknown" },
    derived_action: "relocate",
    source_views: ["cam4", "flir"],
    visibility: "partial",
    review_status: "proposed",
    label_origin: "human_video_review",
    notes: "",
  });
}

function updateTime() {
  state.currentTime = Math.max(
    0,
    Math.min(Number(timeline.value), Number(state.data.duration_sec)),
  );
  timeline.value = state.currentTime;
  $("#time-output").textContent = `${state.currentTime.toFixed(3)} s`;
  $("#event-time").value = state.currentTime.toFixed(3);
}

async function drawFrame(view, canvas, stampNode) {
  const response = await fetch(
    `/api/frame?view=${encodeURIComponent(view)}&time_sec=${state.currentTime.toFixed(9)}`,
  );
  if (!response.ok) throw new Error(`${view} frame`);
  const blob = await response.blob();
  const bitmap = await createImageBitmap(blob);
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  const context = canvas.getContext("2d");
  context.drawImage(bitmap, 0, 0);
  if (
    view === "cam4" &&
    state.selected?.proposal?.bbox_xywh_px &&
    Math.abs(Number(state.selected.time_sec) - state.currentTime) < 0.001
  ) {
    const [x, y, width, height] = state.selected.proposal.bbox_xywh_px;
    const tokens = getComputedStyle(document.documentElement);
    context.lineWidth = Math.max(3, bitmap.width / 320);
    context.strokeStyle = tokens.getPropertyValue("--card").trim();
    context.strokeRect(x, y, width, height);
    context.lineWidth = Math.max(1.5, bitmap.width / 640);
    context.strokeStyle = tokens.getPropertyValue("--brand").trim();
    context.strokeRect(x, y, width, height);
  }
  const actualNs = Number(response.headers.get("X-Bag-Timestamp-Ns"));
  stampNode.textContent = `${(actualNs / 1e9).toFixed(3)} s`;
}

let frameSequence = 0;
async function refreshFrames() {
  const sequence = ++frameSequence;
  $("#viewer-error").hidden = true;
  try {
    await Promise.all([
      drawFrame("cam4", $("#cam4-canvas"), $("#cam4-stamp")),
      drawFrame("flir", $("#flir-canvas"), $("#flir-stamp")),
    ]);
    if (sequence !== frameSequence) return;
  } catch (error) {
    if (sequence !== frameSequence) return;
    $("#viewer-error").hidden = false;
  }
}

function eventFromForm() {
  const toolId = $("#tool-id").value;
  const tool =
    state.data.tools.find((item) => item.id === toolId) ||
    (state.selected?.tool?.id === toolId ? state.selected.tool : null);
  if (!tool) {
    throw new Error("도구 정보를 불러오지 못했습니다. 후보를 다시 선택해 주세요.");
  }
  const eventType = $("#event-type").value;
  const sourceViews = ["cam4", "flir"].filter((view) => $(`#view-${view}`).checked);
  return {
    ...state.selected,
    event_type: eventType,
    time_sec: Number($("#event-time").value),
    tool: {
      id: tool.id,
      name: tool.name,
      instance_id: $("#instance-id").value.trim(),
    },
    from:
      eventType === "initial_state"
        ? null
        : {
            holder: $("#from-holder").value,
            location: $("#from-location").value,
          },
    to: {
      holder: $("#to-holder").value,
      location: $("#to-location").value,
    },
    source_views: sourceViews,
    visibility: $("#visibility").value,
  };
}

async function saveReview(reviewStatus) {
  if (!state.selected) return;
  const reviewerId = $("#reviewer").value.trim();
  if (!reviewerId) {
    $("#reviewer").focus();
    toast("검토자 ID를 먼저 입력해 주세요.");
    return;
  }
  let event;
  try {
    event = eventFromForm();
  } catch (error) {
    toast(error.message || "입력 내용을 확인해 주세요.", 5000);
    return;
  }

  const actionButtons = ["#confirm", "#ambiguous", "#reject"].map($);
  actionButtons.forEach((button) => {
    button.disabled = true;
  });
  form.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        revision: state.data.revision,
        reviewer_id: reviewerId,
        review_status: reviewStatus,
        review_notes: $("#review-notes").value,
        event,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      toast(result.error || "저장하지 못했습니다.", 5000);
      return;
    }
    state.data = result.state;
    state.selected = null;
    renderQueue();
    const next = state.data.candidates[0] || state.data.events.at(-1);
    if (next) selectEvent(next);
    toast(`${result.event_id} · ${reviewStatus} 저장됨`);
  } catch {
    toast("저장 요청을 보내지 못했습니다. 잠시 후 다시 시도해 주세요.", 5000);
  } finally {
    actionButtons.forEach((button) => {
      button.disabled = false;
    });
    form.setAttribute("aria-busy", "false");
  }
}

timeline.addEventListener("input", () => {
  updateTime();
});
timeline.addEventListener("change", refreshFrames);
$("#event-time").addEventListener("change", () => {
  timeline.value = $("#event-time").value;
  updateTime();
  refreshFrames();
});
$("#event-type").addEventListener("change", () => {
  const initial = $("#event-type").value === "initial_state";
  $("#from-holder").disabled = initial;
  $("#from-location").disabled = initial;
  if (initial) {
    timeline.value = 0;
    updateTime();
    refreshFrames();
  }
});
document.querySelectorAll("[data-step]").forEach((button) => {
  button.addEventListener("click", () => {
    timeline.value = state.currentTime + Number(button.dataset.step);
    updateTime();
    refreshFrames();
  });
});
$("#retry-frame").addEventListener("click", refreshFrames);
$("#confirm").addEventListener("click", () => saveReview("confirmed"));
$("#ambiguous").addEventListener("click", () => saveReview("ambiguous"));
$("#reject").addEventListener("click", () => saveReview("rejected"));
$("#add-event").addEventListener("click", newEvent);
$("#empty-add").addEventListener("click", newEvent);

document.addEventListener("keydown", (event) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  if (event.key === "Enter") saveReview("confirmed");
  if (event.key.toLowerCase() === "a") saveReview("ambiguous");
  if (event.key.toLowerCase() === "r") saveReview("rejected");
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    const amount = (event.shiftKey ? 1 : 0.07043) * (event.key === "ArrowLeft" ? -1 : 1);
    timeline.value = state.currentTime + amount;
    updateTime();
    refreshFrames();
  }
});

setTimeout(() => {
  loadState().catch((error) => {
    $("#queue-loading").hidden = true;
    $("#queue-empty").hidden = false;
    $("#queue-empty strong").textContent = "검토 상태를 불러오지 못했습니다";
    $("#queue-empty p").textContent = error.message;
  });
}, 300);
