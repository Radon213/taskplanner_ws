import { useCallback, useSyncExternalStore } from "react";

import type {
  TypedRfdetrToolDetectionFrame,
  TypedRfdetrToolViewId,
} from "../ros/toolObservationMessages";
import type { TypedRfdetrObservationStore } from "../ros/typedRfdetrObservationStore";

export function useTypedRfdetrObservation(
  store: TypedRfdetrObservationStore | undefined,
  cameraId: TypedRfdetrToolViewId | null,
  fallback?: TypedRfdetrToolDetectionFrame | null,
): TypedRfdetrToolDetectionFrame | null {
  const subscribe = useCallback(
    (onStoreChange: () => void) => (
      store && cameraId ? store.subscribe(cameraId, onStoreChange) : (() => {})
    ),
    [cameraId, store],
  );
  const getSnapshot = useCallback(
    () => (store && cameraId ? store.get(cameraId) : fallback) ?? null,
    [cameraId, fallback, store],
  );

  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
