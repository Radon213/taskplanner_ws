import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { BrainCircuit, Code2, ListTree, RadioTower } from "lucide-react";

import type { useDigitalTwinViewModel } from "../../hooks/useDigitalTwinViewModel";
import type { BTDecision, SkillStatus, VLMHealth, VLMReducerDecision, VLMResult } from "../../types";
import { type Language } from "../../utils/display";

type ViewModel = ReturnType<typeof useDigitalTwinViewModel>;
type TabId = "bt" | "vlm" | "raw";
type TimelineFilter = "all" | "normal" | "warning" | "error";

function timelineItemSortParts(uiId: string, fallback: number): { time: number; sequence: number; fallback: number } {
  const [timestamp, sequence] = uiId.split("-");
  const timeValue = Number(timestamp);
  const sequenceValue = Number(sequence);
  return {
    time: Number.isFinite(timeValue) ? timeValue : -1,
    sequence: Number.isFinite(sequenceValue) ? sequenceValue : 0,
    fallback,
  };
}

function compareTimelineItems(a: { uiId: string }, b: { uiId: string }, aFallback: number, bFallback: number): number {
  const left = timelineItemSortParts(a.uiId, aFallback);
  const right = timelineItemSortParts(b.uiId, bFallback);
  if (left.time !== right.time) return right.time - left.time;
  if (left.sequence !== right.sequence) return right.sequence - left.sequence;
  return right.fallback - left.fallback;
}

function DetailCard({ label, value }: { label: string; value: string | number }) {
  return (
    <article className="detail-card">
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

export function ObservabilityPanel({
  vm,
  language,
  btDecision,
  skillStatus,
  vlmHealth,
  vlmResult,
  vlmReducerDecisions,
}: {
  vm: ViewModel;
  language: Language;
  btDecision: BTDecision;
  skillStatus: SkillStatus;
  vlmHealth: VLMHealth;
  vlmResult: VLMResult;
  vlmReducerDecisions: VLMReducerDecision[];
}) {
  const [tab, setTab] = useState<TabId>("bt");
  const [timelineFilter, setTimelineFilter] = useState<TimelineFilter>("all");
  const timelineStripRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);
  const [followLatest, setFollowLatest] = useState(true);
  const newestFirstTimeline = useMemo(
    () =>
      vm.timeline
        .map((item, index) => ({ item, index }))
        .sort((a, b) =>
          compareTimelineItems(a.item, b.item, vm.timeline.length - a.index, vm.timeline.length - b.index),
        )
        .map(({ item }) => item),
    [vm.timeline],
  );
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

  return (
    <section className="observability-panel">
      <div className="timeline-panel">
        <div className="panel-title-row">
          <div>
            <p className="section-kicker">{vm.ui.timeline}</p>
            <h2>{vm.ui.timelineLog}</h2>
          </div>
          <div className="timeline-toolbar">
            <div className="timeline-filter" role="tablist" aria-label={vm.ui.timelineFilter}>
              {timelineFilters.map((filter) => (
                <button
                  key={filter.id}
                  type="button"
                  aria-pressed={timelineFilter === filter.id}
                  aria-selected={timelineFilter === filter.id}
                  role="tab"
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
          {visibleTimeline.map((item, index) => (
            <motion.article
              key={item.id}
              layout
              data-timeline-index={index}
              data-timeline-ui-id={item.uiId}
              data-timeline-severity={item.severity}
              className={`timeline-item ${item.tone} severity-${item.severity}`}
              initial={{ opacity: 0, x: -22, scale: 0.98 }}
              animate={{ opacity: 1, x: 0, scale: 1 }}
              transition={{
                opacity: { duration: 0.16 },
                scale: { duration: 0.16 },
                x: { duration: 0.2 },
                layout: { duration: 0.26, ease: [0.22, 1, 0.36, 1] },
              }}
            >
              <span />
              <strong>{item.title}</strong>
              <TimelineMeta value={item.meta} />
            </motion.article>
          ))}
          {visibleTimeline.length === 0 ? (
            <div className="timeline-empty">
              {language === "ko" ? "이 필터에 표시할 이벤트가 없습니다." : "No events match this filter."}
            </div>
          ) : null}
        </div>
      </div>

      <div className="explain-panel">
        <div className="panel-title-row compact">
          <div>
            <p className="section-kicker">{vm.ui.observability}</p>
            <h2>{tab === "bt" ? vm.ui.bt : tab === "vlm" ? vm.ui.vlm : vm.ui.rawResult}</h2>
          </div>
          <div className="tab-switch" role="tablist" aria-label={vm.ui.observability}>
            <button className={tab === "bt" ? "active" : ""} onClick={() => setTab("bt")} type="button">
              <ListTree size={15} />
              {vm.ui.bt}
            </button>
            <button className={tab === "vlm" ? "active" : ""} onClick={() => setTab("vlm")} type="button">
              <BrainCircuit size={15} />
              {vm.ui.vlm}
            </button>
            <button className={tab === "raw" ? "active" : ""} onClick={() => setTab("raw")} type="button">
              <Code2 size={15} />
              Raw
            </button>
          </div>
        </div>

        <AnimatePresence mode="wait">
          {tab === "bt" ? (
            <motion.div
              key="bt"
              className="detail-grid"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.18 }}
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
              key="vlm"
              className="detail-grid"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.18 }}
            >
              <DetailCard label={vm.ui.connection} value={vm.vlmStatus.connection} />
              <DetailCard label={vm.ui.health} value={vm.vlmStatus.health} />
              <DetailCard label={vm.ui.model} value={vlmHealth.model_id || vm.ui.none} />
              <DetailCard label={vm.ui.mode} value={vlmHealth.last_mode || vm.ui.none} />
              <DetailCard label={vm.ui.source} value={vlmResult.source || vm.ui.none} />
              <DetailCard label={vm.ui.imageSource} value={vlmHealth.image_source || vm.ui.none} />
              <DetailCard label={vm.ui.latency} value={vlmHealth.latency_sec ? `${vlmHealth.latency_sec.toFixed(3)}s` : vm.ui.none} />
              <DetailCard label={vm.ui.currentPhase} value={vlmResult.phase_ids[0] ? `${vm.metrics.phase} (${Math.round((vlmResult.phase_confidences[0] || 0) * 100)}%)` : vm.ui.none} />
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
              key="raw"
              className="raw-block"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.18 }}
            >
              {vlmResult.raw_json || "{}"}
            </motion.pre>
          ) : null}
        </AnimatePresence>
      </div>
    </section>
  );
}
