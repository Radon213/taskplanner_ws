import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  EyeOff,
  Image as ImageIcon,
  Radio,
} from "lucide-react";

import type { DebugReadOnlyTopicSubscriber } from "../../hooks/useIntegrationDebugBridge";
import {
  DEBUG_PERCEPTION_FINAL_OVERLAY_STATUS_TOPIC,
  DEBUG_PERCEPTION_FINAL_OVERLAY_TOPIC,
  DEBUG_PERCEPTION_MAX_AGE_MS,
  type DebugFinalOverlayCameraId,
  type DebugFinalOverlayCameraStatus,
  type DebugFinalOverlayLayerId,
  type DebugFinalOverlayLayerStatus,
  type DebugFinalOverlayState,
  type DebugFinalOverlayStatus,
  type DebugPerceptionFrame,
  parseCompressedFrame,
  parseFinalOverlayStatus,
} from "../../utils/debugPerceptionContract";
import "./DirectPerceptionOverlayPanel.css";

interface DirectPerceptionOverlayPanelProps {
  subscribeTopic: DebugReadOnlyTopicSubscriber;
}

interface FinalOverlayFrameState {
  frame: DebugPerceptionFrame | null;
  contractError: string;
}

interface FinalOverlayStatusState {
  status: DebugFinalOverlayStatus | null;
  contractError: string;
}

type RasterState = "live" | "stale" | "waiting" | "error";

const CAMERA_IDS: readonly DebugFinalOverlayCameraId[] = ["cam3", "cam4"];
const LAYER_IDS: readonly DebugFinalOverlayLayerId[] = ["tool", "pose", "hand", "blood"];
const CAMERA_LABELS: Record<DebugFinalOverlayCameraId, string> = {
  cam3: "CAM3",
  cam4: "CAM4",
};
const LAYER_LABELS: Record<DebugFinalOverlayLayerId, string> = {
  tool: "Tool",
  pose: "Pose",
  hand: "Hand",
  blood: "Blood",
};

function formatAge(receivedAt: number | null, now: number): string {
  if (receivedAt === null) return "수신 전";
  const age = Math.max(0, now - receivedAt);
  return age < 1_000 ? `${Math.round(age)} ms 전` : `${(age / 1_000).toFixed(1)} s 전`;
}

function rasterStateFor(
  receivedAt: number | null,
  contractError: string,
  now: number,
): RasterState {
  if (contractError) return "error";
  if (receivedAt === null) return "waiting";
  return now - receivedAt > DEBUG_PERCEPTION_MAX_AGE_MS ? "stale" : "live";
}

function rasterStateLabel(state: RasterState): string {
  switch (state) {
    case "live": return "LIVE";
    case "stale": return "STALE";
    case "error": return "CONTRACT ERROR";
    default: return "FRAME WAITING";
  }
}

function stateLabel(state: DebugFinalOverlayState): string {
  switch (state) {
    case "live": return "ACTIVE · LIVE";
    case "stale": return "ACTIVE · STALE";
    case "missing": return "ACTIVE · MISSING";
    default: return "DISABLED";
  }
}

function StateIcon({ state }: { state: DebugFinalOverlayState | RasterState }) {
  if (state === "live") return <CheckCircle2 aria-hidden="true" size={14} />;
  if (state === "disabled") return <EyeOff aria-hidden="true" size={14} />;
  if (state === "waiting" || state === "missing") return <Radio aria-hidden="true" size={14} />;
  return <AlertTriangle aria-hidden="true" size={14} />;
}

/**
 * The browser receives only one server-composited image. Decode the successor
 * off-DOM so the previous decoded raster remains painted while it loads.
 */
function useFinalOverlayFrame(
  subscribeTopic: DebugReadOnlyTopicSubscriber,
): FinalOverlayFrameState {
  const [frame, setFrame] = useState<DebugPerceptionFrame | null>(null);
  const [contractError, setContractError] = useState("");

  useEffect(() => {
    let disposed = false;
    let latestSequence = 0;
    const unsubscribe = subscribeTopic({
      name: DEBUG_PERCEPTION_FINAL_OVERLAY_TOPIC,
      messageType: "sensor_msgs/msg/CompressedImage",
      compression: "cbor",
      throttleRate: 180,
      queueLength: 1,
      reliability: "best_effort",
    }, (message) => {
      const next = parseCompressedFrame(
        message,
        DEBUG_PERCEPTION_FINAL_OVERLAY_TOPIC,
        false,
        Date.now(),
      );
      if (disposed) return;
      if (!next) {
        setContractError("final_overlay CompressedImage header, format 또는 frame data가 유효하지 않습니다.");
        return;
      }
      const sequence = ++latestSequence;
      let committed = false;
      const commit = () => {
        if (disposed || committed || sequence !== latestSequence) return;
        committed = true;
        setFrame(next);
        setContractError("");
      };
      const preload = new Image();
      preload.onload = commit;
      preload.onerror = () => {
        if (disposed || sequence !== latestSequence) return;
        setContractError("브라우저가 final_overlay JPEG/PNG/WebP 프레임을 디코드하지 못했습니다.");
      };
      preload.src = next.src;
      if (typeof preload.decode === "function") {
        void preload.decode().then(commit).catch(() => undefined);
      }
    });
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, [subscribeTopic]);

  return { frame, contractError };
}

function useFinalOverlayStatus(
  subscribeTopic: DebugReadOnlyTopicSubscriber,
): FinalOverlayStatusState {
  const [status, setStatus] = useState<DebugFinalOverlayStatus | null>(null);
  const [contractError, setContractError] = useState("");

  useEffect(() => subscribeTopic({
    name: DEBUG_PERCEPTION_FINAL_OVERLAY_STATUS_TOPIC,
    messageType: "std_msgs/msg/String",
    queueLength: 1,
    reliability: "reliable",
  }, (message) => {
    const next = parseFinalOverlayStatus(message, Date.now());
    if (!next) {
      setStatus(null);
      setContractError("final_overlay status가 pnu.perception.final_overlay.v1 strict 계약과 일치하지 않습니다.");
      return;
    }
    setStatus(next);
    setContractError("");
  }), [subscribeTopic]);

  return { status, contractError };
}

function LayerStatusRow({
  label,
  layer,
}: {
  label: string;
  layer: DebugFinalOverlayLayerStatus;
}) {
  return (
    <li className={layer.state}>
      <span className="debug-direct-perception-layer-name"><StateIcon state={layer.state} /> {label}</span>
      <span>{stateLabel(layer.state)}</span>
      <span>{layer.count} result · {layer.ageSec === null ? "수신 전" : `${layer.ageSec.toFixed(2)} s`} · drop {layer.dropped}</span>
    </li>
  );
}

function CameraStatusCard({
  id,
  camera,
}: {
  id: DebugFinalOverlayCameraId;
  camera: DebugFinalOverlayCameraStatus;
}) {
  return (
    <article className={`debug-direct-perception-camera-status ${camera.state}`} data-slot={`debug-direct-perception-status-${id}`}>
      <header>
        <div>
          <p>{CAMERA_LABELS[id]} · SERVER LAYERS</p>
          <h3>{stateLabel(camera.state)}</h3>
        </div>
        <span className={`debug-direct-perception-preview ${camera.state}`}>
          <StateIcon state={camera.state} />
          {stateLabel(camera.state)}
        </span>
      </header>
      <dl className="debug-direct-perception-base-facts">
        <div><dt>BASE STAMP</dt><dd><code>{camera.base.sourceStamp?.key || "—"}</code></dd></div>
        <div><dt>AGE</dt><dd>{camera.base.ageSec === null ? "—" : `${camera.base.ageSec.toFixed(2)} s`}</dd></div>
        <div><dt>RECV / DROP</dt><dd>{camera.base.received} / {camera.base.dropped}</dd></div>
      </dl>
      <ul aria-label={`${CAMERA_LABELS[id]} 서버 레이어 상태`} className="debug-direct-perception-layer-status-list">
        {LAYER_IDS.map((layerId) => (
          <LayerStatusRow key={layerId} label={LAYER_LABELS[layerId]} layer={camera.layers[layerId]} />
        ))}
      </ul>
    </article>
  );
}

export function DirectPerceptionOverlayPanel({
  subscribeTopic,
}: DirectPerceptionOverlayPanelProps) {
  const [now, setNow] = useState(Date.now());
  const image = useFinalOverlayFrame(subscribeTopic);
  const status = useFinalOverlayStatus(subscribeTopic);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, []);

  const imageState = rasterStateFor(image.frame?.receivedAt ?? null, image.contractError, now);
  const snapshotRelation = useMemo(() => {
    if (!status.status?.output.sourceStamp || status.status.output.bytes === 0) {
      return "OUTPUT NOT YET PUBLISHED";
    }
    if (
      image.frame
      && image.frame.sourceStampKey === status.status.output.sourceStamp.key
      && image.frame.sizeBytes === status.status.output.bytes
    ) return "SAME SNAPSHOT";
    return "STATUS SNAPSHOT · LOW-RATE";
  }, [image.frame, status.status]);
  const statusError = status.contractError;
  const statusState = rasterStateFor(status.status?.receivedAt ?? null, statusError, now);

  return (
    <article
      className="debug-section-card debug-direct-perception-card"
      data-slot="debug-direct-perception-overlay-panel"
    >
      <div className="debug-section-heading">
        <div>
          <p>1.7 · SERVER-COMPOSITED FINAL OVERLAY</p>
          <h2>CAM3 + CAM4 단일 최종 인식 프레임</h2>
          <span>서버가 합성한 2-up raster 한 장만 표시합니다. 한 레이어가 누락돼도 마지막 유효 final frame은 유지됩니다.</span>
        </div>
        <span className={`debug-direct-perception-state ${imageState}`} role="status">
          <StateIcon state={imageState} /> FINAL RASTER · {rasterStateLabel(imageState)}
        </span>
      </div>

      <figure className="debug-direct-perception-figure">
        <div
          aria-label="CAM3 및 CAM4 서버 합성 최종 인식 오버레이"
          className="debug-direct-perception-viewport"
          data-source-stamp={image.frame?.sourceStampKey ?? ""}
          data-state={imageState}
          data-slot="debug-direct-perception-final-viewport"
        >
          {image.frame ? (
            <img
              alt="CAM3와 CAM4의 서버 합성 최종 인식 오버레이"
              className="debug-direct-perception-final-frame"
              data-slot="debug-direct-perception-final-overlay"
              decoding="async"
              src={image.frame.src}
            />
          ) : (
            <div className="debug-direct-perception-empty" role={imageState === "error" ? "alert" : "status"}>
              {imageState === "error" ? <AlertTriangle aria-hidden="true" size={32} /> : <ImageIcon aria-hidden="true" size={32} />}
              <strong>{imageState === "error" ? "최종 오버레이 이미지 계약을 확인하세요" : "최종 2-up 프레임 대기"}</strong>
              <span>{image.contractError || DEBUG_PERCEPTION_FINAL_OVERLAY_TOPIC}</span>
            </div>
          )}
          <span className={`debug-direct-perception-raster-badge ${imageState}`}>
            <StateIcon state={imageState} /> {rasterStateLabel(imageState)}
          </span>
        </div>
        <figcaption>
          <span><strong>RASTER</strong> <code>{DEBUG_PERCEPTION_FINAL_OVERLAY_TOPIC}</code></span>
          <span><strong>STAMP</strong> <code>{image.frame?.sourceStampKey || "—"}</code></span>
          <span><strong>AGE</strong> {formatAge(image.frame?.receivedAt ?? null, now)}</span>
          <span><strong>FORMAT</strong> {image.frame?.format || "JPEG/PNG/WebP 대기"}</span>
        </figcaption>
      </figure>

      <section className="debug-direct-perception-status-card" data-slot="debug-direct-perception-final-status" aria-labelledby="debug-direct-perception-status-heading">
        <header>
          <div>
            <p>FINAL OVERLAY STATUS</p>
            <h3 id="debug-direct-perception-status-heading">CAM3 · CAM4 server layer health</h3>
          </div>
          <span className={`debug-direct-perception-state ${statusState}`} role={statusState === "error" ? "alert" : "status"}>
            <StateIcon state={statusState} /> STATUS · {rasterStateLabel(statusState)}
          </span>
        </header>
        {status.status && !statusError ? (
          <>
            <dl className="debug-direct-perception-output-facts">
              <div><dt>OUTPUT STAMP</dt><dd><code>{status.status.output.sourceStamp?.key || "—"}</code></dd></div>
              <div><dt>RATE</dt><dd>{status.status.output.hz.toFixed(1)} Hz</dd></div>
              <div><dt>SIZE</dt><dd>{status.status.output.width} × {status.status.output.height} · {status.status.output.bytes.toLocaleString()} B</dd></div>
              <div><dt>STATUS AGE</dt><dd>{formatAge(status.status.receivedAt, now)}</dd></div>
              <div data-slot="debug-direct-perception-snapshot-relation"><dt>RASTER / STATUS</dt><dd>{snapshotRelation}</dd></div>
            </dl>
            <div className="debug-direct-perception-camera-status-grid">
              {CAMERA_IDS.map((id) => <CameraStatusCard camera={status.status!.cameras[id]} id={id} key={id} />)}
            </div>
          </>
        ) : (
          <div className="debug-direct-perception-status-empty" role={statusError ? "alert" : "status"}>
            {statusError ? <AlertTriangle aria-hidden="true" size={20} /> : <Radio aria-hidden="true" size={20} />}
            <div>
              <strong>{statusError ? "상태 계약 오류" : "상태 레코드 대기"}</strong>
              <span>{statusError || DEBUG_PERCEPTION_FINAL_OVERLAY_STATUS_TOPIC}</span>
            </div>
          </div>
        )}
      </section>

      <p className="debug-direct-perception-note">
        이 화면은 공유 final raster를 읽기 전용으로 표시합니다. 브라우저별 레이어 토글은 제공하지 않으며, 합성 레이어의 활성화·live·stale 상태는 위 server status에서만 해석합니다.
      </p>
    </article>
  );
}
