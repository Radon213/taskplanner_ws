import type { CompressedImageFrame, RosTime } from "../types";

/**
 * The operator stage has a deliberately separate raster lane.  Camera frames
 * are not application state: putting 5 x 15 Hz JPEGs through the root React
 * tree makes every unrelated panel compete with image decode and paint.
 *
 * This store owns only browser-local Blob URLs and source metadata.  It has no
 * ROS control authority; useRosBridge remains the sole transport owner.
 */
export const LIVE_STAGE_CAMERA_IDS = [
  "cam1",
  "cam2",
  "cam3",
  "cam4",
  "flir",
] as const;

export type LiveStageCameraId = (typeof LIVE_STAGE_CAMERA_IDS)[number];

export type RosCompressedImagePayload = {
  header?: {
    stamp?: RosTime;
    frame_id?: string;
  };
  format?: string;
  /** CBOR rosbridge delivers a Uint8Array.  Arrays are accepted for replay. */
  data?: Uint8Array | ArrayBuffer | number[];
};

export type LiveCameraMediaListener = (
  frame: CompressedImageFrame | null,
) => void;

type PendingFrame = {
  message: RosCompressedImagePayload;
  topic: string;
  receivedAt: number;
};

const MAX_COMPRESSED_IMAGE_BYTES = 12 * 1024 * 1024;
// Keep the former decoded Blob alive across at least several source periods.
// Revoking it after 160 ms can race an overloaded browser's JPEG decode and
// briefly blank an otherwise healthy viewport. This bounded grace window is
// released per camera and does not create an unbounded frame queue.
const RELEASE_PREVIOUS_URL_AFTER_MS = 1_500;

function mimeTypeFromCompressedFormat(format: string): string {
  const normalized = format.toLowerCase();
  if (normalized.includes("png")) return "image/png";
  if (normalized.includes("webp")) return "image/webp";
  return "image/jpeg";
}

function sourceStampSec(stamp: RosTime | undefined): number | undefined {
  const sec = Number(stamp?.sec);
  const nanosec = Number(stamp?.nanosec);
  if (!Number.isFinite(sec) || !Number.isFinite(nanosec)) return undefined;
  return sec + nanosec / 1_000_000_000;
}

function imageBytes(data: RosCompressedImagePayload["data"]): Uint8Array | null {
  if (data instanceof Uint8Array) return data;
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (Array.isArray(data) && data.every((value) => Number.isInteger(value) && value >= 0 && value <= 255)) {
    return new Uint8Array(data);
  }
  return null;
}

function frameFromPending(pending: PendingFrame): CompressedImageFrame | null {
  const bytes = imageBytes(pending.message.data);
  if (!bytes || bytes.byteLength <= 0 || bytes.byteLength > MAX_COMPRESSED_IMAGE_BYTES) {
    return null;
  }
  const format = typeof pending.message.format === "string" && pending.message.format.trim()
    ? pending.message.format
    : "jpeg";
  // A fresh ArrayBuffer makes this work with CBOR views backed by SharedArrayBuffer
  // as well as ordinary ArrayBuffer views.  No Uint8Array -> base64 conversion
  // happens on the live stage path.
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  const src = URL.createObjectURL(
    new Blob([copy.buffer], { type: mimeTypeFromCompressedFormat(format) }),
  );
  return {
    src,
    format,
    topic: pending.topic,
    frameId: pending.message.header?.frame_id || "",
    sourceStampSec: sourceStampSec(pending.message.header?.stamp),
    sizeBytes: bytes.byteLength,
    receivedAt: pending.receivedAt,
  };
}

/**
 * Browser-local latest-frame holder for the five stage cameras.
 *
 * rAF coalescing is an overload valve, not a normal throttle: a 15 Hz source
 * reaches the next paint before its following frame arrives.  If the browser
 * misses that deadline, keeping only the newest waiting frame prevents stale
 * video from turning into an ever-growing latency queue.
 */
export class LiveCameraMediaStore {
  private readonly listeners = new Map<LiveStageCameraId, Set<LiveCameraMediaListener>>();
  private readonly frames = new Map<LiveStageCameraId, CompressedImageFrame>();
  private readonly pending = new Map<LiveStageCameraId, PendingFrame>();
  private readonly releaseTimers = new Map<string, number>();
  private flushHandle: number | null = null;
  private disposed = false;

  get(cameraId: LiveStageCameraId): CompressedImageFrame | null {
    return this.frames.get(cameraId) ?? null;
  }

  /**
   * React 18 development StrictMode deliberately performs one setup/cleanup/
   * setup cycle for effects.  `dispose()` is still the correct final-unmount
   * cleanup, but the same hook instance must be able to accept frames again
   * during that verification cycle.
   */
  activate(): void {
    this.disposed = false;
  }

  subscribe(cameraId: LiveStageCameraId, listener: LiveCameraMediaListener): () => void {
    let subscribers = this.listeners.get(cameraId);
    if (!subscribers) {
      subscribers = new Set();
      this.listeners.set(cameraId, subscribers);
    }
    subscribers.add(listener);
    return () => {
      subscribers?.delete(listener);
      if (subscribers?.size === 0) this.listeners.delete(cameraId);
    };
  }

  ingest(
    cameraId: LiveStageCameraId,
    message: RosCompressedImagePayload,
    topic: string,
  ): void {
    if (this.disposed) return;
    this.pending.set(cameraId, {
      message,
      topic,
      receivedAt: Date.now(),
    });
    if (this.flushHandle !== null) return;
    this.flushHandle = window.requestAnimationFrame(() => {
      this.flushHandle = null;
      this.flush();
    });
  }

  expireOlderThan(maxAgeMs: number, now = Date.now()): void {
    for (const cameraId of LIVE_STAGE_CAMERA_IDS) {
      const frame = this.frames.get(cameraId);
      if (frame && now - frame.receivedAt > maxAgeMs) this.clear(cameraId);
    }
  }

  clear(cameraId?: LiveStageCameraId): void {
    const cameraIds = cameraId ? [cameraId] : LIVE_STAGE_CAMERA_IDS;
    if (!cameraId) {
      this.pending.clear();
      if (this.flushHandle !== null) {
        window.cancelAnimationFrame(this.flushHandle);
        this.flushHandle = null;
      }
    } else {
      this.pending.delete(cameraId);
    }
    for (const id of cameraIds) {
      const previous = this.frames.get(id);
      if (!previous) continue;
      this.frames.delete(id);
      this.emit(id, null);
      this.scheduleRelease(previous.src);
    }
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.flushHandle !== null) {
      window.cancelAnimationFrame(this.flushHandle);
      this.flushHandle = null;
    }
    this.pending.clear();
    const activeUrls = Array.from(this.frames.values(), (frame) => frame.src);
    this.frames.clear();
    for (const [src, timer] of this.releaseTimers) {
      window.clearTimeout(timer);
      URL.revokeObjectURL(src);
    }
    this.releaseTimers.clear();
    for (const src of activeUrls) URL.revokeObjectURL(src);
    this.listeners.clear();
  }

  private flush(): void {
    if (this.disposed || this.pending.size === 0) return;
    const pending = Array.from(this.pending.entries());
    this.pending.clear();
    for (const [cameraId, nextPending] of pending) {
      const next = frameFromPending(nextPending);
      if (!next) continue;
      const previous = this.frames.get(cameraId);
      this.frames.set(cameraId, next);
      this.emit(cameraId, next);
      if (previous) this.scheduleRelease(previous.src);
    }
  }

  private emit(cameraId: LiveStageCameraId, frame: CompressedImageFrame | null): void {
    for (const listener of this.listeners.get(cameraId) ?? []) listener(frame);
  }

  private scheduleRelease(src: string): void {
    if (this.releaseTimers.has(src)) return;
    const timer = window.setTimeout(() => {
      this.releaseTimers.delete(src);
      URL.revokeObjectURL(src);
    }, RELEASE_PREVIOUS_URL_AFTER_MS);
    this.releaseTimers.set(src, timer);
  }
}

export function createLiveCameraMediaStore(): LiveCameraMediaStore {
  return new LiveCameraMediaStore();
}
