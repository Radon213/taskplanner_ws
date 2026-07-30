import { useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { BrainCircuit, Code2, ListTree, RadioTower } from "lucide-react";

import type { BedRobotArmGroupTrace, useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import type {
  BTDecision,
  CompressedImageFrame,
  SimulationState,
  SkillStatus,
  SurgeonState,
  VLMHealth,
  VLMReducerDecision,
  VLMResult,
  WorldState,
} from "../../types";
import { type Language } from "../../utils/display";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;
type TabId = "bt" | "vlm" | "raw";
type TimelineFilter = "all" | "normal" | "warning" | "error";
type DetailTone = "normal" | "match" | "mismatch";
type PanelVariant = "combined" | "timeline" | "decision";

const VLM_IMPLICIT_REQUEST_EVENTS = new Set([
  "extend_hand_for_handover",
  "implicit_tool_request",
  "request_tool",
]);
const VLM_IMPLICIT_REQUEST_POSES = new Set([
  "hand_extending",
  "open_palm",
  "open_receive",
  "palm_up",
]);

function DetailCard({ label, value, tone = "normal" }: { label: string; value: string | number; tone?: DetailTone }) {
  return (
    <article className={`detail-card tone-${tone}`}>
      <span>{label}</span>
      <strong>{value || "none"}</strong>
    </article>
  );
}

function TimelineMeta({ value }: { value: string }) {
  const parts = value.split(" · ").filter(Boolean);
  if (parts.length <= 1) return <small>{value}</small>;
  return (
    <small>
      {parts.map((part) => (
        <span key={part}>{part}</span>
      ))}
    </small>
  );
}

function compactIdentifier(value: string): string {
  if (value.length <= 24) return value;
  return `${value.slice(0, 14)}…${value.slice(-6)}`;
}

function BedRobotArmGroupTraceCard({
  trace,
  language,
}: {
  trace: BedRobotArmGroupTrace;
  language: Language;
}) {
  const requestLabel = language === "ko" ? "요청 ID" : "Request ID";
  return (
    <article
      className={`bed-group-trace-card tone-${trace.tone}`}
      data-slot="bed-robot-arm-group-trace"
      data-bed-group-request-id={trace.requestId}
      data-bed-group-id={trace.groupId}
      aria-label={`${trace.groupLabel}, ${requestLabel} ${trace.requestId}`}
    >
      <div className="bed-group-trace-card-header">
        <div>
          <strong>{trace.groupLabel}</strong>
          <span>{trace.summary}</span>
        </div>
        <em title={trace.outcomeLabel}>{trace.outcomeLabel}</em>
      </div>
      <code title={trace.requestId}>
        {requestLabel}: {compactIdentifier(trace.requestId)}
      </code>
      <ol aria-label={language === "ko" ? "그룹 요청 처리 단계" : "Group request processing stages"}>
        {trace.steps.map((step) => (
          <li
            key={step.id}
            className={`state-${step.state}`}
            data-bed-group-trace-step={step.id}
            title={step.detail}
            aria-label={`${step.label}: ${step.stateLabel}. ${step.detail}`}
          >
            <span className="bed-group-trace-marker" aria-hidden="true" />
            <div>
              <strong>{step.label}</strong>
              <small title={step.detail}>{step.detail}</small>
            </div>
            <em>{step.stateLabel}</em>
          </li>
        ))}
      </ol>
    </article>
  );
}

function parseVlmToolLabel(vlmResult: VLMResult, displayToolName: (toolId: string) => string, noneLabel: string): string {
  if (!vlmResult.raw_json) return noneLabel;
  try {
    const payload = JSON.parse(vlmResult.raw_json) as { tool?: unknown };
    const rawTool = payload.tool;
    if (Array.isArray(rawTool) && Array.isArray(rawTool[0])) {
      const first = rawTool[0] as unknown[];
      const toolId = String(first[0] || "");
      const confidence = Number(first[1] || 0);
      return toolId ? `${displayToolName(toolId)} (${Math.round(confidence * 100)}%)` : noneLabel;
    }
    if (Array.isArray(rawTool) && rawTool.length === 2) {
      const toolId = String(rawTool[0] || "");
      const confidence = Number(rawTool[1] || 0);
      return toolId ? `${displayToolName(toolId)} (${Math.round(confidence * 100)}%)` : noneLabel;
    }
  } catch {
    return noneLabel;
  }
  return noneLabel;
}

function parseVlmMayoLabel(
  vlmResult: VLMResult,
  displayToolName: (toolId: string) => string,
  noneLabel: string,
  language: Language,
): string {
  if (!vlmResult.raw_json) return noneLabel;
  try {
    const payload = JSON.parse(vlmResult.raw_json) as { mayo?: unknown };
    if (!Array.isArray(payload.mayo)) return noneLabel;
    const rows = payload.mayo.flatMap((row) => {
      if (!Array.isArray(row) || row.length < 3) return [];
      const toolId = String(row[0] ?? "");
      const decision = String(row[1] ?? "").toLowerCase();
      const confidence = Number(row[2]);
      if (!toolId || !Number.isFinite(confidence)) return [];
      const decisionLabel =
        decision === "reuse"
          ? language === "ko"
            ? "재사용"
            : "reuse"
          : language === "ko"
            ? "회수"
            : "recover";
      return [`${displayToolName(toolId)} ${decisionLabel} ${Math.round(confidence * 100)}%`];
    });
    return rows.length ? rows.join(" · ") : noneLabel;
  } catch {
    return noneLabel;
  }
}

export function ObservabilityPanel({
  vm,
  language,
  btDecision,
  skillStatus,
  simulationState,
  worldState,
  surgeonState,
  vlmHealth,
  vlmResult,
  vlmReducerDecisions,
  vlmImage,
  variant = "combined",
}: {
  vm: ViewModel;
  language: Language;
  btDecision: BTDecision;
  skillStatus: SkillStatus;
  simulationState: SimulationState;
  worldState: WorldState;
  surgeonState: SurgeonState;
  vlmHealth: VLMHealth;
  vlmResult: VLMResult;
  vlmReducerDecisions: VLMReducerDecision[];
  vlmImage: CompressedImageFrame | null;
  variant?: PanelVariant;
}) {
  const [tab, setTab] = useState<TabId>("bt");
  const [timelineFilter, setTimelineFilter] = useState<TimelineFilter>("all");
  const timelineStripRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);
  const [followLatest, setFollowLatest] = useState(true);
  const prefersReducedMotion = useReducedMotion();
  const newestFirstTimeline = useMemo(() => vm.timeline, [vm.timeline]);
  const timelineCounts = useMemo(
    () => ({
      all: newestFirstTimeline.length,
      normal: newestFirstTimeline.filter((item) => item.severity === "normal").length,
      warning: newestFirstTimeline.filter((item) => item.severity === "warning").length,
      error: newestFirstTimeline.filter((item) => item.severity === "error").length,
    }),
    [newestFirstTimeline],
  );
  const filteredTimeline =
    timelineFilter === "all"
      ? newestFirstTimeline
      : newestFirstTimeline.filter((item) => item.severity === timelineFilter);
  const visibleTimeline = filteredTimeline.slice(0, 8);
  const latestTimelineId = visibleTimeline[0]?.uiId ?? `${timelineFilter}-empty`;
  const timelineFilters: Array<{ id: TimelineFilter; label: string }> = [
    { id: "all", label: vm.ui.timelineAll },
    { id: "normal", label: vm.ui.timelineNormal },
    { id: "warning", label: vm.ui.timelineWarning },
    { id: "error", label: vm.ui.timelineError },
  ];
  const groundPhase = surgeonState.phase_id || "";
  const vlmPhase = vlmResult.phase_ids[0] || "";
  const systemPhase = simulationState.filtered_phase || "";
  const groundLabel = groundPhase ? vm.displayPhaseName(groundPhase) : vm.ui.none;
  const vlmPhaseLabel = vlmPhase
    ? `${vm.displayPhaseName(vlmPhase)} (${Math.round((vlmResult.phase_confidences[0] || 0) * 100)}%)`
    : vm.ui.none;
  const systemPhaseLabel = systemPhase ? vm.displayPhaseName(systemPhase) : vm.ui.none;
  const vlmMatchesGround = Boolean(groundPhase && vlmPhase && groundPhase === vlmPhase);
  const systemMatchesGround = Boolean(groundPhase && systemPhase && groundPhase === systemPhase);
  const vlmPhaseTone: DetailTone = groundPhase && vlmPhase ? (vlmMatchesGround ? "match" : "mismatch") : "normal";
  const systemPhaseTone: DetailTone = groundPhase && systemPhase ? (systemMatchesGround ? "match" : "mismatch") : "normal";
  const phaseCheckTone: DetailTone = groundPhase && vlmPhase && systemPhase ? (vlmMatchesGround && systemMatchesGround ? "match" : "mismatch") : "normal";
  const phaseMatchLabel = groundPhase
    ? language === "ko"
      ? `VLM ${vlmMatchesGround ? "일치" : "불일치"} · 시스템 ${systemMatchesGround ? "일치" : "불일치"}`
      : `VLM ${vlmMatchesGround ? "match" : "mismatch"} · system ${systemMatchesGround ? "match" : "mismatch"}`
    : vm.ui.none;
  const rawVlmToolLabel = parseVlmToolLabel(vlmResult, vm.displayToolName, vm.ui.none);
  const rawVlmMayoLabel = parseVlmMayoLabel(
    vlmResult,
    vm.displayToolName,
    vm.ui.none,
    language,
  );
  const implicitRequestDetected =
    VLM_IMPLICIT_REQUEST_EVENTS.has(vlmResult.gesture_event_type) &&
    VLM_IMPLICIT_REQUEST_POSES.has(vlmResult.gesture_hand_pose) &&
    vlmResult.gesture_confidence > 0;
  const implicitRequestToolLabel = vlmResult.gesture_requested_tool
    ? vm.displayToolName(vlmResult.gesture_requested_tool)
    : language === "ko"
      ? "도구 미확정"
      : "tool unresolved";
  const implicitRequestLabel = implicitRequestDetected
    ? language === "ko"
      ? `감지 · ${implicitRequestToolLabel} (${Math.round(vlmResult.gesture_confidence * 100)}%)`
      : `Detected · ${implicitRequestToolLabel} (${Math.round(vlmResult.gesture_confidence * 100)}%)`
    : language === "ko"
      ? "감지 안 됨"
      : "Not detected";
  const finalMayoLabel = simulationState.instrument_states
    .filter((instrument) =>
      instrument.lifecycle_stage === "mayo_reuse" ||
      instrument.lifecycle_stage === "mayo_recovery" ||
      instrument.location_type === "mayo_stand" ||
      instrument.location_type === "mayo_reuse_zone" ||
      instrument.location_type === "mayo_recovery_zone")
    .map((instrument) => {
      const recovery =
        instrument.lifecycle_stage === "mayo_recovery" ||
        instrument.next_required_transition === "recover_left" ||
        simulationState.active_recovery_tools.includes(instrument.instrument_id);
      const decision = recovery
        ? language === "ko" ? "회수 확정" : "recovery"
        : language === "ko" ? "재사용 유지" : "keep for reuse";
      return `${vm.displayToolName(instrument.instrument_id)} ${decision}`;
    })
    .join(" · ") || vm.ui.none;
  const systemPredictedToolLabel = worldState.predicted_tool
    ? `${vm.displayToolName(worldState.predicted_tool)} (${Math.round((worldState.predicted_tool_confidence || 0) * 100)}%, ${Math.round(worldState.predicted_tool_stability_sec || 0)}s)`
    : vm.ui.none;

  useLayoutEffect(() => {
    if (!followLatestRef.current) return;
    const strip = timelineStripRef.current;
    if (!strip) return;
    strip.scrollTo({ left: 0, behavior: "auto" });
    window.requestAnimationFrame(() => {
      if (followLatestRef.current) {
        strip.scrollTo({ left: 0, behavior: "auto" });
      }
    });
  }, [latestTimelineId]);

  function handleTimelineScroll() {
    const strip = timelineStripRef.current;
    if (!strip) return;
    const nextFollowLatest = strip.scrollLeft <= 4;
    if (followLatestRef.current === nextFollowLatest) return;
    followLatestRef.current = nextFollowLatest;
    setFollowLatest(nextFollowLatest);
  }

  function handleFilterChange(nextFilter: TimelineFilter) {
    followLatestRef.current = true;
    setFollowLatest(true);
    setTimelineFilter(nextFilter);
    window.requestAnimationFrame(() => {
      timelineStripRef.current?.scrollTo({ left: 0, behavior: "auto" });
    });
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, currentTab: TabId) {
    const tabOrder: TabId[] = ["bt", "vlm", "raw"];
    const currentIndex = tabOrder.indexOf(currentTab);
    let nextTab: TabId | undefined;
    if (event.key === "ArrowRight") nextTab = tabOrder[(currentIndex + 1) % tabOrder.length];
    if (event.key === "ArrowLeft") nextTab = tabOrder[(currentIndex - 1 + tabOrder.length) % tabOrder.length];
    if (event.key === "Home") nextTab = tabOrder[0];
    if (event.key === "End") nextTab = tabOrder[tabOrder.length - 1];
    if (!nextTab) return;
    event.preventDefault();
    setTab(nextTab);
    window.requestAnimationFrame(() => document.getElementById(`observability-tab-${nextTab}`)?.focus());
  }

  const timelinePanel = (
      <div className="timeline-panel">
        <div className="panel-title-row">
          <div>
            <p className="section-kicker">{vm.ui.timeline}</p>
            <h2>{vm.ui.timelineLog}</h2>
          </div>
          <div className="timeline-toolbar">
            <div className="timeline-filter" role="group" aria-label={vm.ui.timelineFilter}>
              {timelineFilters.map((filter) => (
                <button
                  key={filter.id}
                  type="button"
                  aria-pressed={timelineFilter === filter.id}
                  data-timeline-filter={filter.id}
                  className={timelineFilter === filter.id ? "active" : ""}
                  onClick={() => handleFilterChange(filter.id)}
                >
                  <span>{filter.label}</span>
                  <small>{timelineCounts[filter.id]}</small>
                </button>
              ))}
            </div>
            <RadioTower size={18} />
          </div>
        </div>
        <div
          className="timeline-strip"
          data-follow-latest={followLatest ? "true" : "false"}
          ref={timelineStripRef}
          onScroll={handleTimelineScroll}
        >
          <AnimatePresence initial={false}>
            {visibleTimeline.map((item, index) => (
              <motion.article
                key={item.uiId || item.id}
                layout="position"
                data-timeline-index={index}
                data-timeline-ui-id={item.uiId}
                data-timeline-severity={item.severity}
                data-bed-group-request-id={item.requestId}
                data-bed-group-id={item.groupId}
                className={`timeline-item ${item.tone} severity-${item.severity}`}
                initial={prefersReducedMotion ? false : { opacity: 0, x: -26 }}
                animate={{ opacity: 1, x: 0 }}
                exit={prefersReducedMotion ? undefined : { opacity: 0, x: 22 }}
                transition={
                  prefersReducedMotion
                    ? { duration: 0 }
                    : {
                        opacity: { duration: 0.16 },
                        x: { duration: 0.22, ease: [0.22, 1, 0.36, 1] },
                        layout: { duration: 0.34, ease: [0.22, 1, 0.36, 1] },
                      }
                }
              >
                <span />
                <strong>{item.title}</strong>
                <TimelineMeta value={item.meta} />
              </motion.article>
            ))}
          </AnimatePresence>
          {visibleTimeline.length === 0 ? (
            <div className="timeline-empty">
              {language === "ko" ? "이 필터에 표시할 이벤트가 없습니다." : "No events match this filter."}
            </div>
          ) : null}
        </div>
      </div>
  );

  const decisionPanel = (
      <div className="explain-panel">
        <div className="panel-title-row compact">
          <div>
            <p className="section-kicker">{vm.ui.observability}</p>
            <h2>{tab === "bt" ? vm.ui.bt : tab === "vlm" ? vm.ui.vlm : vm.ui.rawResult}</h2>
          </div>
          <div className="tab-switch" role="tablist" aria-label={vm.ui.observability}>
            <button
              id="observability-tab-bt"
              className={tab === "bt" ? "active" : ""}
              onClick={() => setTab("bt")}
              type="button"
              role="tab"
              aria-selected={tab === "bt"}
              aria-controls="observability-panel-bt"
              tabIndex={tab === "bt" ? 0 : -1}
              onKeyDown={(event) => handleTabKeyDown(event, "bt")}
            >
              <ListTree size={15} />
              {vm.ui.bt}
            </button>
            <button
              id="observability-tab-vlm"
              className={tab === "vlm" ? "active" : ""}
              onClick={() => setTab("vlm")}
              type="button"
              role="tab"
              aria-selected={tab === "vlm"}
              aria-controls="observability-panel-vlm"
              tabIndex={tab === "vlm" ? 0 : -1}
              onKeyDown={(event) => handleTabKeyDown(event, "vlm")}
            >
              <BrainCircuit size={15} />
              {vm.ui.vlm}
            </button>
            <button
              id="observability-tab-raw"
              className={tab === "raw" ? "active" : ""}
              onClick={() => setTab("raw")}
              type="button"
              role="tab"
              aria-selected={tab === "raw"}
              aria-controls="observability-panel-raw"
              tabIndex={tab === "raw" ? 0 : -1}
              onKeyDown={(event) => handleTabKeyDown(event, "raw")}
            >
              <Code2 size={15} />
              Raw
            </button>
          </div>
        </div>

        <div className="decision-scroll">
          <section className="bed-group-trace-section" aria-labelledby="bed-group-trace-title">
            <div className="bed-group-trace-title-row">
              <div>
                <h3 id="bed-group-trace-title">
                  {language === "ko" ? "베드 로봇암 요청 추적" : "Bed robot request trace"}
                </h3>
                <p>
                  {language === "ko"
                    ? "같은 요청 ID로 발화부터 그룹 상태까지 연결합니다."
                    : "Links the utterance to group status with one request ID."}
                </p>
              </div>
              <span aria-label={language === "ko" ? "최근 요청 수" : "Recent request count"}>
                {vm.bedRobotArmGroupTraces.length}
              </span>
            </div>
            {vm.bedRobotArmGroupTraces.length ? (
              <div className="bed-group-trace-list" aria-live="polite">
                {vm.bedRobotArmGroupTraces.slice(0, 2).map((trace) => (
                  <BedRobotArmGroupTraceCard key={trace.requestId} trace={trace} language={language} />
                ))}
              </div>
            ) : (
              <p className="bed-group-trace-empty">
                {language === "ko" ? "아직 관측된 그룹 요청이 없습니다." : "No group request has been observed yet."}
              </p>
            )}
          </section>

          <AnimatePresence mode="wait">
            {tab === "bt" ? (
              <motion.div
                id="observability-panel-bt"
                key="bt"
                className="detail-grid"
                role="tabpanel"
                aria-labelledby="observability-tab-bt"
                initial={prefersReducedMotion ? false : { opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={prefersReducedMotion ? undefined : { opacity: 0, y: -8 }}
                transition={{ duration: prefersReducedMotion ? 0 : 0.18 }}
              >
                <DetailCard label={vm.ui.selectedTool} value={btDecision.selected_tool ? vm.displayToolName(btDecision.selected_tool) : vm.ui.none} />
                <DetailCard
                  label={vm.ui.lifecycle}
                  value={btDecision.selected_tool_lifecycle ? vm.displayLifecycleName(btDecision.selected_tool_lifecycle) : vm.ui.none}
                />
                <DetailCard
                  label={vm.ui.nextTransition}
                  value={btDecision.next_required_transition ? vm.displayTransitionName(btDecision.next_required_transition) : vm.ui.none}
                />
                <DetailCard label={vm.ui.guard} value={btDecision.blocking_guard || vm.ui.none} />
                <DetailCard label={vm.ui.skill} value={skillStatus.action ? vm.displayActionName(skillStatus.action) : vm.ui.none} />
                <DetailCard label={vm.ui.progress} value={`${Math.round((skillStatus.progress || 0) * 100)}%`} />
                <article className="detail-card wide">
                  <span>{vm.ui.rationale}</span>
                  <strong>{btDecision.decision_reason || btDecision.rationale || vm.ui.none}</strong>
                </article>
              </motion.div>
            ) : null}

            {tab === "vlm" ? (
              <motion.div
                id="observability-panel-vlm"
                key="vlm"
                className="detail-grid"
                role="tabpanel"
                aria-labelledby="observability-tab-vlm"
                initial={prefersReducedMotion ? false : { opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={prefersReducedMotion ? undefined : { opacity: 0, y: -8 }}
                transition={{ duration: prefersReducedMotion ? 0 : 0.18 }}
              >
                <DetailCard
                  label={language === "ko" ? "VLM 입력 영상" : "VLM input image"}
                  value={
                    vlmImage
                      ? language === "ko"
                        ? `${vlmHealth.image_source === "flir_raw_fallback" ? "원본 FLIR 폴백" : "RF-DETR 분할 FLIR"} · ${Math.max(1, Math.round(vlmImage.sizeBytes / 1024))} KB`
                        : `${vlmHealth.image_source === "flir_raw_fallback" ? "Raw FLIR fallback" : "RF-DETR segmented FLIR"} · ${Math.max(1, Math.round(vlmImage.sizeBytes / 1024))} KB`
                      : language === "ko"
                        ? "frame 없음"
                        : "no frame"
                  }
                />
                <DetailCard label={vm.ui.connection} value={vm.vlmStatus.connection} />
                <DetailCard label={vm.ui.health} value={vm.vlmStatus.health} />
                <DetailCard label={vm.ui.model} value={vlmHealth.model_id || vm.ui.none} />
                <DetailCard label={vm.ui.mode} value={vlmHealth.last_mode || vm.ui.none} />
                <DetailCard label={vm.ui.source} value={vlmResult.source || vm.ui.none} />
                <DetailCard label={vm.ui.imageSource} value={vlmHealth.image_source || vm.ui.none} />
                <DetailCard
                  label={language === "ko" ? "암묵적 도구 요청" : "Implicit tool request"}
                  value={implicitRequestLabel}
                  tone={implicitRequestDetected ? "match" : "normal"}
                />
                <DetailCard label={vm.ui.latency} value={vlmHealth.latency_sec ? `${vlmHealth.latency_sec.toFixed(3)}s` : vm.ui.none} />
                <DetailCard label={language === "ko" ? "집도의 정답 단계" : "Actor ground"} value={groundLabel} />
                <DetailCard label={language === "ko" ? "VLM 제안 단계" : "VLM proposed phase"} value={vlmPhaseLabel} tone={vlmPhaseTone} />
                <DetailCard label={language === "ko" ? "시스템 최종 단계" : "System final phase"} value={systemPhaseLabel} tone={systemPhaseTone} />
                <DetailCard label={language === "ko" ? "단계 검증" : "Phase check"} value={phaseMatchLabel} tone={phaseCheckTone} />
                <DetailCard label={language === "ko" ? "VLM 제안 다음 도구" : "VLM proposed next tool"} value={rawVlmToolLabel} />
                <DetailCard label={language === "ko" ? "시스템 최종 다음 도구" : "System final next tool"} value={systemPredictedToolLabel} />
                <article className="detail-card wide">
                  <span>{language === "ko" ? "Mayo VLM 원시 판단" : "Raw VLM Mayo decision"}</span>
                  <strong>{rawVlmMayoLabel}</strong>
                </article>
                <article className="detail-card wide">
                  <span>{language === "ko" ? "Mayo DT 최종 판단" : "Final DT Mayo decision"}</span>
                  <strong>{finalMayoLabel}</strong>
                </article>
                <DetailCard
                  label={language === "ko" ? "E2E 지표 형식" : "E2E metric format"}
                  value={language === "ko" ? "정답 / 제안 / 평가가능" : "correct / proposed / evaluable"}
                />
                <article className="detail-card wide">
                  <span>{language === "ko" ? "임상 분석" : "Clinical analysis"}</span>
                  <strong>{vlmResult.summary || vm.ui.none}</strong>
                </article>
                <article className="detail-card wide">
                  <span>{vm.ui.reducer}</span>
                  <strong>
                    {vlmReducerDecisions[0]
                      ? `${vlmReducerDecisions[0].reducer_result}: ${vlmReducerDecisions[0].reducer_reason}`
                      : vm.ui.none}
                  </strong>
                </article>
              </motion.div>
            ) : null}

            {tab === "raw" ? (
              <motion.pre
                id="observability-panel-raw"
                key="raw"
                className="raw-block"
                role="tabpanel"
                aria-labelledby="observability-tab-raw"
                initial={prefersReducedMotion ? false : { opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={prefersReducedMotion ? undefined : { opacity: 0, y: -8 }}
                transition={{ duration: prefersReducedMotion ? 0 : 0.18 }}
              >
                {vlmResult.raw_json || "{}"}
              </motion.pre>
            ) : null}
          </AnimatePresence>
        </div>
      </div>
  );

  if (variant === "timeline") {
    return <section className="observability-panel observability-panel-timeline">{timelinePanel}</section>;
  }

  if (variant === "decision") {
    return <section className="observability-panel observability-panel-decision">{decisionPanel}</section>;
  }

  return (
    <section className="observability-panel">
      {timelinePanel}
      {decisionPanel}
    </section>
  );
}
