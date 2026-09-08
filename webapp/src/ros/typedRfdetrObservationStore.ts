import type {
  TypedRfdetrToolDetectionFrame,
  TypedRfdetrToolViewId,
} from "./toolObservationMessages";

export type TypedRfdetrObservationListener = (
  frame: TypedRfdetrToolDetectionFrame | null,
) => void;

/**
 * Latest-only browser projection for typed detector geometry.
 *
 * The wire message can carry large lossless masks, but those masks are removed
 * by the normalizer before this store sees a frame. Keeping detector cadence
 * outside root React state prevents an observer-only overlay from repainting
 * the mission controls, ASR panel, and Digital Twin on every observation.
 */
export class TypedRfdetrObservationStore {
  private readonly frames = new Map<
    TypedRfdetrToolViewId,
    TypedRfdetrToolDetectionFrame
  >();
  private readonly listeners = new Map<
    TypedRfdetrToolViewId,
    Set<TypedRfdetrObservationListener>
  >();

  get(cameraId: TypedRfdetrToolViewId): TypedRfdetrToolDetectionFrame | null {
    return this.frames.get(cameraId) ?? null;
  }

  ingest(frame: TypedRfdetrToolDetectionFrame): void {
    const previous = this.frames.get(frame.cameraId);
    if (previous && frame.sourceStampSec <= previous.sourceStampSec) return;
    this.frames.set(frame.cameraId, frame);
    this.emit(frame.cameraId, frame);
  }

  subscribe(
    cameraId: TypedRfdetrToolViewId,
    listener: TypedRfdetrObservationListener,
  ): () => void {
    const cameraListeners = this.listeners.get(cameraId) ?? new Set();
    cameraListeners.add(listener);
    this.listeners.set(cameraId, cameraListeners);
    return () => {
      cameraListeners.delete(listener);
      if (cameraListeners.size === 0) this.listeners.delete(cameraId);
    };
  }

  expireOlderThan(maxAgeMs: number, now = Date.now()): void {
    for (const cameraId of ["cam3", "cam4"] as const) {
      const frame = this.frames.get(cameraId);
      if (frame && now - frame.receivedAt > maxAgeMs) this.clear(cameraId);
    }
  }

  clear(cameraId?: TypedRfdetrToolViewId): void {
    const cameraIds = cameraId ? [cameraId] : (["cam3", "cam4"] as const);
    for (const id of cameraIds) {
      if (!this.frames.delete(id)) continue;
      this.emit(id, null);
    }
  }

  private emit(
    cameraId: TypedRfdetrToolViewId,
    frame: TypedRfdetrToolDetectionFrame | null,
  ): void {
    for (const listener of this.listeners.get(cameraId) ?? []) listener(frame);
  }
}

export function createTypedRfdetrObservationStore(): TypedRfdetrObservationStore {
  return new TypedRfdetrObservationStore();
}
