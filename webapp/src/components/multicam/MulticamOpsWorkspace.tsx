import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { LayoutGroup } from "framer-motion";
import * as m from "framer-motion/m";
import {
  Activity,
  ArrowLeft,
  Box,
  Camera,
  CheckCircle2,
  CircleAlert,
  Database,
  Eye,
  Gauge,
  LoaderCircle,
  Maximize2,
  Play,
  Radio,
  RefreshCw,
  Save,
  Square,
  Workflow,
  X,
} from "lucide-react";

import {
  MULTICAM_CAMERAS,
  CAPTURE_STATUS_MAX_AGE_MS,
  type CameraFrame,
  type CameraFrames,
  type CaptureStatus,
  type MulticamCameraId,
  type MulticamView,
  type WorldAction,
  type WorldActionResult,
  type WorldAnchorStatus,
  useMulticamOpsBridge,
} from "../../hooks/useMulticamOpsBridge";
import type { DebugReadOnlyRosSession } from "../../hooks/useIntegrationDebugBridge";
import { silk } from "../../motion-system";
import { TfScene } from "./TfScene";
import "./MulticamOpsWorkspace.css";

type Language = "ko" | "en";
type DepthPresentation = "visualized" | "raw";
type MulticamCamera = (typeof MULTICAM_CAMERAS)[number];
type ExpandedCameraFrame = {
  camera: MulticamCamera;
  frame: CameraFrame;
  view: MulticamView;
  depthPresentation: DepthPresentation;
};

function useClock(intervalMs = 1_000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") setNow(Date.now());
    };
    refreshWhenVisible();
    const timer = window.setInterval(refreshWhenVisible, intervalMs);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [intervalMs]);
  return now;
}

function formatAge(at: number | null | undefined, now: number): string {
  if (!at) return "수신 전";
  const seconds = Math.max(0, (now - at) / 1_000);
  return seconds < 1 ? `${Math.round(seconds * 1_000)} ms 전` : `${seconds.toFixed(1)} s 전`;
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0 s";
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.floor(seconds % 60);
  return minutes > 0 ? `${minutes} m ${remainder.toString().padStart(2, "0")} s` : `${remainder} s`;
}

function statusTone(live: boolean, stale = false): "ok" | "warn" | "idle" {
  if (live && !stale) return "ok";
  return stale ? "warn" : "idle";
}

function cameraDisplayName(id: string): string {
  return MULTICAM_CAMERAS.find((camera) => camera.id === id)?.label || id;
}

function isRecent(frame: CameraFrame | null, now: number): boolean {
  return Boolean(frame && now - frame.receivedAt < 3_000);
}

function CameraGrid({
  view,
  depthPresentation,
  frames,
  captureStatus,
  now,
}: {
  view: MulticamView;
  depthPresentation: DepthPresentation;
  frames: CameraFrames;
  captureStatus: CaptureStatus | null;
  now: number;
}) {
  const cameras = view === "depth" ? MULTICAM_CAMERAS.filter((camera) => camera.depthTopic) : MULTICAM_CAMERAS;
  const [expanded, setExpanded] = useState<ExpandedCameraFrame | null>(null);
  const expandTriggerRef = useRef<HTMLButtonElement | null>(null);

  const closeExpanded = useCallback(() => {
    setExpanded(null);
    window.requestAnimationFrame(() => expandTriggerRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!expanded) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeExpanded();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [closeExpanded, expanded]);

  return (
    <>
      <div
        aria-label={view === "color" ? "동기화 컬러 카메라" : "동기화 깊이 카메라"}
        aria-labelledby={`multicam-tab-${view}`}
        className="ops-camera-grid"
        id="multicam-camera-panel"
        role="tabpanel"
      >
        {cameras.map((camera) => {
          const frame = frames[camera.id];
          const streamLive = isRecent(frame, now);
          const nodeOnline = Boolean(captureStatus?.online_cameras.includes(camera.id));
          const online = nodeOnline && streamLive;
          const viewLabel = view === "color" ? "컬러" : "깊이";
          return (
            <article className={`ops-camera-card ${online ? "is-live" : "is-stale"}`} key={`${view}-${camera.id}`} data-camera-id={camera.id}>
              <header>
                <div><strong>{camera.label}</strong><span>{view === "color" ? "SYNCED COLOR" : "SYNCED DEPTH"}</span></div>
                <span className={`ops-status-dot ${statusTone(online, Boolean(frame) && !streamLive)}`} title={online ? "capture_status와 브라우저 프리뷰 모두 최신" : "드라이버 또는 동기화 스트림을 확인하세요."}>{online ? "LIVE" : streamLive ? "CHECK" : "WAIT"}</span>
              </header>
              <div className="ops-camera-media">
                {frame ? (
                  <button
                    aria-label={`${camera.label} ${viewLabel} 프레임 확대`}
                    className="ops-camera-expand"
                    onClick={(event) => {
                      expandTriggerRef.current = event.currentTarget;
                      setExpanded({ camera, frame, view, depthPresentation });
                    }}
                    type="button"
                  >
                    {view === "depth" ? <DepthPreview cameraLabel={camera.label} frame={frame} presentation={depthPresentation} /> : <img src={frame.src} alt={`${camera.label} 동기화 컬러 프리뷰`} />}
                    <span aria-hidden="true" className="ops-camera-expand-hint"><Maximize2 size={16} /><span>확대</span></span>
                  </button>
                ) : <div className="ops-camera-empty"><Camera size={28} /><span>프레임 대기</span></div>}
              </div>
              <footer>
                <span>{frame ? `${frame.previewHz.toFixed(1)} Hz preview` : "수신 전"}</span>
                <span>{formatAge(frame?.receivedAt, now)}</span>
                <code title={view === "color" ? camera.colorTopic : camera.depthTopic || ""}>{frame?.frameId || "frame_id 대기"}</code>
              </footer>
            </article>
          );
        })}
      </div>
      {expanded ? <CameraInspectDialog expanded={expanded} onClose={closeExpanded} /> : null}
    </>
  );
}

function CameraInspectDialog({ expanded, onClose }: { expanded: ExpandedCameraFrame; onClose: () => void }) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const snapshotFrame = useRetainedCameraFrame(expanded.frame);

  useEffect(() => {
    const focusFrame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    return () => window.cancelAnimationFrame(focusFrame);
  }, []);

  return (
    <div className="ops-camera-inspect-backdrop" onMouseDown={onClose}>
      <section
        aria-describedby="ops-camera-inspect-detail"
        aria-labelledby="ops-camera-inspect-title"
        aria-modal="true"
        className="ops-camera-inspect-dialog"
        onKeyDown={(event) => {
          if (event.key === "Tab") {
            event.preventDefault();
            closeButtonRef.current?.focus();
          }
        }}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header>
          <div>
            <p>{expanded.view === "color" ? "SYNCED COLOR SNAPSHOT" : "SYNCED DEPTH SNAPSHOT"}</p>
            <h3 id="ops-camera-inspect-title">{expanded.camera.label} 프레임 확대</h3>
            <span id="ops-camera-inspect-detail">선택 시점의 {expanded.frame.previewHz.toFixed(1)} Hz 프리뷰 · {expanded.frame.frameId || "frame_id 대기"}</span>
          </div>
          <button aria-label="확대 화면 닫기" className="ops-camera-inspect-close" onClick={onClose} ref={closeButtonRef} type="button"><X size={19} /><span>닫기</span></button>
        </header>
        <div className="ops-camera-inspect-media">
          {expanded.view === "depth"
            ? <DepthPreview cameraLabel={expanded.camera.label} frame={snapshotFrame} maxRenderWidth={960} presentation={expanded.depthPresentation} />
            : <img src={snapshotFrame.src} alt={`${expanded.camera.label} 동기화 컬러 확대 프리뷰`} />}
        </div>
        <footer><code title={expanded.view === "color" ? expanded.camera.colorTopic : expanded.camera.depthTopic || ""}>{expanded.view === "color" ? expanded.camera.colorTopic : expanded.camera.depthTopic}</code><span>Esc 또는 닫기 버튼으로 돌아가기</span></footer>
      </section>
    </div>
  );
}

/**
 * Preview cards deliberately revoke their old Blob URL shortly after a newer
 * ROS message arrives. An inspection dialog outlives that cadence, so retain
 * one independent Blob only while the operator is looking at its snapshot.
 * This preserves the selected frame without adding another image subscription.
 */
function useRetainedCameraFrame(frame: CameraFrame): CameraFrame {
  const [snapshotSrc, setSnapshotSrc] = useState(frame.src);

  useEffect(() => {
    let cancelled = false;
    let retainedUrl: string | null = null;
    setSnapshotSrc(frame.src);

    if (!frame.objectUrl) return undefined;
    void fetch(frame.src)
      .then((response) => {
        if (!response.ok) throw new Error("Unable to retain the selected camera frame.");
        return response.blob();
      })
      .then((blob) => {
        retainedUrl = URL.createObjectURL(blob);
        if (cancelled) {
          URL.revokeObjectURL(retainedUrl);
          return;
        }
        setSnapshotSrc(retainedUrl);
      })
      // The visible image has already claimed the source URL. Retaining is a
      // best-effort safeguard, so leave it visible even if a browser declines
      // to clone the local Blob URL.
      .catch(() => undefined);

    return () => {
      cancelled = true;
      if (retainedUrl) URL.revokeObjectURL(retainedUrl);
    };
  }, [frame.objectUrl, frame.src]);

  return snapshotSrc === frame.src ? frame : { ...frame, src: snapshotSrc, objectUrl: true };
}

function DepthPreview({ cameraLabel, frame, presentation, maxRenderWidth = 480 }: { cameraLabel: string; frame: CameraFrame; presentation: DepthPresentation; maxRenderWidth?: number }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    if (presentation !== "visualized") return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    let cancelled = false;
    const image = new Image();
    image.onload = () => {
      if (cancelled || !image.naturalWidth || !image.naturalHeight) return;
      const scale = Math.min(1, maxRenderWidth / image.naturalWidth);
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) return;
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
      const histogram = new Uint32Array(256);
      for (let index = 0; index < pixels.data.length; index += 4) histogram[pixels.data[index]] += 1;
      const validPixels = canvas.width * canvas.height - histogram[0];
      if (!validPixels) return;
      const percentile = (fraction: number) => {
        const target = validPixels * fraction;
        let count = 0;
        for (let value = 1; value < histogram.length; value += 1) {
          count += histogram[value];
          if (count >= target) return value;
        }
        return 255;
      };
      const low = percentile(0.03);
      const high = Math.max(low + 1, percentile(0.98));
      for (let index = 0; index < pixels.data.length; index += 4) {
        const highByte = pixels.data[index];
        if (!highByte) continue;
        const intensity = Math.round(Math.max(0, Math.min(1, (highByte - low) / (high - low))) * 255);
        pixels.data[index] = intensity;
        pixels.data[index + 1] = intensity;
        pixels.data[index + 2] = intensity;
      }
      context.putImageData(pixels, 0, 0);
    };
    image.src = frame.src;
    return () => { cancelled = true; };
  }, [frame.src, maxRenderWidth, presentation]);

  if (presentation === "raw") return <img src={frame.src} alt={`${cameraLabel} 동기화 깊이 원본 PNG 프리뷰`} />;
  return <canvas ref={canvasRef} aria-label={`${cameraLabel} 동기화 깊이 가시화 프리뷰`} role="img" />;
}

function CaptureStatusPanel({ language, status, colorFrames, now }: { language: Language; status: CaptureStatus | null; colorFrames: CameraFrames; now: number }) {
  const fresh = Boolean(status && now - status.receivedAt <= CAPTURE_STATUS_MAX_AGE_MS);
  const lastStatusPrefix = language === "ko" ? "마지막 상태" : "Last status";
  const freshWaitLabel = language === "ko" ? "새 상태 대기" : "waiting for fresh status";
  const statusDetail = (detail: string): string => {
    if (!status) return language === "ko" ? "수신 대기" : "Waiting for status";
    return fresh ? detail : `${lastStatusPrefix} · ${formatAge(status.receivedAt, now)} · ${freshWaitLabel}`;
  };
  const statusToneFor = (tone: "ok" | "warn" | "idle"): "ok" | "warn" | "idle" => {
    if (!status) return "idle";
    return fresh ? tone : "warn";
  };
  const columnLabels = language === "ko"
    ? { camera: "카메라", config: "설정 ID", driver: "드라이버/USB 수신", preview: "프리뷰", alignment: "정합 상태" }
    : { camera: "Camera", config: "Config ID", driver: "Driver / USB", preview: "Preview", alignment: "Alignment" };
  return (
    <section className="ops-card ops-capture-card" aria-labelledby="ops-capture-heading">
      <header className="ops-card-heading">
        <div>
          <p>CAPTURE STATUS</p>
          <h2 id="ops-capture-heading">카메라 · 동기화 · USB 인벤토리</h2>
          <span>{status ? `${formatAge(status.receivedAt, now)} · /multicam_node/capture_status` : "상태 토픽 대기"}</span>
        </div>
        <span className={`ops-status-dot ${statusTone(Boolean(status?.all_cameras_online), Boolean(status) && !fresh)}`}>{status?.all_cameras_online && fresh ? "5/5 ONLINE" : "CHECK"}</span>
      </header>
      <div className="ops-kpi-grid">
        <Metric icon={Camera} label="온라인 카메라" value={`${status?.online_cameras.length || 0}/5`} detail={statusDetail(status?.offline_cameras.length ? `오프라인: ${status.offline_cameras.join(", ")}` : "동기화 입력 정상")} tone={statusToneFor(status?.all_cameras_online ? "ok" : "warn")} />
        <Metric icon={Gauge} label="최근 동기화" value={status ? status.synced_frames.toLocaleString() : "—"} detail={statusDetail(status ? `최대 skew ${status.max_sync_skew_ms.toFixed(1)} ms` : "수신 대기")} tone={statusToneFor(status && status.max_sync_skew_ms < 50 ? "ok" : "warn")} />
        <Metric icon={Activity} label="캡처 세션" value={status?.recording ? "REC" : "IDLE"} detail={statusDetail(status?.recording ? `${status.session_name || "이름 없음"} · ${formatDuration(status.elapsed_sec)}` : "현재 녹화 중이 아님")} tone={statusToneFor(status?.recording ? "ok" : "idle")} />
        <Metric icon={Workflow} label="보정 준비" value={status?.ready_for_calibration ? "READY" : "HOLD"} detail={statusDetail(status?.hint || "상태 대기")} tone={statusToneFor(status?.ready_for_calibration ? "ok" : "idle")} />
      </div>
      <div className="ops-inventory-note"><CircleAlert size={16} /><span><strong>USB 판정 기준:</strong> launch inventory의 카메라 ID/serial과 드라이버가 실제로 내보낸 `capture_status`·동기화 프레임을 함께 확인합니다. 케이블 링크 속도·포트 재열거 같은 물리 USB 상세는 원격 `cam_watch`/`preflight.sh`의 별도 검사 항목입니다.</span></div>
      <div className="ops-inventory-table-wrap">
        <table className="ops-table">
          <thead><tr><th>{columnLabels.camera}</th><th>{columnLabels.config}</th><th>{columnLabels.driver}</th><th>{columnLabels.preview}</th><th>{columnLabels.alignment}</th></tr></thead>
          <tbody>{MULTICAM_CAMERAS.map((camera) => {
            const frame = colorFrames[camera.id];
            const coverage = status?.cameras.find((item) => item.camera_name === camera.id);
            const driverOnline = Boolean(status?.online_cameras.includes(camera.id));
            const frameFresh = Boolean(frame && now - frame.receivedAt < 3_000);
            const driverLabel = !status
              ? "not reported"
              : fresh
                ? driverOnline ? "driver online" : "not reported"
                : `${lastStatusPrefix} · ${driverOnline ? "driver online" : "not reported"}`;
            const previewLabel = frame
              ? `${frame.previewHz.toFixed(1)} Hz · ${formatAge(frame.receivedAt, now)}`
              : "수신 전";
            const coverageLabel = coverage
              ? `${coverage.detect_rate_hz.toFixed(1)} Hz tag · ${Math.round(coverage.area_coverage * 100)}%`
              : "coverage 대기";
            return <tr key={camera.id}>
              <th data-label={columnLabels.camera} scope="row">{camera.label}</th>
              <td data-label={columnLabels.config}><code>{camera.serial}</code></td>
              <td data-label={columnLabels.driver}><span className={`ops-inline-status ${fresh && driverOnline ? "ok" : "warn"}`}>{driverLabel}</span></td>
              <td data-label={columnLabels.preview}><span className={`ops-inline-status ${frameFresh ? "ok" : "warn"}`}>{previewLabel}</span></td>
              <td data-label={columnLabels.alignment}><span className={`ops-inline-status ${fresh && coverage ? "ok" : "warn"}`}>{fresh ? coverageLabel : coverage ? `${lastStatusPrefix} · ${coverageLabel}` : coverageLabel}</span></td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      {status?.capture_dir ? <p className="ops-path"><span>저장 위치</span><code aria-label="캡처 저장 위치" tabIndex={0}>{status.capture_dir}</code></p> : null}
    </section>
  );
}

function Metric({ icon: Icon, label, value, detail, tone }: { icon: typeof Camera; label: string; value: string; detail: string; tone: "ok" | "warn" | "idle" }) {
  return <article className={`ops-metric tone-${tone}`}><Icon size={17} aria-hidden="true" /><div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div></article>;
}

function WorldAnchorPanel({
  status,
  now,
  pending,
  result,
}: {
  status: WorldAnchorStatus | null;
  now: number;
  pending: WorldAction | null;
  result: WorldActionResult | null;
}) {
  const stale = Boolean(status && now - status.receivedAt > 3_000);
  return (
    <section className="ops-card ops-world-card" aria-labelledby="ops-world-heading">
      <header className="ops-card-heading">
        <div>
          <p>WORLD CONSOLE</p>
          <h2 id="ops-world-heading">World Anchor</h2>
          <span>{status ? `${formatAge(status.receivedAt, now)} · ${status.reference_frame || "reference 대기"} → ${status.world_frame || "world 대기"}` : "world_anchor_node status 대기"}</span>
        </div>
        <span className={`ops-status-dot ${statusTone(Boolean(status?.collecting), stale)}`}>{stale ? "STALE" : status?.collecting ? "COLLECTING" : "IDLE"}</span>
      </header>
      <p className={`ops-world-message ${stale ? "is-stale" : ""}`}>{status ? stale ? `마지막 상태 · ${status.message || "상태 메시지 없음"} · 새 상태 대기` : status.message || "상태 메시지 없음" : "world_anchor_node의 상태 메시지를 기다리는 중입니다."}</p>
      <div className={`ops-world-tags ${stale ? "is-stale" : ""}`}>
        {Object.entries(status?.tags || {}).map(([id, tag]) => <article key={id}><span>TAG {id} · {tag.role || "role 미지정"}</span><strong>{tag.total ?? 0}<small> samples</small></strong><p>{Object.entries(tag.per_camera || {}).map(([camera, value]) => `${camera}: ${value.count ?? 0}${value.fresh ? "" : " (stale)"}`).join(" · ") || "카메라 샘플 대기"}</p></article>)}
        {!status?.tags || !Object.keys(status.tags).length ? <p className="ops-empty-inline">태그 샘플 상태 대기</p> : null}
      </div>
      <div className="ops-world-actions">
        <button className="ops-button secondary" disabled type="button"><Play size={16} />샘플 수집 시작</button>
        <button className="ops-button secondary" disabled type="button"><Square size={16} />수집 중지</button>
        <label className="ops-confirmation"><input disabled type="checkbox" /><span>Observer 화면에서는 World Anchor 변경을 승인하거나 실행할 수 없습니다.</span></label>
        <button className="ops-button danger" disabled type="button">{pending === "solve" ? <LoaderCircle className="ops-spin" size={16} /> : <Save size={16} />}Solve · 저장 · TF 발행</button>
        <button className="ops-button secondary" disabled type="button">{pending === "publish" ? <LoaderCircle className="ops-spin" size={16} /> : <RefreshCw size={16} />}저장된 Anchor 다시 발행</button>
      </div>
      <p className="ops-control-boundary"><CircleAlert size={15} />전용 observer는 read-only입니다. 이 화면은 World Anchor Trigger 서비스를 호출하지 않으며 실행 중인 런타임을 변경하지 않습니다.</p>
      {result ? <p className={`ops-action-result ${result.success ? "ok" : "error"}`} role="status">{result.success ? <CheckCircle2 size={16} /> : <CircleAlert size={16} />}<span><strong>{result.action}</strong> · {result.message}</span></p> : null}
    </section>
  );
}

function TopicInspector({
  topics,
  selectedTopic,
  selectedType,
  sample,
  topicError,
  onSelect,
  onRefresh,
  now,
}: {
  topics: Array<{ name: string; type: string }>;
  selectedTopic: string;
  selectedType: string;
  sample: { receivedAt: number; hz: number; count: number; preview: string } | null;
  topicError: string;
  onSelect: (topic: string) => void;
  onRefresh: () => void;
  now: number;
}) {
  const [filter, setFilter] = useState("");
  const visible = useMemo(() => topics.filter((topic) => topic.name.toLowerCase().includes(filter.toLowerCase()) || topic.type.toLowerCase().includes(filter.toLowerCase())), [filter, topics]);
  return (
    <section className="ops-card ops-topic-card" aria-labelledby="ops-topic-heading">
      <header className="ops-card-heading">
        <div><p>ROS GRAPH</p><h2 id="ops-topic-heading">토픽 상태 · 내용 검사</h2><span>{topics.length ? `${topics.length}개 토픽 발견 · 선택한 토픽만 bounded sample` : "rosapi topic 목록 대기"}</span></div>
        <button className="ops-icon-button" onClick={onRefresh} type="button" title="ROS 토픽 목록 새로고침" aria-label="ROS 토픽 목록 새로고침"><RefreshCw size={16} /></button>
      </header>
      {topicError ? <p className="ops-topic-error"><CircleAlert size={15} />{topicError}</p> : null}
      <div className="ops-topic-controls"><input aria-label="토픽 필터" onChange={(event) => setFilter(event.target.value)} placeholder="토픽 또는 타입 필터" value={filter} /><select aria-label="검사할 ROS 토픽" onChange={(event) => onSelect(event.target.value)} value={selectedTopic}><option value="">토픽 선택</option>{topics.map((topic) => <option key={topic.name} value={topic.name}>{topic.name}</option>)}</select></div>
      <div className="ops-topic-workspace">
        <div className="ops-topic-list" role="list" aria-label="발견된 ROS 토픽">
          {visible.map((topic) => (
            <div key={topic.name} role="listitem">
              <button className={topic.name === selectedTopic ? "selected" : ""} onClick={() => onSelect(topic.name)} type="button"><code>{topic.name}</code><span>{topic.type}</span></button>
            </div>
          ))}
          {!visible.length ? <p>일치하는 토픽이 없습니다.</p> : null}
        </div>
        <div className="ops-topic-preview">
          <div><span>선택</span><strong>{selectedTopic || "토픽 선택"}</strong><small>{selectedType || "type 대기"}</small></div>
          <p className={`ops-inline-status ${sample && now - sample.receivedAt < 3_000 ? "ok" : "warn"}`}>{sample ? `${sample.hz.toFixed(1)} Hz · ${formatAge(sample.receivedAt, now)}` : "메시지 수신 대기"}</p>
          <pre>{sample?.preview || "선택한 토픽의 최신 메시지를 안전한 크기로 표시합니다. 이미지/바이너리 payload는 크기만 표시됩니다."}</pre>
        </div>
      </div>
    </section>
  );
}

export function MulticamOpsWorkspace({
  language,
  onExit,
  embedded = false,
  readOnlySession,
}: {
  language: Language;
  onExit?: () => void;
  embedded?: boolean;
  readOnlySession?: DebugReadOnlyRosSession;
}) {
  const mainId = embedded ? "debug-multicam-main" : "multicam-main";
  // A standalone workspace replaces its navigation trigger, so restore
  // keyboard/screen-reader focus to its landmark. Embedded Debug keeps focus
  // on the outer roving tab; moving it into this inner region would break
  // arrow-key tab navigation.
  useEffect(() => {
    if (embedded) return;
    const focusFrame = window.requestAnimationFrame(() => {
      document.getElementById(mainId)?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(focusFrame);
  }, [embedded, mainId]);
  const [view, setView] = useState<MulticamView>("color");
  const [depthPresentation, setDepthPresentation] = useState<DepthPresentation>("visualized");
  // The Debug TF tab owns its own `/tf_static` + `/tf` subscriptions through
  // the Debug bridge. Do not keep a duplicate static-transform subscription in
  // the embedded multicam observer.
  const bridge = useMulticamOpsBridge(view, {
    observeStaticTf: !embedded,
    readOnlySession,
  });
  const now = useClock();
  const handleViewKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, current: MulticamView) => {
    const options: MulticamView[] = ["color", "depth"];
    const currentIndex = options.indexOf(current);
    const nextIndex = event.key === "ArrowRight"
      ? (currentIndex + 1) % options.length
      : event.key === "ArrowLeft"
        ? (currentIndex - 1 + options.length) % options.length
        : event.key === "Home"
          ? 0
          : event.key === "End"
            ? options.length - 1
            : null;
    if (nextIndex === null) return;
    event.preventDefault();
    const next = options[nextIndex];
    setView(next);
    window.requestAnimationFrame(() => document.getElementById(`multicam-tab-${next}`)?.focus());
  };
  const activeFrames = view === "color" ? bridge.colorFrames : bridge.depthFrames;
  const freshFrameCount = Object.values(activeFrames).filter(
    (frame) => frame && now - frame.receivedAt <= 3_000,
  ).length;

  const observerConnection = (
    <div className="ops-connection">
      <div className="ops-observer-signals" aria-label="멀티캠 observer 상태">
        <div className={`ops-connection-state ${bridge.socketConnected ? "ok" : "warn"}`}><Radio size={16} /><span>Transport {bridge.socketConnected ? "연결" : "대기"}</span></div>
        <div className={`ops-connection-state ${bridge.captureTopicDiscovered ? "ok" : "warn"}`}><Activity size={16} /><span>Graph topic {bridge.captureTopicDiscovered ? "발견" : "미발견"}</span></div>
        <div className={`ops-connection-state ${bridge.captureStatusFresh ? "ok" : "warn"}`}><Gauge size={16} /><span>CaptureStatus {bridge.captureStatusFresh ? "fresh" : bridge.captureStatus ? "stale" : "대기"}</span></div>
      </div>
      <div className="ops-observer-endpoint"><code title={bridge.url}>{bridge.url}</code><button className="ops-icon-button" onClick={bridge.retry} type="button" title="멀티캠 observer 재연결" aria-label="멀티캠 observer 재연결"><RefreshCw size={16} /></button></div>
      <small aria-live="polite">{bridge.connectionMessage} · {view} frame fresh {freshFrameCount}/{view === "color" ? 5 : 4}</small>
    </div>
  );

  const readinessBoundary = !bridge.connected ? <p aria-live="polite" className="ops-disconnected-banner" data-slot="multicam-readiness-boundary" role="status"><CircleAlert aria-hidden="true" size={16} />전용 멀티캠 observer가 ready 상태가 아닙니다. 실행 중인 모드는 유지되며, fresh CaptureStatus 확인 전에는 관측 내용을 신뢰하지 않습니다.</p> : null;

  const consoleBody = (
    <div className={`ops-layout${embedded ? " ops-embedded-layout" : ""}`}>
      <section className="ops-card ops-preview-card" aria-labelledby="ops-preview-heading">
        <header className="ops-card-heading">
          <div><p>SYNCED PREVIEW</p><h2 id="ops-preview-heading">주요 동기화 뷰</h2><span>{view === "color" ? "5개 /synced color stream · 토픽 수신 프레임을 원본 그대로 표시" : depthPresentation === "visualized" ? "D455 4대의 /synced compressedDepth stream · 원본 거리값을 화면 대비로만 가시화" : "D455 4대의 /synced compressedDepth stream · 토픽 원본 PNG 표시 · FLIR은 depth 센서가 없습니다."}</span></div>
          <div className="ops-preview-actions">
            <LayoutGroup id={`multicam-view-tabs-${embedded ? "debug" : "workspace"}`}>
              <div className="ops-segmented" role="tablist" aria-label="영상 유형">
                {(["color", "depth"] as const).map((option) => {
                  const active = view === option;
                  const Icon = option === "color" ? Eye : Box;
                  return (
                    <button
                      aria-controls="multicam-camera-panel"
                      aria-selected={active}
                      className={active ? "active" : ""}
                      id={`multicam-tab-${option}`}
                      key={option}
                      onClick={() => setView(option)}
                      onKeyDown={(event) => handleViewKeyDown(event, option)}
                      role="tab"
                      tabIndex={active ? 0 : -1}
                      type="button"
                    >
                      {active ? (
                        <m.span
                          aria-hidden="true"
                          className="ops-segment-focus"
                          layoutId={`multicam-active-view-${embedded ? "debug" : "workspace"}`}
                          transition={silk.layout.transition}
                        />
                      ) : null}
                      <span className="ops-segment-label"><Icon size={15} />{option === "color" ? "Color" : "Depth"}</span>
                    </button>
                  );
                })}
              </div>
            </LayoutGroup>
            {view === "depth" ? (
              <LayoutGroup id={`multicam-depth-presentation-${embedded ? "debug" : "workspace"}`}>
                <div className="ops-segmented ops-depth-presentation" aria-label="Depth 표시 방식" role="group">
                  {(["visualized", "raw"] as const).map((option) => {
                    const active = depthPresentation === option;
                    return (
                      <button
                        aria-pressed={active}
                        className={active ? "active" : ""}
                        key={option}
                        onClick={() => setDepthPresentation(option)}
                        type="button"
                      >
                        {active ? (
                          <m.span
                            aria-hidden="true"
                            className="ops-segment-focus"
                            layoutId={`multicam-active-depth-presentation-${embedded ? "debug" : "workspace"}`}
                            transition={silk.layout.transition}
                          />
                        ) : null}
                        <span className="ops-segment-label">{option === "visualized" ? "가시화" : "원본 PNG"}</span>
                      </button>
                    );
                  })}
                </div>
              </LayoutGroup>
            ) : null}
          </div>
        </header>
        <CameraGrid captureStatus={bridge.captureStatus} depthPresentation={depthPresentation} frames={activeFrames} now={now} view={view} />
      </section>
      {!embedded ? <TfScene transforms={bridge.tfTransforms} /> : null}
      <WorldAnchorPanel now={now} pending={bridge.worldActionPending} result={bridge.worldActionResult} status={bridge.worldStatus} />
      <CaptureStatusPanel colorFrames={bridge.colorFrames} language={language} now={now} status={bridge.captureStatus} />
      <TopicInspector now={now} onRefresh={bridge.refreshTopics} onSelect={bridge.setSelectedTopic} sample={bridge.selectedTopicSample} selectedTopic={bridge.selectedTopic} selectedType={bridge.selectedTopicType} topicError={bridge.topicError} topics={bridge.topics} />
    </div>
  );

  if (embedded) {
    return (
      <section className="ops-embedded-workspace" data-slot="debug-multicam-ops" id={mainId} tabIndex={-1} aria-labelledby="debug-multicam-heading">
        <header className="ops-card ops-embedded-header">
          <div className="ops-card-heading">
            <div><p>DEBUG · MULTICAM OBSERVER</p><h2 id="debug-multicam-heading">멀티캠 관제</h2><span>동기화 영상, Capture 상태, World Anchor를 read-only observer로 확인합니다. TF·3D 모델은 개별 기능의 TF 탭에서 확인합니다.</span></div>
          </div>
          {observerConnection}
        </header>
        {readinessBoundary}
        {consoleBody}
      </section>
    );
  }

  return (
    <div className="app-shell ops-app-shell" data-slot="multicam-ops-workspace">
      <a className="skip-link" href={`#${mainId}`}>
        {language === "ko" ? "멀티캠 본문으로 이동" : "Skip to multicamera content"}
      </a>
      <header className="ops-header">
        <div className="ops-brand"><button className="ops-back-button" onClick={() => onExit?.()} type="button"><ArrowLeft size={18} />{language === "ko" ? "미션 화면" : "Mission"}</button><div><p>ARPA MULTICAM · ROS 2 OPERATIONS</p><h1>멀티캠 관제 콘솔</h1><span>동기화 영상, 고정 TF, Capture 상태 및 World Anchor를 하나의 ROSBridge 세션에서 확인합니다.</span></div></div>
        {observerConnection}
      </header>

      {readinessBoundary}
      <main id={mainId} tabIndex={-1}>{consoleBody}</main>
    </div>
  );
}

export default MulticamOpsWorkspace;
