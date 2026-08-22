import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Radio,
  ShieldCheck,
} from "lucide-react";

import type { DebugReadOnlyTopicSubscriber } from "../../hooks/useIntegrationDebugBridge";
import {
  TfScene,
  type TfSceneTransform,
  type TfTransformSource,
} from "../multicam/MulticamOpsWorkspace";
import "./DebugTfPanel.css";

interface DebugTfPanelProps {
  subscribeTopic: DebugReadOnlyTopicSubscriber;
}

type TfTransform = TfSceneTransform & {
  source: TfTransformSource;
  receivedAt: number;
  sourceStamp: string;
};

interface StreamMeta {
  receivedAt: number | null;
  messageCount: number;
  hz: number;
  rejectedTransforms: number;
}

const MAX_TRANSFORMS_PER_STREAM = 512;
const MAX_TF_FRAME_CHARS = 256;
const DYNAMIC_STALE_AFTER_MS = 3_000;
const RATE_WINDOW_MS = 5_000;

const EMPTY_STREAM_META: StreamMeta = {
  receivedAt: null,
  messageCount: 0,
  hz: 0,
  rejectedTransforms: 0,
};

function finite(value: unknown): number | null {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function boundedFrame(value: unknown): string {
  return typeof value === "string" ? value.trim().slice(0, MAX_TF_FRAME_CHARS) : "";
}

function sourceStamp(header: unknown): string {
  if (!header || typeof header !== "object") return "stamp 없음";
  const stamp = (header as { stamp?: unknown }).stamp;
  if (!stamp || typeof stamp !== "object") return "stamp 없음";
  const sec = finite((stamp as { sec?: unknown }).sec);
  const nanosec = finite((stamp as { nanosec?: unknown }).nanosec);
  if (sec === null || nanosec === null || sec < 0 || nanosec < 0) return "stamp 없음";
  return `${Math.floor(sec)}:${Math.floor(nanosec).toString().padStart(9, "0")}`;
}

function parseTransforms(message: unknown, source: TfTransformSource, receivedAt: number): {
  transforms: TfTransform[];
  rejectedTransforms: number;
} {
  if (!message || typeof message !== "object") return { transforms: [], rejectedTransforms: 1 };
  const rawTransforms = (message as { transforms?: unknown }).transforms;
  if (!Array.isArray(rawTransforms)) return { transforms: [], rejectedTransforms: 1 };

  let rejectedTransforms = 0;
  const transforms = rawTransforms.slice(0, MAX_TRANSFORMS_PER_STREAM).flatMap((item): TfTransform[] => {
    if (!item || typeof item !== "object") {
      rejectedTransforms += 1;
      return [];
    }
    const raw = item as {
      header?: unknown;
      child_frame_id?: unknown;
      transform?: {
        translation?: { x?: unknown; y?: unknown; z?: unknown };
        rotation?: { x?: unknown; y?: unknown; z?: unknown; w?: unknown };
      };
    };
    const parentFrame = boundedFrame((raw.header as { frame_id?: unknown } | undefined)?.frame_id);
    const childFrame = boundedFrame(raw.child_frame_id);
    const translation = raw.transform?.translation;
    const rotation = raw.transform?.rotation;
    const x = finite(translation?.x);
    const y = finite(translation?.y);
    const z = finite(translation?.z);
    const qx = finite(rotation?.x);
    const qy = finite(rotation?.y);
    const qz = finite(rotation?.z);
    const qw = finite(rotation?.w);
    const quaternionNorm = qx === null || qy === null || qz === null || qw === null
      ? 0
      : Math.hypot(qx, qy, qz, qw);
    if (!parentFrame || !childFrame || x === null || y === null || z === null || quaternionNorm < 0.001) {
      rejectedTransforms += 1;
      return [];
    }
    return [{
      parentFrame,
      childFrame,
      translation: { x, y, z },
      rotation: { x: qx!, y: qy!, z: qz!, w: qw! },
      source,
      receivedAt,
      sourceStamp: sourceStamp(raw.header),
    }];
  });
  rejectedTransforms += Math.max(0, rawTransforms.length - MAX_TRANSFORMS_PER_STREAM);
  return { transforms, rejectedTransforms };
}

function mergeLatestByChild(current: readonly TfTransform[], additions: readonly TfTransform[]): TfTransform[] {
  const byChild = new Map(current.map((transform) => [transform.childFrame, transform]));
  for (const transform of additions) {
    if (byChild.has(transform.childFrame)) byChild.delete(transform.childFrame);
    while (byChild.size >= MAX_TRANSFORMS_PER_STREAM) {
      const oldest = byChild.keys().next().value;
      if (typeof oldest !== "string") break;
      byChild.delete(oldest);
    }
    byChild.set(transform.childFrame, transform);
  }
  return [...byChild.values()].sort((left, right) => left.childFrame.localeCompare(right.childFrame));
}

function formatAge(receivedAt: number | null, now: number): string {
  if (!receivedAt) return "수신 전";
  const ageMs = Math.max(0, now - receivedAt);
  return ageMs < 1_000 ? `${Math.round(ageMs)} ms 전` : `${(ageMs / 1_000).toFixed(1)} s 전`;
}

function statusForStream(
  source: TfTransformSource,
  meta: StreamMeta,
  now: number,
): { tone: "ready" | "waiting" | "stale"; label: string } {
  if (!meta.receivedAt) return { tone: "waiting", label: "WAIT" };
  if (source === "dynamic" && now - meta.receivedAt > DYNAMIC_STALE_AFTER_MS) {
    return { tone: "stale", label: "STALE" };
  }
  return { tone: "ready", label: "LIVE" };
}

function displayFrame(frame: string): string {
  return frame.length <= 44 ? frame : `${frame.slice(0, 29)}…${frame.slice(-12)}`;
}

function StreamSummary({
  source,
  transforms,
  meta,
  now,
}: {
  source: TfTransformSource;
  transforms: readonly TfTransform[];
  meta: StreamMeta;
  now: number;
}) {
  const status = statusForStream(source, meta, now);
  const topic = source === "static" ? "/tf_static" : "/tf";
  const description = source === "static"
    ? "retained 고정 보정 변환 · 재수신 전에는 값이 바뀌지 않습니다"
    : "시간에 따라 변하는 최신 transform · child frame별 마지막 값만 표시합니다";
  return (
    <article
      className={`debug-tf-stream-card ${source} ${status.tone}`}
      data-slot={`debug-tf-${source}-stream`}
      data-state={status.label}
    >
      <header>
        <div>
          <p>{source === "static" ? "STATIC TRANSFORMS" : "DYNAMIC TRANSFORMS"}</p>
          <h3><code>{topic}</code></h3>
          <span>{description}</span>
        </div>
        <span className={`debug-tf-stream-status ${status.tone}`}>
          {status.tone === "ready" ? <CheckCircle2 size={15} aria-hidden="true" /> : status.tone === "stale" ? <AlertTriangle size={15} aria-hidden="true" /> : <Clock3 size={15} aria-hidden="true" />}
          {status.label}
        </span>
      </header>
      <dl>
        <div><dt>FRAMES</dt><dd>{transforms.length}</dd></div>
        <div><dt>AGE</dt><dd>{formatAge(meta.receivedAt, now)}</dd></div>
        <div><dt>INPUT</dt><dd>{meta.hz.toFixed(meta.hz >= 10 ? 1 : 2)} Hz</dd></div>
        <div><dt>REJECTED</dt><dd>{meta.rejectedTransforms}</dd></div>
      </dl>
    </article>
  );
}

function TransformList({
  source,
  transforms,
  now,
}: {
  source: TfTransformSource;
  transforms: readonly TfTransform[];
  now: number;
}) {
  const topic = source === "static" ? "/tf_static" : "/tf";
  return (
    <section className={`debug-tf-transform-list ${source}`} data-slot={`debug-tf-${source}-list`} aria-labelledby={`debug-tf-${source}-heading`}>
      <header>
        <div>
          <p>{source === "static" ? "FIXED CALIBRATION / ANCHOR" : "LATEST MOVING FRAMES"}</p>
          <h3 id={`debug-tf-${source}-heading`}><code>{topic}</code> {source === "static" ? "고정 프레임" : "동적 프레임"}</h3>
        </div>
        <span>{transforms.length} child frame</span>
      </header>
      {transforms.length ? (
        <ol>
          {transforms.map((transform) => (
            <li key={`${source}:${transform.childFrame}`}>
              <header>
                <code title={`${transform.parentFrame} → ${transform.childFrame}`}>{displayFrame(transform.parentFrame)} <span aria-hidden="true">→</span> {displayFrame(transform.childFrame)}</code>
                <span>{transform.sourceStamp}</span>
              </header>
              <dl>
                <div><dt>XYZ · m</dt><dd>{transform.translation.x.toFixed(3)}, {transform.translation.y.toFixed(3)}, {transform.translation.z.toFixed(3)}</dd></div>
                <div><dt>Q · xyzw</dt><dd>{transform.rotation.x.toFixed(3)}, {transform.rotation.y.toFixed(3)}, {transform.rotation.z.toFixed(3)}, {transform.rotation.w.toFixed(3)}</dd></div>
                <div><dt>RECEIVED</dt><dd>{formatAge(transform.receivedAt, now)}</dd></div>
              </dl>
            </li>
          ))}
        </ol>
      ) : (
        <div className="debug-tf-empty-list" role="status">
          <Radio size={18} aria-hidden="true" />
          <span><code>{topic}</code> 메시지를 기다리는 중입니다.</span>
        </div>
      )}
    </section>
  );
}

/**
 * Read-only TF inspection surface. The only subscriptions are `/tf_static`
 * and `/tf`; it deliberately has no service, action, advertise, or publish
 * path. It unmounts with its Debug tab so high-rate `/tf` is never retained in
 * the browser outside an active inspection session.
 */
export function DebugTfPanel({ subscribeTopic }: DebugTfPanelProps) {
  const [staticTransforms, setStaticTransforms] = useState<TfTransform[]>([]);
  const [dynamicTransforms, setDynamicTransforms] = useState<TfTransform[]>([]);
  const [staticMeta, setStaticMeta] = useState<StreamMeta>(EMPTY_STREAM_META);
  const [dynamicMeta, setDynamicMeta] = useState<StreamMeta>(EMPTY_STREAM_META);
  const [now, setNow] = useState(Date.now());
  const rateSamplesRef = useRef<Record<TfTransformSource, number[]>>({ static: [], dynamic: [] });

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const updateStream = (source: TfTransformSource, message: unknown) => {
      const receivedAt = Date.now();
      const parsed = parseTransforms(message, source, receivedAt);
      const samples = rateSamplesRef.current[source];
      samples.push(receivedAt);
      while (samples.length && receivedAt - samples[0] > RATE_WINDOW_MS) samples.shift();
      const setMeta = source === "static" ? setStaticMeta : setDynamicMeta;
      const setTransforms = source === "static" ? setStaticTransforms : setDynamicTransforms;
      setMeta((current) => ({
        receivedAt,
        messageCount: current.messageCount + 1,
        hz: samples.length / (RATE_WINDOW_MS / 1_000),
        rejectedTransforms: current.rejectedTransforms + parsed.rejectedTransforms,
      }));
      if (parsed.transforms.length) {
        setTransforms((current) => mergeLatestByChild(current, parsed.transforms));
      }
    };

    const unsubscribeStatic = subscribeTopic({
      name: "/tf_static",
      messageType: "tf2_msgs/msg/TFMessage",
      queueLength: 32,
      reliability: "reliable",
      durability: "transient_local",
    }, (message) => updateStream("static", message));
    const unsubscribeDynamic = subscribeTopic({
      name: "/tf",
      messageType: "tf2_msgs/msg/TFMessage",
      // The scene only needs current poses; sampling bounds high-rate bridge
      // traffic while still exposing a meaningful incoming-rate indicator.
      throttleRate: 200,
      queueLength: 10,
      // Standard tf2_ros.TransformBroadcaster offers BEST_EFFORT/VOLATILE.
      // Requesting RELIABLE here would make the Debug reader QoS-incompatible
      // and leave a healthy dynamic stream permanently in WAIT.
      reliability: "best_effort",
      durability: "volatile",
    }, (message) => updateStream("dynamic", message));
    return () => {
      unsubscribeStatic();
      unsubscribeDynamic();
    };
  }, [subscribeTopic]);

  // `/tf` is a live observation, not a retained calibration record. Once its
  // stream goes stale, remove the last children from both the list and scene
  // so an operator cannot mistake a departed tool for a current pose.
  const freshDynamicTransforms = useMemo(
    () => dynamicTransforms.filter((transform) => now - transform.receivedAt <= DYNAMIC_STALE_AFTER_MS),
    [dynamicTransforms, now],
  );
  const sceneTransforms = useMemo(() => {
    const dynamicChildren = new Set(freshDynamicTransforms.map((transform) => transform.childFrame));
    return [
      ...staticTransforms.filter((transform) => !dynamicChildren.has(transform.childFrame)),
      ...freshDynamicTransforms,
    ];
  }, [freshDynamicTransforms, staticTransforms]);
  const dynamicStatus = statusForStream("dynamic", dynamicMeta, now);

  return (
    <section className="debug-panel-stack" data-slot="debug-tf-panel">
      <article className="debug-section-card debug-tf-header-card">
        <div className="debug-section-heading">
          <div>
            <p>READ-ONLY · TF INSPECTOR</p>
            <h2>TF 좌표계 · 3D 모델</h2>
            <span><code>/tf_static</code> 고정 보정값과 <code>/tf</code> 최신 동적 프레임을 분리해 확인합니다. 이 화면은 transform을 발행·수정하거나 robot/world 정합을 주장하지 않습니다.</span>
          </div>
          <span className={`debug-tf-header-state ${dynamicStatus.tone}`}>
            <ShieldCheck size={16} aria-hidden="true" />
            MONITOR ONLY
          </span>
        </div>
        <div className="debug-tf-caution" role="status">
          <AlertTriangle size={16} aria-hidden="true" />
          <span>CAM3·CAM4 tool frame은 parent → child 관계와 카메라 frame을 그대로 표시합니다. rig/world 보정이 없으면 서로 다른 root·orphan frame은 공간적으로 정렬되었다고 해석하지 마세요.</span>
        </div>
      </article>

      <div className="debug-tf-stream-grid">
        <StreamSummary source="static" transforms={staticTransforms} meta={staticMeta} now={now} />
        <StreamSummary source="dynamic" transforms={freshDynamicTransforms} meta={dynamicMeta} now={now} />
      </div>

      <TfScene transforms={sceneTransforms} showTransformTree={false} />

      <div className="debug-tf-list-grid">
        <TransformList source="static" transforms={staticTransforms} now={now} />
        <TransformList source="dynamic" transforms={freshDynamicTransforms} now={now} />
      </div>
    </section>
  );
}
