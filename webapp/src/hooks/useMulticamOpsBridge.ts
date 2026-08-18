import { startTransition, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import ROSLIB from "roslib";

import { multicamBridgeUrl } from "../runtimeModes";

export const MULTICAM_CAMERAS = [
  {
    id: "cam_1",
    label: "CAM 1",
    serial: "339522301105",
    colorTopic: "/synced/cam_1/color/image_raw/compressed",
    depthTopic: "/synced/cam_1/depth/image_rect_raw/compressedDepth",
  },
  {
    id: "cam_2",
    label: "CAM 2",
    serial: "338522301897",
    colorTopic: "/synced/cam_2/color/image_raw/compressed",
    depthTopic: "/synced/cam_2/depth/image_rect_raw/compressedDepth",
  },
  {
    id: "cam_3",
    label: "CAM 3",
    serial: "146222253041",
    colorTopic: "/synced/cam_3/color/image_raw/compressed",
    depthTopic: "/synced/cam_3/depth/image_rect_raw/compressedDepth",
  },
  {
    id: "cam_4",
    label: "CAM 4",
    serial: "146222251000",
    colorTopic: "/synced/cam_4/color/image_raw/compressed",
    depthTopic: "/synced/cam_4/depth/image_rect_raw/compressedDepth",
  },
  {
    id: "flir",
    label: "FLIR",
    serial: "25054909",
    colorTopic: "/synced/flir/color/image_raw/compressed",
    depthTopic: null,
  },
] as const;

export type MulticamCameraId = (typeof MULTICAM_CAMERAS)[number]["id"];
export type MulticamView = "color" | "depth";
export type WorldAction = "begin" | "stop" | "solve" | "publish";

export type CameraFrame = {
  src: string;
  objectUrl: boolean;
  format: string;
  topic: string;
  frameId: string;
  receivedAt: number;
  previewHz: number;
};

export type CameraFrames = Record<MulticamCameraId, CameraFrame | null>;

export type CaptureCameraCoverage = {
  camera_name: string;
  processed_frames: number;
  usable_frames: number;
  tags_last_frame: number;
  area_coverage: number;
  detect_rate_hz: number;
  ok: boolean;
};

export type CaptureStatus = {
  receivedAt: number;
  online_cameras: string[];
  offline_cameras: string[];
  all_cameras_online: boolean;
  uptime_sec: number;
  recording: boolean;
  session_name: string;
  capture_dir: string;
  elapsed_sec: number;
  calib_bag_uri: string;
  calib_bag_elapsed_sec: number;
  calib_bag_index: number;
  cameras: CaptureCameraCoverage[];
  multi_cam_frames: number;
  pair_names: string[];
  pair_frames: number[];
  min_pair_frames: number;
  synced_frames: number;
  max_sync_skew_ms: number;
  ready_for_calibration: boolean;
  hint: string;
  calibrating: boolean;
  calibration_stage: string;
  calibration_progress: number;
  extrinsics_json: string;
  published_frames: string[];
};

export type WorldAnchorStatus = {
  receivedAt: number;
  valid: boolean;
  collecting: boolean;
  reference_frame: string;
  world_frame: string;
  min_samples: number;
  message: string;
  tags: Record<string, {
    role?: string;
    size?: number;
    total?: number;
    per_camera?: Record<string, { count?: number; fresh?: boolean }>;
  }>;
  raw: string;
};

export type StaticTransform = {
  parentFrame: string;
  childFrame: string;
  translation: { x: number; y: number; z: number };
  rotation: { x: number; y: number; z: number; w: number };
};

export type TopicInventoryRow = {
  name: string;
  type: string;
};

export type TopicSample = {
  topic: string;
  type: string;
  receivedAt: number;
  hz: number;
  count: number;
  preview: string;
};

export type WorldActionResult = {
  action: WorldAction;
  success: boolean;
  message: string;
  completedAt: number;
};

const IMAGE_QUEUE_LENGTH = 1;
// `/tf_static` is sent as a retained snapshot by each publisher when a browser
// subscribes. Keep enough bridge-side messages to merge the snapshots from the
// world-anchor and multicam publishers instead of retaining only the last one.
const STATIC_TF_QUEUE_LENGTH = 32;
// The multicam synchronizer is the rate authority (currently 15 Hz). Do not
// add a browser-side ROSBridge throttle: render each /synced message received.
const IMAGE_THROTTLE_MS = 0;
const CAPTURE_STATUS_MAX_AGE_MS = 3_500;
const TOPIC_DISCOVERY_TIMEOUT_MS = 4_000;
const OBSERVER_TOPICS_SERVICE = "/multicam_observer/rosapi/topics";
const IMAGE_QOS = {
  history: "keep_last",
  depth: 1,
  reliability: "reliable",
  durability: "volatile",
} as const;
const TF_STATIC_QOS = {
  history: "keep_last",
  // Individual static-transform publishers may each retain one message. Keep a
  // bounded burst so initial discovery does not lose sibling frame trees.
  depth: 32,
  reliability: "reliable",
  durability: "transient_local",
} as const;

type ObserverServiceResponseMessage = {
  result?: boolean;
  values?: Record<string, unknown> | string;
};

type ObserverServiceConnection = {
  idCounter?: number;
  isConnected?: boolean;
  on: (event: string, callback: (message: ObserverServiceResponseMessage) => void) => void;
  off?: (event: string, callback: (message: ObserverServiceResponseMessage) => void) => void;
  removeListener?: (event: string, callback: (message: ObserverServiceResponseMessage) => void) => void;
  callOnConnection: (message: Record<string, unknown>) => void;
};

function emptyFrames(): CameraFrames {
  return {
    cam_1: null,
    cam_2: null,
    cam_3: null,
    cam_4: null,
    flir: null,
  };
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}

function finite(value: unknown): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function mimeType(format: string): string {
  const normalized = format.toLowerCase();
  if (normalized.includes("png")) return "image/png";
  if (normalized.includes("webp")) return "image/webp";
  return "image/jpeg";
}

function bytesToBase64(data: number[] | Uint8Array): string {
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < data.length; offset += chunkSize) {
    const chunk = data.slice(offset, offset + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return window.btoa(binary);
}

function base64ToBytes(value: string): Uint8Array | null {
  try {
    const encoded = value.includes(",") ? value.slice(value.indexOf(",") + 1) : value;
    const binary = window.atob(encoded);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return bytes;
  } catch {
    return null;
  }
}

function imageBytes(data: unknown): Uint8Array | null {
  if (typeof data === "string") return base64ToBytes(data);
  if (Array.isArray(data)) return new Uint8Array(data);
  if (data instanceof Uint8Array) return data;
  return null;
}

function pngBytesFromCompressedDepth(bytes: Uint8Array | null): Uint8Array | null {
  if (!bytes) return null;
  const pngMagic = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
  for (let offset = 0; offset <= bytes.length - pngMagic.length; offset += 1) {
    if (pngMagic.every((value, index) => bytes[offset + index] === value)) {
      return bytes.slice(offset);
    }
  }
  return null;
}

function blobUrlFromBytes(bytes: Uint8Array, type: string): string {
  // Copy into an ArrayBuffer-backed view because the received CBOR view may be
  // backed by SharedArrayBuffer, which Blob does not accept in every browser.
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return URL.createObjectURL(new Blob([copy.buffer], { type }));
}

function decodeCompressedImage(message: unknown, topic: string, previewHz: number): CameraFrame | null {
  if (!message || typeof message !== "object") return null;
  const raw = message as {
    format?: unknown;
    data?: unknown;
    header?: { frame_id?: unknown };
  };
  if (typeof raw.data !== "string" && !Array.isArray(raw.data) && !(raw.data instanceof Uint8Array)) {
    return null;
  }
  const format = String(raw.format || "jpeg");
  const bytes = imageBytes(raw.data);
  const imagePayload = format.toLowerCase().includes("compresseddepth")
    ? pngBytesFromCompressedDepth(bytes)
    : bytes;
  const objectUrl = Boolean(imagePayload);
  const fallbackSrc = imagePayload
    ? ""
    : (() => {
      const base64 = typeof raw.data === "string" ? raw.data : bytesToBase64(raw.data);
      return base64.startsWith("data:") ? base64 : `data:${mimeType(format)};base64,${base64}`;
    })();
  return {
    // Blob URLs keep the compressed /synced payload intact and avoid a second
    // base64 encode/decode cycle for every image frame in the browser.
    src: imagePayload
      ? blobUrlFromBytes(imagePayload, mimeType(format))
      : fallbackSrc,
    objectUrl,
    format,
    topic,
    frameId: String(raw.header?.frame_id || ""),
    receivedAt: Date.now(),
    previewHz,
  };
}

function captureStatusFromMessage(message: unknown): CaptureStatus {
  const raw = message && typeof message === "object" ? message as Record<string, unknown> : {};
  const coverage = Array.isArray(raw.cameras)
    ? raw.cameras.map((camera) => {
      const row = camera && typeof camera === "object" ? camera as Record<string, unknown> : {};
      return {
        camera_name: String(row.camera_name || ""),
        processed_frames: finite(row.processed_frames),
        usable_frames: finite(row.usable_frames),
        tags_last_frame: finite(row.tags_last_frame),
        area_coverage: finite(row.area_coverage),
        detect_rate_hz: finite(row.detect_rate_hz),
        ok: Boolean(row.ok),
      };
    })
    : [];
  return {
    receivedAt: Date.now(),
    online_cameras: stringArray(raw.online_cameras),
    offline_cameras: stringArray(raw.offline_cameras),
    all_cameras_online: Boolean(raw.all_cameras_online),
    uptime_sec: finite(raw.uptime_sec),
    recording: Boolean(raw.recording),
    session_name: String(raw.session_name || ""),
    capture_dir: String(raw.capture_dir || ""),
    elapsed_sec: finite(raw.elapsed_sec),
    calib_bag_uri: String(raw.calib_bag_uri || ""),
    calib_bag_elapsed_sec: finite(raw.calib_bag_elapsed_sec),
    calib_bag_index: finite(raw.calib_bag_index),
    cameras: coverage,
    multi_cam_frames: finite(raw.multi_cam_frames),
    pair_names: stringArray(raw.pair_names),
    pair_frames: Array.isArray(raw.pair_frames) ? raw.pair_frames.map(finite) : [],
    min_pair_frames: finite(raw.min_pair_frames),
    synced_frames: finite(raw.synced_frames),
    max_sync_skew_ms: finite(raw.max_sync_skew_ms),
    ready_for_calibration: Boolean(raw.ready_for_calibration),
    hint: String(raw.hint || "capture status를 기다리는 중입니다."),
    calibrating: Boolean(raw.calibrating),
    calibration_stage: String(raw.calibration_stage || ""),
    calibration_progress: finite(raw.calibration_progress),
    extrinsics_json: String(raw.extrinsics_json || ""),
    published_frames: stringArray(raw.published_frames),
  };
}

function worldStatusFromMessage(message: unknown): WorldAnchorStatus {
  const rawText = String((message as { data?: unknown } | null)?.data || "");
  try {
    const raw = JSON.parse(rawText) as Record<string, unknown>;
    const tags = raw.tags && typeof raw.tags === "object"
      ? raw.tags as WorldAnchorStatus["tags"]
      : {};
    return {
      receivedAt: Date.now(),
      valid: true,
      collecting: Boolean(raw.collecting),
      reference_frame: String(raw.reference_frame || ""),
      world_frame: String(raw.world_frame || ""),
      min_samples: finite(raw.min_samples),
      message: String(raw.message || ""),
      tags,
      raw: rawText,
    };
  } catch {
    return {
      receivedAt: Date.now(),
      valid: false,
      collecting: false,
      reference_frame: "",
      world_frame: "",
      min_samples: 0,
      message: rawText || "world anchor status를 기다리는 중입니다.",
      tags: {},
      raw: rawText,
    };
  }
}

function transformsFromMessage(message: unknown): StaticTransform[] {
  const transforms = (message as { transforms?: unknown } | null)?.transforms;
  if (!Array.isArray(transforms)) return [];
  return transforms.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const raw = item as {
      header?: { frame_id?: unknown };
      child_frame_id?: unknown;
      transform?: {
        translation?: { x?: unknown; y?: unknown; z?: unknown };
        rotation?: { x?: unknown; y?: unknown; z?: unknown; w?: unknown };
      };
    };
    const parentFrame = String(raw.header?.frame_id || "").trim();
    const childFrame = String(raw.child_frame_id || "").trim();
    if (!parentFrame || !childFrame) return [];
    return [{
      parentFrame,
      childFrame,
      translation: {
        x: finite(raw.transform?.translation?.x),
        y: finite(raw.transform?.translation?.y),
        z: finite(raw.transform?.translation?.z),
      },
      rotation: {
        x: finite(raw.transform?.rotation?.x),
        y: finite(raw.transform?.rotation?.y),
        z: finite(raw.transform?.rotation?.z),
        w: finite(raw.transform?.rotation?.w) || 1,
      },
    }];
  });
}

function safePreview(value: unknown, depth = 0): unknown {
  if (depth > 5) return "…";
  if (value === null || value === undefined || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "string") return value.length > 480 ? `${value.slice(0, 480)}…` : value;
  if (value instanceof Uint8Array) return `<binary ${value.byteLength.toLocaleString()} bytes omitted>`;
  if (Array.isArray(value)) {
    const visible = value.slice(0, 24).map((item) => safePreview(item, depth + 1));
    return value.length > visible.length ? [...visible, `… ${value.length - visible.length} more`] : visible;
  }
  if (typeof value === "object") {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>).slice(0, 48)) {
      if (key === "data" && (typeof item === "string" || Array.isArray(item) || item instanceof Uint8Array)) {
        const length = typeof item === "string" ? item.length : item.length;
        result[key] = `<binary payload ${length.toLocaleString()} bytes omitted>`;
      } else {
        result[key] = safePreview(item, depth + 1);
      }
    }
    return result;
  }
  return String(value);
}

function previewMessage(value: unknown): string {
  const serialized = JSON.stringify(safePreview(value), null, 2);
  return serialized.length > 12_000 ? `${serialized.slice(0, 12_000)}\n… payload preview truncated` : serialized;
}

function injectQos(topic: any, qos: Record<string, unknown>): void {
  const sendOnConnection = topic.callForSubscribeAndAdvertise.bind(topic);
  topic.callForSubscribeAndAdvertise = (request: Record<string, unknown>) => {
    sendOnConnection(request.op === "subscribe" ? { ...request, qos } : request);
  };
}

function unsubscribeWhileConnected(ros: any, topics: any[]): void {
  if (!ros?.isConnected || ros.socket?.readyState !== WebSocket.OPEN) return;
  topics.forEach((topic) => {
    try {
      topic.unsubscribe();
    } catch {
      // Closing the socket already releases server-side subscriptions.
    }
  });
}

export function useMulticamOpsBridge(activeView: MulticamView) {
  const [url] = useState(multicamBridgeUrl);
  const [retryGeneration, setRetryGeneration] = useState(0);
  const [socketConnected, setSocketConnected] = useState(false);
  const [captureTopicDiscovered, setCaptureTopicDiscovered] = useState(false);
  const [captureStatusFresh, setCaptureStatusFresh] = useState(false);
  const [connectionMessage, setConnectionMessage] = useState("멀티캠 observer 연결 대기");
  const [colorFrames, setColorFrames] = useState<CameraFrames>(emptyFrames);
  const [depthFrames, setDepthFrames] = useState<CameraFrames>(emptyFrames);
  const [captureStatus, setCaptureStatus] = useState<CaptureStatus | null>(null);
  const [worldStatus, setWorldStatus] = useState<WorldAnchorStatus | null>(null);
  const [tfTransforms, setTfTransforms] = useState<StaticTransform[]>([]);
  const [topics, setTopics] = useState<TopicInventoryRow[]>([]);
  const [topicError, setTopicError] = useState("");
  const [selectedTopic, setSelectedTopic] = useState("/multicam_node/capture_status");
  const [selectedTopicSample, setSelectedTopicSample] = useState<TopicSample | null>(null);
  const [worldActionPending, setWorldActionPending] = useState<WorldAction | null>(null);
  const [worldActionResult, setWorldActionResult] = useState<WorldActionResult | null>(null);
  const [activeRos, setActiveRos] = useState<any>(null);
  const connected = socketConnected && captureStatusFresh;

  const rosRef = useRef<any>(null);
  const tfByChildRef = useRef(new Map<string, StaticTransform>());
  const colorPendingRef = useRef(new Map<MulticamCameraId, CameraFrame>());
  const depthPendingRef = useRef(new Map<MulticamCameraId, CameraFrame>());
  const previewTimesRef = useRef(new Map<string, number[]>());
  const frameFlushRef = useRef<number | null>(null);
  const objectUrlsRef = useRef(new Set<string>());
  const captureStatusFreshRef = useRef(false);
  const observerGenerationRef = useRef(0);
  const topicRefreshInFlightRef = useRef(false);
  const pendingServiceCancelsRef = useRef(new Set<(reason: string) => void>());

  const selectedTopicType = useMemo(
    () => topics.find((topic) => topic.name === selectedTopic)?.type || "",
    [selectedTopic, topics],
  );

  const callObserverTopics = useCallback((
    ros: ObserverServiceConnection,
    generation: number,
  ): Promise<Record<string, unknown>> => new Promise((resolve, reject) => {
    const serviceCallId = `call_service:${OBSERVER_TOPICS_SERVICE}:${Number(ros.idCounter ?? 0) + 1}`;
    ros.idCounter = Number(ros.idCounter ?? 0) + 1;
    let timeout = 0;
    let settled = false;
    let cancel = (_reason: string) => {};
    const cleanup = (handler: (message: ObserverServiceResponseMessage) => void) => {
      window.clearTimeout(timeout);
      pendingServiceCancelsRef.current.delete(cancel);
      if (typeof ros.off === "function") ros.off(serviceCallId, handler);
      else if (typeof ros.removeListener === "function") ros.removeListener(serviceCallId, handler);
    };
    const handler = (message: ObserverServiceResponseMessage) => {
      if (settled) return;
      if (
        generation !== observerGenerationRef.current ||
        rosRef.current !== ros ||
        !ros.isConnected
      ) {
        cancel("멀티캠 observer 연결이 변경되어 이전 서비스 응답을 무시했습니다.");
        return;
      }
      settled = true;
      cleanup(handler);
      if (message.result === false) {
        reject(new Error(String(message.values || "rosapi topic discovery failed")));
        return;
      }
      resolve(
        typeof message.values === "object" && message.values !== null
          ? message.values
          : {},
      );
    };
    cancel = (reason: string) => {
      if (settled) return;
      settled = true;
      cleanup(handler);
      reject(new Error(reason));
    };
    pendingServiceCancelsRef.current.add(cancel);
    timeout = window.setTimeout(
      () => cancel("멀티캠 observer 토픽 조회 응답 시간이 초과되었습니다."),
      TOPIC_DISCOVERY_TIMEOUT_MS,
    );
    ros.on(serviceCallId, handler);
    try {
      ros.callOnConnection({
        op: "call_service",
        id: serviceCallId,
        service: OBSERVER_TOPICS_SERVICE,
        type: "rosapi_msgs/srv/Topics",
        args: new ROSLIB.ServiceRequest({}),
        timeout: TOPIC_DISCOVERY_TIMEOUT_MS / 1_000,
      });
    } catch (error) {
      cancel(error instanceof Error ? error.message : String(error));
    }
  }), []);

  const refreshTopics = useCallback(() => {
    const ros = rosRef.current as ObserverServiceConnection | null;
    const generation = observerGenerationRef.current;
    if (!ros?.isConnected || topicRefreshInFlightRef.current) return;
    topicRefreshInFlightRef.current = true;
    // roslib's legacy getTopics() identifies this ROS 2 service as
    // `rosapi/Topics`, which makes rosbridge try to import the nonexistent
    // Python module rosapi.srv. Call the canonical Jazzy interface directly,
    // with an explicit listener timeout so a silent observer cannot accumulate
    // one pending response handler on every polling interval.
    void callObserverTopics(ros, generation)
      .then((result) => {
        if (observerGenerationRef.current !== generation || rosRef.current !== ros) return;
        const names = Array.isArray(result.topics) ? result.topics : [];
        const types = Array.isArray(result.types) ? result.types : [];
        const next = names
          .map((name, index) => ({ name: String(name), type: String(types[index] || "unknown") }))
          .filter((topic) => topic.name.startsWith("/"))
          .sort((left, right) => left.name.localeCompare(right.name));
        setTopics(next);
        const observerReady = next.some(
          (topic) => topic.name === "/multicam_node/capture_status",
        );
        setTopicError(observerReady ? "" : "멀티캠 observer 토픽을 찾지 못했습니다.");
        if (!observerReady) {
          setCaptureTopicDiscovered(false);
          setConnectionMessage("멀티캠 observer unavailable · CaptureStatus 토픽 미발견");
        } else {
          setCaptureTopicDiscovered(true);
          if (!captureStatusFreshRef.current) {
            setConnectionMessage("CaptureStatus 토픽 발견 · 실제 메시지 수신 대기");
          }
        }
        setSelectedTopic((current) => {
          if (next.some((topic) => topic.name === current)) return current;
          return next.find((topic) => topic.name === "/multicam_node/capture_status")?.name || next[0]?.name || "";
        });
      })
      .catch((error: unknown) => {
        if (observerGenerationRef.current !== generation || rosRef.current !== ros) return;
        const message = String((error as { error?: unknown } | null)?.error || "rosapi topic discovery failed");
        setCaptureTopicDiscovered(false);
        setTopicError(message);
        setConnectionMessage("멀티캠 observer unavailable · 실행 중인 런타임은 변경하지 않았습니다.");
      })
      .finally(() => {
        if (observerGenerationRef.current === generation && rosRef.current === ros) {
          topicRefreshInFlightRef.current = false;
        }
      });
  }, [callObserverTopics]);

  const retry = useCallback(() => {
    setRetryGeneration((current) => current + 1);
  }, []);

  const callWorldAction = useCallback(async (action: WorldAction): Promise<WorldActionResult> => {
    const result: WorldActionResult = {
      action,
      success: false,
      message: "멀티캠 observer는 read-only입니다. World Anchor 서비스 호출을 전송하지 않았습니다.",
      completedAt: Date.now(),
    };
    setWorldActionResult(result);
    return result;
  }, []);

  useLayoutEffect(() => {
    let disposed = false;
    let reconnectTimer: number | null = null;
    let connectionTimer: number | null = null;
    const generation = observerGenerationRef.current + 1;
    observerGenerationRef.current = generation;
    for (const cancel of Array.from(pendingServiceCancelsRef.current)) {
      cancel("멀티캠 observer 연결 세대가 변경되어 토픽 조회를 취소했습니다.");
    }
    topicRefreshInFlightRef.current = false;
    const isCurrentGeneration = () =>
      !disposed && observerGenerationRef.current === generation;
    captureStatusFreshRef.current = false;
    tfByChildRef.current.clear();
    colorPendingRef.current.clear();
    depthPendingRef.current.clear();
    previewTimesRef.current.clear();
    setSocketConnected(false);
    setCaptureTopicDiscovered(false);
    setCaptureStatusFresh(false);
    setCaptureStatus(null);
    setWorldStatus(null);
    setTfTransforms([]);
    setTopics([]);
    setTopicError("");
    setSelectedTopic("/multicam_node/capture_status");
    setSelectedTopicSample(null);
    setWorldActionPending(null);
    setWorldActionResult(null);
    setColorFrames(emptyFrames());
    setDepthFrames(emptyFrames());
    setActiveRos(null);
    setConnectionMessage("멀티캠 observer 연결 대기");
    const scheduleReconnect = () => {
      if (!isCurrentGeneration() || reconnectTimer !== null) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        setRetryGeneration((current) => current + 1);
      }, 1_500);
    };
    const ros = new ROSLIB.Ros();
    rosRef.current = ros;

    ros.on("connection", () => {
      if (!isCurrentGeneration()) return;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
      setSocketConnected(true);
      setCaptureTopicDiscovered(false);
      captureStatusFreshRef.current = false;
      setCaptureStatusFresh(false);
      setActiveRos(ros);
      setConnectionMessage("멀티캠 observer 연결 확인 중");
      refreshTopics();
    });
    ros.on("close", () => {
      if (!isCurrentGeneration()) return;
      for (const cancel of Array.from(pendingServiceCancelsRef.current)) {
        cancel("멀티캠 observer 연결이 종료되어 토픽 조회를 취소했습니다.");
      }
      topicRefreshInFlightRef.current = false;
      setSocketConnected(false);
      setCaptureTopicDiscovered(false);
      captureStatusFreshRef.current = false;
      setCaptureStatusFresh(false);
      setActiveRos((current: any) => current === ros ? null : current);
      setConnectionMessage("멀티캠 observer 연결이 끊겼습니다. 재시도 중입니다.");
      scheduleReconnect();
    });
    ros.on("error", () => {
      if (!isCurrentGeneration()) return;
      for (const cancel of Array.from(pendingServiceCancelsRef.current)) {
        cancel("멀티캠 observer 연결 오류로 토픽 조회를 취소했습니다.");
      }
      topicRefreshInFlightRef.current = false;
      setSocketConnected(false);
      setCaptureTopicDiscovered(false);
      captureStatusFreshRef.current = false;
      setCaptureStatusFresh(false);
      setActiveRos((current: any) => current === ros ? null : current);
      setConnectionMessage("멀티캠 observer unavailable · 실행 중인 런타임은 변경하지 않았습니다.");
      scheduleReconnect();
    });

    const subscriptions: any[] = [];
    const captureTopic = new ROSLIB.Topic({
      ros,
      name: "/multicam_node/capture_status",
      messageType: "arpa_multicam_msgs/msg/CaptureStatus",
      queue_length: 1,
    });
    captureTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      setCaptureStatus(captureStatusFromMessage(message));
      captureStatusFreshRef.current = true;
      setCaptureStatusFresh(true);
      setConnectionMessage("멀티캠 observer ready · CaptureStatus fresh");
    });
    subscriptions.push(captureTopic);

    const worldTopic = new ROSLIB.Topic({
      ros,
      name: "/world_anchor_node/status",
      messageType: "std_msgs/msg/String",
      queue_length: 1,
    });
    worldTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      setWorldStatus(worldStatusFromMessage(message));
    });
    subscriptions.push(worldTopic);

    const tfTopic = new ROSLIB.Topic({
      ros,
      name: "/tf_static",
      messageType: "tf2_msgs/msg/TFMessage",
      queue_length: STATIC_TF_QUEUE_LENGTH,
    });
    injectQos(tfTopic, TF_STATIC_QOS);
    tfTopic.subscribe((message: unknown) => {
      if (!isCurrentGeneration()) return;
      const additions = transformsFromMessage(message);
      if (!additions.length) return;
      for (const transform of additions) tfByChildRef.current.set(transform.childFrame, transform);
      setTfTransforms([...tfByChildRef.current.values()].sort((left, right) => left.childFrame.localeCompare(right.childFrame)));
    });
    subscriptions.push(tfTopic);
    connectionTimer = window.setTimeout(() => {
      connectionTimer = null;
      if (isCurrentGeneration()) ros.connect(url);
    }, 0);

    return () => {
      disposed = true;
      if (observerGenerationRef.current === generation) {
        for (const cancel of Array.from(pendingServiceCancelsRef.current)) {
          cancel("멀티캠 observer 화면을 닫아 토픽 조회를 취소했습니다.");
        }
        topicRefreshInFlightRef.current = false;
      }
      if (connectionTimer !== null) window.clearTimeout(connectionTimer);
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      unsubscribeWhileConnected(ros, subscriptions);
      if (frameFlushRef.current !== null) window.cancelAnimationFrame(frameFlushRef.current);
      frameFlushRef.current = null;
      tfByChildRef.current.clear();
      objectUrlsRef.current.forEach((objectUrl) => URL.revokeObjectURL(objectUrl));
      objectUrlsRef.current.clear();
      colorPendingRef.current.clear();
      depthPendingRef.current.clear();
      setActiveRos((current: any) => current === ros ? null : current);
      if (rosRef.current === ros) rosRef.current = null;
      if (typeof ros.close === "function") ros.close();
    };
  }, [refreshTopics, retryGeneration, url]);

  useEffect(() => {
    const generation = observerGenerationRef.current;
    const updateFreshness = () => {
      if (observerGenerationRef.current !== generation) return;
      const fresh = Boolean(
        socketConnected &&
        captureStatus &&
        Date.now() - captureStatus.receivedAt <= CAPTURE_STATUS_MAX_AGE_MS,
      );
      captureStatusFreshRef.current = fresh;
      setCaptureStatusFresh((current) => {
        if (current && !fresh && socketConnected) {
          setConnectionMessage("멀티캠 observer degraded · CaptureStatus stale");
        }
        return fresh;
      });
    };
    updateFreshness();
    const timer = window.setInterval(updateFreshness, 500);
    return () => window.clearInterval(timer);
  }, [captureStatus, socketConnected]);

  useEffect(() => {
    if (!activeRos) return;
    const generation = observerGenerationRef.current;
    const isCurrentGeneration = () =>
      observerGenerationRef.current === generation && rosRef.current === activeRos;
    const pendingFrames = activeView === "color" ? colorPendingRef : depthPendingRef;
    const setFrames = activeView === "color" ? setColorFrames : setDepthFrames;
    const imageTopics = MULTICAM_CAMERAS.flatMap((camera) => {
      const name = activeView === "color" ? camera.colorTopic : camera.depthTopic;
      return name ? [{ camera, name }] : [];
    });
    const subscriptions = imageTopics.map(({ camera, name }) => {
      const topic = new ROSLIB.Topic({
        ros: activeRos,
        name,
        messageType: "sensor_msgs/msg/CompressedImage",
        compression: "cbor",
        throttle_rate: IMAGE_THROTTLE_MS,
        queue_length: IMAGE_QUEUE_LENGTH,
      });
      injectQos(topic, IMAGE_QOS);
      const streamKey = `${camera.id}:${activeView}`;
      previewTimesRef.current.delete(streamKey);
      topic.subscribe((message: unknown) => {
        if (!isCurrentGeneration()) return;
        const now = Date.now();
        const samples = previewTimesRef.current.get(streamKey) || [];
        samples.push(now);
        while (samples.length && now - samples[0] > 5_000) samples.shift();
        previewTimesRef.current.set(streamKey, samples);
        const frame = decodeCompressedImage(message, name, samples.length / 5);
        if (!frame) return;
        if (frame.objectUrl) objectUrlsRef.current.add(frame.src);
        const pendingFrame = pendingFrames.current.get(camera.id);
        if (pendingFrame?.objectUrl) {
          objectUrlsRef.current.delete(pendingFrame.src);
          URL.revokeObjectURL(pendingFrame.src);
        }
        pendingFrames.current.set(camera.id, frame);
        if (frameFlushRef.current !== null) return;
        frameFlushRef.current = window.requestAnimationFrame(() => {
          frameFlushRef.current = null;
          if (!isCurrentGeneration()) return;
          const nextFrames = pendingFrames.current;
          if (!nextFrames.size) return;
          if (activeView === "color") colorPendingRef.current = new Map();
          else depthPendingRef.current = new Map();
          startTransition(() => {
            setFrames((current) => {
              const next = { ...current };
              nextFrames.forEach((nextFrame, id) => {
                const previousFrame = current[id];
                if (previousFrame?.objectUrl) {
                  objectUrlsRef.current.delete(previousFrame.src);
                  window.setTimeout(() => URL.revokeObjectURL(previousFrame.src), 250);
                }
                next[id] = nextFrame;
              });
              return next;
            });
          });
        });
      });
      return topic;
    });
    return () => {
      unsubscribeWhileConnected(activeRos, subscriptions);
      if (frameFlushRef.current !== null) {
        window.cancelAnimationFrame(frameFlushRef.current);
        frameFlushRef.current = null;
      }
      pendingFrames.current.forEach((frame) => {
        if (!frame.objectUrl) return;
        objectUrlsRef.current.delete(frame.src);
        URL.revokeObjectURL(frame.src);
      });
      pendingFrames.current.clear();
    };
  }, [activeRos, activeView]);

  useEffect(() => {
    if (!socketConnected) return;
    refreshTopics();
    const timer = window.setInterval(refreshTopics, 5_000);
    return () => window.clearInterval(timer);
  }, [refreshTopics, socketConnected]);

  useEffect(() => {
    const ros = rosRef.current;
    if (!ros || !connected || !selectedTopic || !selectedTopicType) {
      setSelectedTopicSample(null);
      return;
    }
    const isCompressedImage = selectedTopicType.includes("CompressedImage");
    const generation = observerGenerationRef.current;
    const topic = new ROSLIB.Topic({
      ros,
      name: selectedTopic,
      messageType: selectedTopicType,
      compression: isCompressedImage ? "cbor" : "none",
      throttle_rate: isCompressedImage ? IMAGE_THROTTLE_MS : 250,
      queue_length: 1,
    });
    const samples: number[] = [];
    setSelectedTopicSample(null);
    topic.subscribe((message: unknown) => {
      if (observerGenerationRef.current !== generation || rosRef.current !== ros) return;
      const now = Date.now();
      samples.push(now);
      while (samples.length && now - samples[0] > 5_000) samples.shift();
      setSelectedTopicSample({
        topic: selectedTopic,
        type: selectedTopicType,
        receivedAt: now,
        hz: samples.length / 5,
        count: samples.length,
        preview: previewMessage(message),
      });
    });
    return () => unsubscribeWhileConnected(ros, [topic]);
  }, [connected, selectedTopic, selectedTopicType]);

  return {
    url,
    connected,
    socketConnected,
    captureTopicDiscovered,
    captureStatusFresh,
    connectionMessage,
    retry,
    colorFrames,
    depthFrames,
    captureStatus,
    worldStatus,
    tfTransforms,
    topics,
    topicError,
    refreshTopics,
    selectedTopic,
    setSelectedTopic,
    selectedTopicType,
    selectedTopicSample,
    worldActionPending,
    worldActionResult,
    callWorldAction,
  };
}
