import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { AnimatePresence, useReducedMotion } from "framer-motion";
import * as m from "framer-motion/m";
import { BrainCircuit, Code2, ListTree, RadioTower } from "lucide-react";

import type { BedRobotArmTrace, useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import type {
  BTDecision,
  CompressedImageFrame,
  InputSourceStatus,
  InstrumentState,
  SimulationState,
  SkillStatus,
  SurgeonState,
  VLMHealth,
  VLMReducerDecision,
  VLMResult,
  WorldState,
} from "../../types";
import { MOTION_DURATION, SILK_EASE } from "../../motion-system";
import { parseBoundedJson, type Language } from "../../utils/display";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;
type TabId = "bt" | "vlm" | "raw";
type TimelineFilter = "all" | "normal" | "warning" | "error";
type DetailTone = "normal" | "match" | "mismatch";
type PanelVariant = "combined" | "timeline" | "decision";

const MAX_DISPLAYED_NEXT_TOOL_RANK = 3;

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

function isCanonicalMayoInstrument(instrument: InstrumentState): boolean {
  return (
    instrument.location_type === "mayo_stand"
    && instrument.location_id === "mayo_stand"
    && (instrument.lifecycle_stage === "mayo_reuse" || instrument.lifecycle_stage === "mayo_recovery")
  );
}

type RankedToolRow = {
  rank: number;
  toolId: string;
  confidence: number;
};

function rankedVlmToolRows(vlmResult: VLMResult): RankedToolRow[] {
  if (!vlmResult.raw_json) return [];
  try {
    const parsed = parseBoundedJson(vlmResult.raw_json);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];
    const rawTool = (parsed as { tool?: unknown }).tool;
    if (!Array.isArray(rawTool)) return [];
    const rawRows = Array.isArray(rawTool[0])
      ? rawTool
      : rawTool.length === 2 ? [rawTool] : [];
    const candidates = new Map<string, number>();
    for (const row of rawRows.slice(0, 24)) {
      if (!Array.isArray(row) || row.length !== 2) continue;
      const toolId = String(row[0] ?? "").trim();
      const confidence = Number(row[1]);
      if (!toolId || !Number.isFinite(confidence) || confidence < 0 || confidence > 1) continue;
      const previous = candidates.get(toolId);
      if (previous === undefined || confidence > previous) candidates.set(toolId, confidence);
    }
    return [...candidates.entries()]
      .map(([toolId, confidence]) => ({ toolId, confidence }))
      .sort((left, right) => right.confidence - left.confidence || left.toolId.localeCompare(right.toolId))
      .slice(0, MAX_DISPLAYED_NEXT_TOOL_RANK)
      .map((row, index) => ({ ...row, rank: index + 1 }));
  } catch {
    return [];
  }
}

function formatRankedToolLabel(
  rows: readonly RankedToolRow[],
  displayToolName: (toolId: string) => string,
  noneLabel: string,
  language: Language,
): string {
  if (!rows.length) return noneLabel;
  return rows
    .map((row) => `${language === "ko" ? `${row.rank}순위` : `#${row.rank}`} ${displayToolName(row.toolId)} (${Math.round(row.confidence * 100)}%)`)
    .join(" · ");
}

function systemRankedToolRows(worldState: WorldState): RankedToolRow[] {
  const seenRanks = new Set<number>();
  const seenTools = new Set<string>();
  const rows: RankedToolRow[] = [];
  for (const value of worldState.ranked_tool_predictions.slice(0, 24)) {
    const rank = Number(value.rank);
    const toolId = String(value.instrument_id || "").trim();
    const confidence = Number(value.confidence);
    if (
      !Number.isInteger(rank)
      || rank < 1
      || rank > MAX_DISPLAYED_NEXT_TOOL_RANK
      || !toolId
      || !Number.isFinite(confidence)
      || confidence < 0
      || confidence > 1
      || seenRanks.has(rank)
      || seenTools.has(toolId)
    ) {
      continue;
    }
    seenRanks.add(rank);
    seenTools.add(toolId);
    rows.push({ rank, toolId, confidence });
  }
  return rows
    .sort((left, right) => left.rank - right.rank || left.toolId.localeCompare(right.toolId))
    .slice(0, MAX_DISPLAYED_NEXT_TOOL_RANK);
}

function vlmInputImageLabel(
  imageSource: string,
  sizeBytes: number,
  receivedAt: number,
  nowMs: number,
  language: Language,
): string {
  const source = String(imageSource || "");
  const sizeLabel = `${Math.max(1, Math.round(sizeBytes / 1024))} KB`;
  const ageMs = Math.max(0, nowMs - receivedAt);
  const ageLabel = ageMs < 1_000
    ? language === "ko" ? "방금" : "just now"
    : `${(ageMs / 1_000).toFixed(1)}${language === "ko" ? "초" : "s"}`;
  const ageSuffix = ageMs < 1_000 ? "" : language === "ko" ? " 전" : " ago";
  const freshnessLabel = ageMs > 3_000
    ? language === "ko" ? "오래된 프레임" : "stale frame"
    : language === "ko" ? "마지막 수신" : "last received";
  const isFlirCam4Visual = source.startsWith("flir_cam4_");

  if (language === "ko") {
    const viewLabel = isFlirCam4Visual
      ? "원본 FLIR + CAM4 모델 시각 문맥"
      : "원본 모델 시각 문맥";
    return `${viewLabel} · 도구 위치 근거 아님 · ${sizeLabel} · ${freshnessLabel} ${ageLabel}${ageSuffix}`;
  }

  const viewLabel = isFlirCam4Visual
    ? "Raw FLIR + CAM4 model visual context"
    : "Raw model visual context";
  return `${viewLabel} · not tool-location evidence · ${sizeLabel} · ${freshnessLabel} ${ageLabel}${ageSuffix}`;
}

function BedRobotArmTraceCard({
  trace,
  language,
}: {
  trace: BedRobotArmTrace;
  language: Language;
}) {
  const requestLabel = language === "ko" ? "요청 ID" : "Request ID";
  return (
    <article
      className={`bed-arm-trace-card tone-${trace.tone}`}
      data-slot="bed-robot-arm-trace"
      data-bed-arm-request-id={trace.requestId}
      data-bed-arm-id={trace.armId}
      aria-label={`${trace.armLabel}, ${requestLabel} ${trace.requestId}`}
    >
      <div className="bed-arm-trace-card-header">
        <div>
          <strong>{trace.armLabel}</strong>
          <span>{trace.summary}</span>
        </div>
        <em title={trace.outcomeLabel}>{trace.outcomeLabel}</em>
      </div>
      <code title={trace.requestId}>
        {requestLabel}: {compactIdentifier(trace.requestId)}
      </code>
      <ol aria-label={language === "ko" ? "리트랙션 요청 처리 단계" : "Retraction request processing stages"}>
        {trace.steps.map((step) => (
          <li
            key={step.id}
            className={`state-${step.state}`}
            data-bed-arm-trace-step={step.id}
            title={step.detail}
            aria-label={`${step.label}: ${step.stateLabel}. ${step.detail}`}
          >
            <span className="bed-arm-trace-marker" aria-hidden="true" />
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

function parseVlmToolLabel(
  vlmResult: VLMResult,
  displayToolName: (toolId: string) => string,
  noneLabel: string,
  language: Language,
): string {
  return formatRankedToolLabel(
    rankedVlmToolRows(vlmResult),
    displayToolName,
    noneLabel,
    language,
  );
}

function parseVlmMayoLabel(
  vlmResult: VLMResult,
  displayToolName: (toolId: string) => string,
  noneLabel: string,
  language: Language,
  eligibleToolIds: ReadonlySet<string>,
): string {
  if (!vlmResult.raw_json) return noneLabel;
  try {
    const parsed = parseBoundedJson(vlmResult.raw_json);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return noneLabel;
    const payload = parsed as { mayo?: unknown };
    if (!Array.isArray(payload.mayo)) return noneLabel;
    const rows = payload.mayo.flatMap((row) => {
      if (!Array.isArray(row) || row.length < 3) return [];
      const toolId = String(row[0] ?? "");
      const decision = String(row[1] ?? "").toLowerCase();
      const confidence = Number(row[2]);
      if (!toolId || !eligibleToolIds.has(toolId) || !Number.isFinite(confidence)) return [];
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
  inputSourceStatuses,
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
  inputSourceStatuses: Record<string, InputSourceStatus>;
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
  const [nowMs, setNowMs] = useState(() => Date.now());
  const prefersReducedMotion = useReducedMotion();
  const hasVlmImage = Boolean(vlmImage);

  useEffect(() => {
    if (!hasVlmImage) return;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [hasVlmImage]);

  const sourceHealthRows = useMemo(
    () =>
      [
        ["flir", "FLIR"],
        ["cam4", "CAM4"],
        ["vlm", "VLM"],
        ["speech", language === "ko" ? "음성" : "Speech"],
      ].map(([sourceId, label]) => {
        const status = inputSourceStatuses[sourceId];
        const state = String(status?.state || "MISSING").toUpperCase();
        const localizedState =
          language === "ko"
            ? ({
                READY: "정상",
                STALE: "지연",
                MISSING: "없음",
                RECOVERING: "복구 중",
                ERROR: "오류",
                DISABLED: "꺼짐",
              }[state] ?? state)
            : state;
        const age = Number(status?.age_sec ?? -1);
        const dropped = Number(status?.dropped_count ?? 0);
        return {
          sourceId,
          label,
          state,
          localizedState,
          detail: [
            age >= 0 ? `${age.toFixed(1)}s` : "-",
            language === "ko" ? `누락 ${dropped}` : `drops ${dropped}`,
          ].join(" · "),
          title: status?.error_code || status?.detail || localizedState,
        };
      }),
    [inputSourceStatuses, language],
  );
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
  const canonicalMayoToolIds = new Set(
    simulationState.instrument_states
      .filter(isCanonicalMayoInstrument)
      .map((instrument) => instrument.instrument_id),
  );
  const rawVlmToolLabel = parseVlmToolLabel(
    vlmResult,
    vm.displayToolName,
    vm.ui.none,
    language,
  );
  const rawVlmMayoLabel = parseVlmMayoLabel(
    vlmResult,
    vm.displayToolName,
    vm.ui.none,
    language,
    canonicalMayoToolIds,
  );
  const handHandoverSignalActive = worldState.implicit_request_visible;
  const handHandoverSignalLabel = handHandoverSignalActive
    ? language === "ko"
      ? `게이트 통과 · ${worldState.implicit_request_stability_sec.toFixed(1)}초`
      : `Gate passed · ${worldState.implicit_request_stability_sec.toFixed(1)}s`
    : language === "ko"
      ? "신호 대기"
      : "Waiting for signal";
  const finalMayoLabel = simulationState.instrument_states
    .filter(isCanonicalMayoInstrument)
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
  const systemPredictedToolRows = systemRankedToolRows(worldState);
  const systemPredictedToolLabel = systemPredictedToolRows.length
    ? formatRankedToolLabel(
      systemPredictedToolRows,
      vm.displayToolName,
      vm.ui.none,
      language,
    )
    : worldState.predicted_tool
      ? formatRankedToolLabel(
        [{
          rank: 1,
          toolId: worldState.predicted_tool,
          confidence: Math.max(0, Math.min(1, Number(worldState.predicted_tool_confidence) || 0)),
        }],
        vm.displayToolName,
        vm.ui.none,
        language,
      )
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
              <m.article
                key={item.uiId || item.id}
                layout="position"
                data-timeline-index={index}
                data-timeline-ui-id={item.uiId}
                data-timeline-severity={item.severity}
                data-bed-arm-request-id={item.requestId}
                data-bed-arm-id={item.armId}
                className={`timeline-item ${item.tone} severity-${item.severity}`}
                initial={prefersReducedMotion ? false : { opacity: 0, x: -26 }}
                animate={{ opacity: 1, x: 0 }}
                exit={prefersReducedMotion ? undefined : { opacity: 0, x: 22 }}
                transition={
                  prefersReducedMotion
                    ? { duration: 0 }
                    : {
                        opacity: { duration: MOTION_DURATION.normal },
                        x: { duration: MOTION_DURATION.normal, ease: SILK_EASE },
                        layout: { duration: MOTION_DURATION.moderate, ease: SILK_EASE },
                      }
                }
              >
                <span />
                <strong>{item.title}</strong>
                <TimelineMeta value={item.meta} />
              </m.article>
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

        <div
          aria-label={language === "ko" ? "판단 상세 스크롤 영역" : "Decision detail scroll region"}
          className="decision-scroll"
          tabIndex={0}
        >
          <section className="bed-arm-trace-section" aria-labelledby="bed-arm-trace-title">
            <div className="bed-arm-trace-title-row">
              <div>
                <h3 id="bed-arm-trace-title">
                  {language === "ko" ? "리트랙션 로봇암 요청 추적" : "Retraction robot request trace"}
                </h3>
                <p>
                  {language === "ko"
                    ? "같은 요청 ID로 발화부터 리트랙션 암 상태까지 연결합니다."
                    : "Links the utterance to retraction arm status with one request ID."}
                </p>
              </div>
              <span aria-label={language === "ko" ? "최근 요청 수" : "Recent request count"}>
                {vm.bedRobotArmTraces.length}
              </span>
            </div>
            {vm.bedRobotArmTraces.length ? (
              <div className="bed-arm-trace-list" aria-live="polite">
                {vm.bedRobotArmTraces.slice(0, 2).map((trace) => (
                  <BedRobotArmTraceCard key={trace.requestId} trace={trace} language={language} />
                ))}
              </div>
            ) : (
              <p className="bed-arm-trace-empty">
                {language === "ko" ? "아직 관측된 리트랙션 요청이 없습니다." : "No retraction request has been observed yet."}
              </p>
            )}
          </section>

          <AnimatePresence initial={false} mode="wait">
            {tab === "bt" ? (
              <m.div
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
                <DetailCard
                  label={language === "ko" ? "손 전달 신호 · 리듀서" : "Hand handover signal · reducer"}
                  value={handHandoverSignalLabel}
                  tone={handHandoverSignalActive ? "match" : "normal"}
                />
                <DetailCard label={vm.ui.skill} value={skillStatus.action ? vm.displayActionName(skillStatus.action) : vm.ui.none} />
                <DetailCard label={vm.ui.progress} value={`${Math.round((skillStatus.progress || 0) * 100)}%`} />
                <article className="detail-card wide">
                  <span>{vm.ui.rationale}</span>
                  <strong>{btDecision.decision_reason || btDecision.rationale || vm.ui.none}</strong>
                </article>
              </m.div>
            ) : null}

            {tab === "vlm" ? (
              <m.div
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
                <section
                  className="source-health-row"
                  aria-label={language === "ko" ? "입력원 상태" : "Input source health"}
                >
                  {sourceHealthRows.map((source) => (
                    <div
                      className={`source-health-item state-${source.state.toLowerCase()}`}
                      key={source.sourceId}
                      title={source.title}
                    >
                      <span>{source.label}</span>
                      <strong>{source.localizedState}</strong>
                      <small>{source.detail}</small>
                    </div>
                  ))}
                </section>
                <DetailCard
                  label={language === "ko" ? "VLM 모델 시각 문맥" : "VLM model visual context"}
                  value={
                    vlmImage
                      ? vlmInputImageLabel(
                          vlmHealth.image_source,
                          vlmImage.sizeBytes,
                          vlmImage.receivedAt,
                          nowMs,
                          language,
                        )
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
              </m.div>
            ) : null}

            {tab === "raw" ? (
              <m.pre
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
              </m.pre>
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
